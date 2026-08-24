"""FastAPI applications for the NanoCat core API and browser-facing BFF."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from copy import copy
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import httpx
from fastapi import (
    Depends,
    FastAPI,
    File,
    HTTPException,
    Query,
    Request,
    Response,
    Security,
    UploadFile,
)
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from starlette.requests import ClientDisconnect
from starlette.types import ASGIApp, Receive, Scope, Send

from nanocat.api.artifacts import ArtifactRegistry, parse_range, read_range
from nanocat.api.auth import ApiAuthenticator, WebSessionAuth, token_fingerprint
from nanocat.api.events import SseBroker, SseCapacityError
from nanocat.application.configuration import (
    ConfigurationConflictError,
    ConfigurationError,
    ConfigurationValidationError,
    restart_setting_paths,
    setting_apply_mode,
)
from nanocat.bus.queue import MessageBus
from nanocat.channels.web import WebChannel, WebIngressRejectedError
from nanocat.observability.redaction import redact_value


class ApiModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class TurnBody(ApiModel):
    content: str = Field(default="", max_length=1_000_000)
    attachment_ids: list[str] = Field(default_factory=list, max_length=16)


class SessionRenameBody(ApiModel):
    title: str = Field(default="", max_length=200)


class SessionCreateBody(ApiModel):
    title: str | None = Field(default=None, max_length=200)


class ModelSelectBody(ApiModel):
    slot: str
    model: str | None


class EffortBody(ApiModel):
    value: str


class PulseBody(ApiModel):
    enabled: bool


class ModelCatalogBody(ApiModel):
    provider: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=500)


class ApprovalBody(ApiModel):
    request_id: str
    action: str


class ApprovalDecisionBody(ApiModel):
    action: str


class CommandBody(ApiModel):
    text: str = Field(max_length=100_000)
    session_id: str | None = None


class SettingsUpdateBody(ApiModel):
    values: dict[str, Any] = Field(min_length=1, max_length=512)


class LoginBody(ApiModel):
    password: str = Field(max_length=4096)


def _bind_artifact_lease(
    turns: Any,
    artifact_registry: ArtifactRegistry,
    turn_id: str,
    lease_id: str,
) -> None:
    """Release a lease once its turn is terminal, including retention races."""
    try:
        turns.add_terminal_finalizer(
            turn_id,
            lambda: artifact_registry.schedule_release_lease(lease_id),
        )
    except KeyError:
        artifact_registry.release_lease(lease_id)


_CAMEL_BOUNDARY = re.compile(r"_([a-z])")
_DEFAULT_MEDIA_FILE_BYTES = 64 * 1024 * 1024
_MULTIPART_OVERHEAD_BYTES = 1024 * 1024
_MAX_CONCURRENT_MEDIA_REQUESTS = 4
_MAX_SETTINGS_UPDATE_BYTES = 1024 * 1024
_MAX_JSON_BODY_BYTES = 2 * 1024 * 1024
_MAX_LOGIN_BODY_BYTES = 8192
_REQUEST_READ_TIMEOUT_S = 30.0
_SECRET_FIELDS = frozenset(
    {
        "authorization",
        "cookie",
        "password",
        "secret",
        "token",
        "authtoken",
        "auth_token",
        "accesstoken",
        "access_token",
        "refreshtoken",
        "refresh_token",
        "api_key",
        "apikey",
        "credential",
        "private_key",
        "access_key",
        "client_secret",
    }
)
_SECRET_MARKERS = (
    "password",
    "secret",
    "token",
    "authorization",
    "cookie",
    "credential",
    "api_key",
    "apikey",
    "private_key",
    "access_key",
)
_OPAQUE_SECRET_MAP_FIELDS = frozenset(
    {"env", "headers", "extra_headers", "extraheaders"}
)


class _DynamicCorsMiddleware:
    """Evaluate CORS origins from the live runtime config for every request."""

    def __init__(self, app: ASGIApp, *, config: Any) -> None:
        self._app = app
        self._config = config

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        origins = list(getattr(self._config.api, "cors_origins", []) or [])
        middleware = CORSMiddleware(
            self._app,
            allow_origins=origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "PATCH", "DELETE"],
            allow_headers=[
                "Authorization",
                "Content-Type",
                "Idempotency-Key",
                "If-Match",
                "Last-Event-ID",
                "Range",
            ],
            expose_headers=[
                "Accept-Ranges",
                "Content-Disposition",
                "Content-Range",
                "ETag",
                "Location",
                "Retry-After",
            ],
        )
        await middleware(scope, receive, send)


def _camelize(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            _CAMEL_BOUNDARY.sub(lambda match: match.group(1).upper(), str(key)): _camelize(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_camelize(item) for item in value]
    return value


def _redact(value: Any, key: str = "") -> Any:
    normalized = key.casefold().replace("-", "_")
    if normalized in _OPAQUE_SECRET_MAP_FIELDS:
        serialized = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        result: dict[str, Any] = {"configured": bool(value)}
        if isinstance(value, Mapping):
            result["keys"] = sorted(str(child) for child in value)
        if value:
            result["fingerprint"] = token_fingerprint(serialized)
        return result
    is_secret = normalized in _SECRET_FIELDS or any(
        marker in normalized for marker in _SECRET_MARKERS
    )
    if is_secret:
        if "password" in normalized or "secret" in normalized:
            return {"configured": bool(value)}
        if isinstance(value, str) and value:
            return {"configured": True, "fingerprint": token_fingerprint(value)}
        return {"configured": False}
    if isinstance(value, Mapping):
        return {str(child): _redact(item, str(child)) for child, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return redact_value(value)


def _resolve_json_schema(schema: Mapping[str, Any], root: Mapping[str, Any]) -> Mapping[str, Any]:
    reference = schema.get("$ref")
    if isinstance(reference, str) and reference.startswith("#/$defs/"):
        resolved = root.get("$defs", {}).get(reference.rsplit("/", 1)[-1], {})
        if isinstance(resolved, Mapping):
            return resolved
    alternatives = schema.get("anyOf")
    if isinstance(alternatives, list):
        for alternative in alternatives:
            if isinstance(alternative, Mapping) and alternative.get("type") != "null":
                return _resolve_json_schema(alternative, root)
    return schema


def _setting_field_schema(
    value: Any,
    key: str = "",
    path: str = "",
    *,
    schema: Mapping[str, Any] | None = None,
    root_schema: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Describe browser-editable leaves without exposing secret values."""
    if root_schema is None:
        from nanocat.config.schema import Config

        root_schema = Config.model_json_schema(by_alias=True)
    schema = _resolve_json_schema(schema or root_schema, root_schema)
    normalized = key.casefold().replace("-", "_")
    is_secret = normalized in _SECRET_FIELDS or any(
        marker in normalized for marker in _SECRET_MARKERS
    )
    is_opaque_map = normalized in _OPAQUE_SECRET_MAP_FIELDS
    if path and (is_secret or is_opaque_map):
        field = {
            "type": "json" if isinstance(value, (Mapping, list, tuple)) else "password",
            "secret": True,
            "applyMode": setting_apply_mode(path),
        }
        if schema.get("description"):
            field["description"] = schema["description"]
        return {path: field}
    if isinstance(value, Mapping):
        if path and not value:
            return {
                path: {
                    "type": "json",
                    "secret": False,
                    "applyMode": setting_apply_mode(path),
                }
            }
        fields: dict[str, Any] = {}
        properties = schema.get("properties", {})
        additional = schema.get("additionalProperties", {})
        for child, item in value.items():
            child_path = f"{path}.{child}" if path else str(child)
            child_schema = properties.get(child, additional)
            fields.update(
                _setting_field_schema(
                    item,
                    str(child),
                    child_path,
                    schema=child_schema if isinstance(child_schema, Mapping) else {},
                    root_schema=root_schema,
                )
            )
        return fields
    if isinstance(value, (list, tuple)):
        field = {
            "type": "json",
            "secret": False,
            "applyMode": setting_apply_mode(path),
        }
        if schema.get("description"):
            field["description"] = schema["description"]
        return {path: field}
    field_type = (
        "boolean"
        if isinstance(value, bool)
        else "number"
        if isinstance(value, (int, float))
        else "text"
    )
    field = {
        "type": field_type,
        "secret": False,
        "applyMode": setting_apply_mode(path),
    }
    enum = schema.get("enum")
    if isinstance(enum, list):
        field["enum"] = enum
    for source, target in (
        ("minimum", "minimum"),
        ("maximum", "maximum"),
        ("exclusiveMinimum", "exclusiveMinimum"),
        ("exclusiveMaximum", "exclusiveMaximum"),
        ("minLength", "minLength"),
        ("maxLength", "maxLength"),
    ):
        if source in schema:
            field[target] = schema[source]
    if schema.get("description"):
        field["description"] = schema["description"]
    return {path: field}


def _problem(
    status_code: int,
    code: str,
    detail: str,
    *,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        media_type="application/problem+json",
        headers=headers,
        content={
            "type": f"urn:nanocat:error:{code}",
            "title": code.replace("_", " ").title(),
            "status": status_code,
            "detail": detail,
            "code": code,
        },
    )


class _RequestBodyTooLargeError(RuntimeError):
    pass


class _RequestReadTimeoutError(RuntimeError):
    pass


class _RequestBodyLimitMiddleware:
    """Bound request bodies before framework parsing, including chunked input."""

    def __init__(
        self,
        app: Any,
        *,
        default_limit: int,
        path_limits: Mapping[str, int] | None = None,
        read_timeout_s: float = _REQUEST_READ_TIMEOUT_S,
    ) -> None:
        self.app = app
        self.default_limit = default_limit
        self.path_limits = dict(path_limits or {})
        self.read_timeout_s = read_timeout_s

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or scope.get("method") in {"GET", "HEAD", "OPTIONS"}:
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path") or "")
        limit = self.path_limits.get(path, self.default_limit)
        headers = {
            key.decode("latin-1").casefold(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        raw_length = headers.get("content-length")
        if raw_length is not None:
            try:
                if int(raw_length) > limit:
                    await _problem(413, "request_too_large", "The request body is too large.")(
                        scope, receive, send
                    )
                    return
            except ValueError:
                await _problem(400, "invalid_content_length", "Content-Length is invalid.")(
                    scope, receive, send
                )
                return
        received = 0
        deadline = asyncio.get_running_loop().time() + self.read_timeout_s

        async def limited_receive() -> Any:
            nonlocal received
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise _RequestReadTimeoutError("request body read timed out")
            try:
                message = await asyncio.wait_for(receive(), timeout=remaining)
            except asyncio.TimeoutError as exc:
                raise _RequestReadTimeoutError("request body read timed out") from exc
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _RequestBodyTooLargeError("request body limit exceeded")
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _RequestBodyTooLargeError:
            await _problem(413, "request_too_large", "The request body is too large.")(
                scope, receive, send
            )
        except _RequestReadTimeoutError:
            await _problem(408, "request_timeout", "The request body was not received in time.")(
                scope, receive, send
            )


def _media_length_error(request: Request, max_request_bytes: int) -> Response | None:
    raw_length = request.headers.get("content-length")
    if raw_length is None:
        return _problem(411, "length_required", "Content-Length is required for media uploads.")
    try:
        content_length = int(raw_length)
    except ValueError:
        return _problem(400, "invalid_content_length", "Content-Length is invalid.")
    if content_length < 0:
        return _problem(400, "invalid_content_length", "Content-Length is invalid.")
    if content_length > max_request_bytes:
        return _problem(413, "media_too_large", "The media upload exceeds the request limit.")
    return None


class _IdempotencyCache:
    """Bounded process-local response cache for accepted mutations."""

    def __init__(self, *, max_entries: int = 2048, ttl_s: float = 24 * 3600) -> None:
        self._entries: OrderedDict[
            str, tuple[str, float, tuple[int, bytes, dict[str, str], str | None]]
        ] = OrderedDict()
        self._max_entries = max_entries
        self._ttl_s = ttl_s
        self._key_locks: dict[str, tuple[asyncio.Lock, int]] = {}
        self._inflight: dict[
            str,
            tuple[
                str,
                asyncio.Task[tuple[int, bytes, dict[str, str], str | None]],
            ],
        ] = {}
        self._closed = False

    @staticmethod
    def _snapshot(response: Response) -> tuple[int, bytes, dict[str, str], str | None]:
        headers = {
            name: value
            for name, value in response.headers.items()
            if name.casefold() in {"cache-control", "etag", "location", "retry-after"}
        }
        return response.status_code, bytes(response.body), headers, response.media_type

    @staticmethod
    def _restore(snapshot: tuple[int, bytes, dict[str, str], str | None]) -> Response:
        status_code, body, headers, media_type = snapshot
        return Response(
            content=body,
            status_code=status_code,
            headers=headers,
            media_type=media_type or "application/json",
        )

    async def _complete(
        self,
        cache_key: str,
        fingerprint: str,
        operation: Callable[[], Awaitable[Response]],
    ) -> tuple[int, bytes, dict[str, str], str | None]:
        try:
            snapshot = self._snapshot(await operation())
            if snapshot[0] < 500 and snapshot[0] != 429:
                self._entries[cache_key] = (
                    fingerprint,
                    time.monotonic() + self._ttl_s,
                    snapshot,
                )
                self._entries.move_to_end(cache_key)
                while len(self._entries) > self._max_entries:
                    self._entries.popitem(last=False)
            return snapshot
        finally:
            current = self._inflight.get(cache_key)
            if current is not None and current[1] is asyncio.current_task():
                self._inflight.pop(cache_key, None)

    @staticmethod
    def _consume_task(task: asyncio.Task[Any]) -> None:
        if not task.cancelled():
            task.exception()

    def _register_key(self, key: str) -> asyncio.Lock | None:
        entry = self._key_locks.get(key)
        if entry is None:
            if len(self._key_locks) >= self._max_entries:
                return None
            lock, users = asyncio.Lock(), 0
        else:
            lock, users = entry
        if users >= 64:
            return None
        self._key_locks[key] = (lock, users + 1)
        return lock

    def _release_key(self, key: str, lock: asyncio.Lock) -> None:
        current = self._key_locks.get(key)
        if current is None or current[0] is not lock:
            return
        if current[1] <= 1:
            self._key_locks.pop(key, None)
        else:
            self._key_locks[key] = (lock, current[1] - 1)

    async def execute(
        self,
        key: str | None,
        fingerprint: str,
        operation: Callable[[], Awaitable[Response]],
    ) -> Response:
        if not key:
            return await operation()
        cache_key = key.strip()
        if not cache_key or len(cache_key) > 256:
            return _problem(422, "invalid_idempotency_key", "Invalid Idempotency-Key.")
        lock = self._register_key(cache_key)
        if lock is None:
            response = _problem(
                503,
                "idempotency_capacity",
                "Too many idempotent mutations are already in progress.",
            )
            response.headers["Retry-After"] = "1"
            return response
        try:
            async with lock:
                if self._closed:
                    return _problem(503, "runtime_stopping", "NanoCat API is stopping.")
                now = time.monotonic()
                for stale in [name for name, item in self._entries.items() if item[1] <= now]:
                    self._entries.pop(stale, None)
                cached = self._entries.get(cache_key)
                if cached is not None:
                    stored_fingerprint, _, snapshot = cached
                    if stored_fingerprint != fingerprint:
                        return _problem(
                            409,
                            "idempotency_conflict",
                            "Idempotency-Key was already used for a different mutation.",
                        )
                    self._entries.move_to_end(cache_key)
                    return self._restore(snapshot)
                flight = self._inflight.get(cache_key)
                if flight is not None:
                    if flight[0] != fingerprint:
                        return _problem(
                            409,
                            "idempotency_conflict",
                            "Idempotency-Key is already executing a different mutation.",
                        )
                    task = flight[1]
                else:
                    if len(self._inflight) >= self._max_entries:
                        response = _problem(
                            503,
                            "idempotency_capacity",
                            "Too many idempotent mutations are already in progress.",
                        )
                        response.headers["Retry-After"] = "1"
                        return response
                    task = asyncio.create_task(
                        self._complete(cache_key, fingerprint, operation),
                        name="nanocat.api.idempotent-mutation",
                    )
                    task.add_done_callback(self._consume_task)
                    self._inflight[cache_key] = (fingerprint, task)
            return self._restore(await asyncio.shield(task))
        finally:
            self._release_key(cache_key, lock)

    async def close(self) -> None:
        self._closed = True
        tasks = tuple(flight[1] for flight in self._inflight.values())
        if not tasks:
            return
        try:
            done, pending = await asyncio.wait(tasks, timeout=5.0)
        except asyncio.CancelledError:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        else:
            for task in pending:
                task.cancel()
            await asyncio.gather(*done, *pending, return_exceptions=True)
        finally:
            self._inflight.clear()


class _ClosingStreamingResponse(StreamingResponse):
    """Close the stream owner on normal completion, cancellation, or disconnect."""

    def __init__(self, *args: Any, close_stream: Callable[[], Awaitable[None]], **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._close_stream = close_stream

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self._close_stream()


def _control_response(result: Mapping[str, Any]) -> Response:
    if result.get("ok"):
        return JSONResponse(_camelize(result.get("data") or {}))
    code = str(result.get("code") or "control_error")
    status_code = {
        "not_found": 404,
        "invalid_argument": 422,
        "invalid_state": 409,
        "conflict": 409,
        "session_conflict": 412,
        "delete_failed": 409,
        "persistence_failed": 503,
        "persistence_pending": 503,
        "unknown_action": 404,
    }.get(code, 500)
    headers = {"Retry-After": "1"} if code == "persistence_failed" else None
    return _problem(
        status_code,
        code,
        str(result.get("error") or "Control operation failed"),
        headers=headers,
    )


async def _select_session(control: Any, session_id: str) -> tuple[Any | None, Response | None]:
    target = control._sessions.get_session("web", session_id)
    if target is None:
        return None, _problem(404, "not_found", f"Session `{session_id}` not found.")
    active = control._sessions.get_or_create("web", target.chat_id)
    if active.id == target.id:
        return target, None
    switched = await control.execute(
        "session.switch",
        {"channel": "web", "chat_id": target.chat_id, "session_id": session_id},
    )
    return (target, None) if switched.get("ok") else (None, _control_response(switched))


@asynccontextmanager
async def _guard_session_route(control: Any, session_id: str):
    """Keep one session resource stable across a compound HTTP operation."""
    initial = control._sessions.get_session("web", session_id)
    if initial is None:
        yield None, _problem(404, "not_found", f"Session `{session_id}` not found.")
        return
    chat_id = initial.chat_id
    async with control.routing_guard(f"web:{chat_id}"):
        current = control._sessions.get_session("web", session_id, chat_id=chat_id)
        if current is None:
            yield None, _problem(404, "not_found", f"Session `{session_id}` not found.")
            return
        yield current, None


def _session_revision(session: Any) -> int:
    payload = f"{session.channel}\0{session.id}\0{session.revision}\0{session.name or ''}"
    value = int.from_bytes(hashlib.sha256(payload.encode("utf-8")).digest()[:8], "big")
    return value & ((1 << 53) - 1) or 1


def _require_session_revision(request: Request, session: Any) -> Response | None:
    header = request.headers.get("if-match", "").strip()
    if not header:
        return _problem(428, "precondition_required", "If-Match is required.")
    normalized = header.removeprefix("W/").strip().strip('"')
    try:
        expected = int(normalized)
    except ValueError:
        return _problem(400, "invalid_revision", "If-Match must contain a revision number.")
    if expected != _session_revision(session):
        return _problem(412, "session_conflict", "The session changed after it was loaded.")
    return None


def create_api_app(
    *,
    control: Any,
    channel: WebChannel,
    broker: SseBroker,
    authenticator: ApiAuthenticator,
    config: Any,
    configuration: Any,
    activity_journal: Any | None = None,
    artifact_registry: ArtifactRegistry | None = None,
) -> FastAPI:
    """Create the authenticated core API without owning its listener."""
    idempotency = _IdempotencyCache()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            await idempotency.close()

    app = FastAPI(
        title="NanoCat API",
        version="1",
        docs_url="/docs",
        redoc_url=None,
        lifespan=lifespan,
    )
    media_request_limit = (
        getattr(artifact_registry, "max_file_bytes", _DEFAULT_MEDIA_FILE_BYTES)
        + _MULTIPART_OVERHEAD_BYTES
    )
    media_gate = asyncio.Semaphore(_MAX_CONCURRENT_MEDIA_REQUESTS)
    bearer_scheme = HTTPBearer(auto_error=False)

    async def require_api(
        request: Request,
        _credentials: HTTPAuthorizationCredentials | None = Security(bearer_scheme),
    ) -> str:
        return await authenticator.require(request)

    @app.middleware("http")
    async def guard_media_upload(
        request: Request, call_next: Callable[..., Awaitable[Response]]
    ):
        if request.method == "POST" and request.url.path == "/api/v1/media":
            length_error = _media_length_error(request, media_request_limit)
            if length_error is not None:
                return length_error
            try:
                await authenticator.require(request)
            except HTTPException as exc:
                response = _problem(
                    exc.status_code,
                    "http_error",
                    str(exc.detail or "Authentication required"),
                )
                response.headers.update(exc.headers or {})
                return response
            if media_gate.locked():
                response = _problem(
                    503,
                    "media_capacity",
                    "Too many media uploads are already in progress.",
                )
                response.headers["Retry-After"] = "1"
                return response
            await media_gate.acquire()
            try:
                return await call_next(request)
            finally:
                media_gate.release()
        return await call_next(request)

    @app.middleware("http")
    async def guard_api_surface(
        request: Request, call_next: Callable[..., Awaitable[Response]]
    ) -> Response:
        path = request.url.path
        if path.startswith("/api/v1/") and path != "/api/v1/health":
            try:
                await authenticator.require(request)
            except HTTPException as exc:
                response = _problem(
                    exc.status_code,
                    "http_error",
                    str(exc.detail or "Authentication required"),
                )
                response.headers.update(exc.headers or {})
                return response
        response = await call_next(request)
        if path.startswith("/api/v1/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    app.add_middleware(_DynamicCorsMiddleware, config=config)

    @app.exception_handler(HTTPException)
    async def handle_http_exception(_request: Request, exc: HTTPException) -> Response:
        detail = str(exc.detail or "Request failed")
        response = _problem(exc.status_code, "http_error", detail)
        response.headers.update(exc.headers or {})
        return response

    @app.exception_handler(RequestValidationError)
    async def handle_validation(_request: Request, exc: RequestValidationError) -> Response:
        return _problem(422, "validation_error", str(exc))

    auth = require_api
    @app.get("/api/v1/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "service": "nanocat", "apiVersion": "v1"}

    @app.get("/api/v1/runtime", dependencies=[Depends(auth)])
    async def runtime_snapshot() -> Response:
        result = await control.execute(
            "runtime.snapshot", {"channel": "web", "principal_id": "web:local"}
        )
        return _control_response(result)

    @app.post("/api/v1/runtime/restart", dependencies=[Depends(auth)])
    async def runtime_restart() -> Response:
        result = await control.execute(
            "runtime.restart", {"channel": "web", "principal_id": "web:local"}
        )
        return _control_response(result)

    @app.get("/api/v1/sessions", dependencies=[Depends(auth)])
    async def sessions(limit: int = 50, min_turns: int = 0) -> Response:
        result = await control.execute(
            "sessions.list",
            {
                "channel": "web",
                "limit": min(max(limit, 1), 1000),
                "min_turns": max(min_turns, 0),
            },
        )
        if result.get("ok"):
            for item in result.get("data", {}).get("sessions", []):
                target = control._sessions.get_session("web", str(item.get("id") or ""))
                if target is not None:
                    item["revision"] = _session_revision(target)
                    item["busy"] = control._session_busy(target.key)
        return _control_response(result)

    @app.post("/api/v1/sessions", dependencies=[Depends(auth)], status_code=201)
    async def session_new(request: Request, body: SessionCreateBody | None = None) -> Response:
        async def create() -> Response:
            chat_id = uuid4().hex
            result = await control.execute(
                "session.create",
                {"channel": "web", "chat_id": chat_id, "name": body.title if body else None},
            )
            if result.get("ok"):
                data = result.get("data") or {}
                session_id = str(data.get("session_id") or data.get("id") or "")
                target = control._sessions.get_session("web", session_id)
                if target is not None:
                    data["revision"] = _session_revision(target)
                return JSONResponse(status_code=201, content=_camelize(data))
            return _control_response(result)

        fingerprint = hashlib.sha256(
            json.dumps(
                body.model_dump(mode="json") if body else {},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        return await idempotency.execute(
            request.headers.get("idempotency-key"), fingerprint, create
        )

    @app.get("/api/v1/sessions/{session_id}", dependencies=[Depends(auth)])
    async def session_get(session_id: str) -> Response:
        target = control._sessions.get_session("web", session_id)
        if target is None:
            return _problem(404, "not_found", f"Session `{session_id}` not found.")
        revision = _session_revision(target)
        data = {
            "sessionId": target.id,
            "sessionName": target.name or "",
            "revision": revision,
            "busy": control._session_busy(target.key),
        }
        response = JSONResponse(_camelize(data))
        response.headers["ETag"] = f'"{revision}"'
        return response

    async def hydrate_artifact_status(payload: dict[str, Any]) -> None:
        refs: list[dict[str, Any]] = []
        for collection_name in ("items", "historicalItems"):
            for item in payload.get(collection_name) or []:
                if not isinstance(item, dict):
                    continue
                metadata = item.get("metadata")
                if not isinstance(metadata, dict):
                    continue
                values = metadata.get("artifactRefs")
                if isinstance(values, list):
                    refs.extend(value for value in values if isinstance(value, dict))
        if not refs:
            return

        artifact_ids = {str(ref.get("id") or "") for ref in refs if ref.get("id")}
        resolved = (
            await artifact_registry.resolve_public_map_async(artifact_ids)
            if artifact_registry is not None
            else {artifact_id: None for artifact_id in artifact_ids}
        )
        for ref in refs:
            artifact_id = str(ref.get("id") or "")
            current = resolved.get(artifact_id)
            if current is None:
                ref["expired"] = True
            else:
                ref.update(current)
                ref["expired"] = False

    async def project_session(
        session_id: str,
        *,
        trajectory: bool,
        cursor: int,
        limit: int,
        latest: bool = False,
        history_cursor: int = 0,
        history_limit: int = 1000,
    ) -> Response:
        from nanocat.application.projections import ConversationProjection, TrajectoryProjection

        session = await asyncio.to_thread(
            control._sessions.get_session, "web", session_id
        )
        if session is None:
            return _problem(404, "not_found", f"Session `{session_id}` not found.")
        projection = TrajectoryProjection if trajectory else ConversationProjection
        bounded_limit = min(max(limit, 1), 1000)
        if not trajectory:
            projected = await asyncio.to_thread(
                projection.from_session_recent if latest else projection.from_session,
                session,
                **({} if latest else {"offset": max(cursor, 0)}),
                limit=bounded_limit,
            )
            payload = projected.to_dict()
            if latest:
                payload["pageOffset"] = max(
                    0, projected.total_count - len(projected.items)
                )
            await hydrate_artifact_status(payload)
            return JSONResponse(payload)
        if activity_journal is not None:
            requested_cursor = max(cursor, 0)
            if latest:
                page = await asyncio.to_thread(
                    activity_journal.read_recent,
                    session.key,
                    limit=bounded_limit,
                )
            else:
                page = await asyncio.to_thread(
                    activity_journal.read_page,
                    session.key,
                    after_sequence=requested_cursor,
                    limit=bounded_limit,
                )
            cursor_expired = (
                page.oldest_sequence is not None
                and 0 < requested_cursor < page.oldest_sequence - 1
            )
            cursor_future = (
                page.latest_sequence is not None
                and requested_cursor > page.latest_sequence
            )
            cursor_reset = cursor_expired or cursor_future
            if cursor_reset:
                page = await asyncio.to_thread(
                    activity_journal.read_page,
                    session.key,
                    after_sequence=0,
                    limit=bounded_limit,
                )
            if page.events or page.oldest_sequence is not None:
                projected = await asyncio.to_thread(
                    projection.from_activity,
                    page.events,
                    offset=0,
                    limit=bounded_limit,
                )
                payload = projected.to_dict()
                payload["nextCursor"] = page.next_cursor
                payload["previousCursor"] = (
                    max(0, page.events[0].sequence - 1)
                    if latest and page.events
                    else None
                )
                payload["hasMore"] = page.has_more
                payload["oldestSequence"] = page.oldest_sequence
                payload["latestSequence"] = page.latest_sequence
                payload["cursorReset"] = cursor_reset
                payload["gapDetected"] = cursor_expired
                payload["cursorResetReason"] = (
                    "retentionGap"
                    if cursor_expired
                    else "cursorAhead"
                    if cursor_future
                    else None
                )
                payload["source"] = "activity"
                payload["cursorKind"] = "activitySequence"
                if latest or requested_cursor == 0 or cursor_reset or history_cursor > 0:
                    first_page = await asyncio.to_thread(
                        activity_journal.read_page,
                        session.key,
                        after_sequence=0,
                        limit=1,
                    )
                    first_timestamp = (
                        first_page.events[0].recorded_at.isoformat()
                        if first_page.events
                        else None
                    )
                    history_message_count = (
                        first_page.events[0].metadata.get("historyMessageCount")
                        if first_page.events
                        else None
                    )
                    if isinstance(history_message_count, bool) or not isinstance(
                        history_message_count, int
                    ):
                        history_message_count = None
                    elif history_message_count < 0:
                        history_message_count = 0
                    else:
                        history_message_count = min(
                            history_message_count, len(session.messages)
                        )
                    bounded_history_limit = min(max(history_limit, 1), 1000)

                    def project_history() -> Any:
                        history_session = session
                        if history_message_count is not None:
                            history_session = copy(session)
                            history_session.messages = list(
                                session.messages[:history_message_count]
                            )
                            history_session.last_compacted = min(
                                history_session.last_compacted,
                                len(history_session.messages),
                            )
                        if latest and history_cursor == 0:
                            return TrajectoryProjection.from_session_recent(
                                history_session,
                                limit=bounded_history_limit,
                            )
                        return TrajectoryProjection.from_session(
                            history_session,
                            offset=max(history_cursor, 0),
                            limit=bounded_history_limit,
                        )

                    historical = await asyncio.to_thread(
                        project_history,
                    )
                    historical_items = []
                    reached_activity = False
                    for item in historical.items:
                        if (
                            history_message_count is None
                            and first_timestamp is not None
                            and item.timestamp is not None
                            and item.timestamp >= first_timestamp
                        ):
                            reached_activity = True
                            continue
                        historical_items.append(item.to_dict())
                    historical_has_more = (
                        historical.previous_offset is not None
                        if latest and history_cursor == 0
                        else historical.has_more
                    ) and not reached_activity
                    payload["historicalItems"] = historical_items
                    payload["historicalTotalCount"] = historical.total_count
                    if latest and history_cursor == 0:
                        payload["historicalPageOffset"] = max(
                            0, historical.total_count - len(historical.items)
                        )
                    payload["historicalHasMore"] = historical_has_more
                    payload["nextHistoricalCursor"] = (
                        (
                            historical.previous_offset
                            if latest and history_cursor == 0
                            else historical.next_offset
                        )
                        if historical_has_more
                        else None
                    )
                    payload["historicalCursorKind"] = "offset"
                await hydrate_artifact_status(payload)
                return JSONResponse(payload)
        projected = await asyncio.to_thread(
            projection.from_session_recent if latest else projection.from_session,
            session,
            **({} if latest else {"offset": max(cursor, 0)}),
            limit=bounded_limit,
        )
        payload = projected.to_dict()
        if latest:
            payload["pageOffset"] = max(
                0, projected.total_count - len(projected.items)
            )
        payload["source"] = "session"
        payload["cursorKind"] = "offset"
        await hydrate_artifact_status(payload)
        return JSONResponse(payload)

    @app.get("/api/v1/sessions/{session_id}/conversation", dependencies=[Depends(auth)])
    async def session_conversation(
        session_id: str,
        cursor: int = 0,
        limit: int = 100,
        latest: bool = False,
    ) -> Response:
        return await project_session(
            session_id,
            trajectory=False,
            cursor=cursor,
            limit=limit,
            latest=latest,
        )

    @app.get("/api/v1/sessions/{session_id}/trajectory", dependencies=[Depends(auth)])
    async def session_trajectory(
        session_id: str,
        cursor: int = 0,
        limit: int = 100,
        latest: bool = False,
        history_cursor: int = Query(default=0, alias="historyCursor", ge=0),
        history_limit: int = Query(default=1000, alias="historyLimit", ge=1, le=1000),
    ) -> Response:
        return await project_session(
            session_id,
            trajectory=True,
            cursor=cursor,
            limit=limit,
            latest=latest,
            history_cursor=history_cursor,
            history_limit=history_limit,
        )

    @app.post("/api/v1/sessions/{session_id}/activate", dependencies=[Depends(auth)])
    async def session_activate(session_id: str) -> Response:
        target = control._sessions.get_session("web", session_id)
        if target is None:
            return _problem(404, "not_found", f"Session `{session_id}` not found.")
        result = await control.execute(
            "session.switch",
            {"channel": "web", "chat_id": target.chat_id, "session_id": session_id},
        )
        return _control_response(result)

    @app.patch("/api/v1/sessions/{session_id}", dependencies=[Depends(auth)])
    async def session_rename(
        session_id: str, body: SessionRenameBody, request: Request
    ) -> Response:
        target = control._sessions.get_session("web", session_id)
        if target is None:
            return _problem(404, "not_found", f"Session `{session_id}` not found.")
        revision_error = _require_session_revision(request, target)
        if revision_error is not None:
            return revision_error
        result = await control.execute(
            "session.rename",
            {
                "channel": "web",
                "chat_id": target.chat_id,
                "session_id": session_id,
                "name": body.title,
                "expected_revision": target.revision,
                "expected_name": target.name,
            },
        )
        if not result.get("ok"):
            return _control_response(result)
        updated = control._sessions.get_session("web", session_id)
        data = result.get("data") or {}
        if updated is not None:
            revision = _session_revision(updated)
            data["revision"] = revision
            response = JSONResponse(_camelize(data))
            response.headers["ETag"] = f'"{revision}"'
            return response
        return _control_response(result)

    @app.delete("/api/v1/sessions/{session_id}", dependencies=[Depends(auth)])
    async def session_delete(session_id: str, request: Request) -> Response:
        target = control._sessions.get_session("web", session_id)
        if target is None:
            return _problem(404, "not_found", f"Session `{session_id}` not found.")
        revision_error = _require_session_revision(request, target)
        if revision_error is not None:
            return revision_error
        result = await control.execute(
            "session.delete",
            {
                "channel": "web",
                "chat_id": target.chat_id,
                "session_id": session_id,
                "create_replacement": False,
                "expected_revision": target.revision,
                "expected_name": target.name,
            },
        )
        return _control_response(result)

    async def submit_turn(
        session_id: str, body: TurnBody, request: Request, *, steer: bool
    ) -> Response:
        if not body.content.strip() and not body.attachment_ids:
            return _problem(422, "invalid_argument", "A turn requires content or media.")
        if MessageBus.is_command_candidate(body.content):
            return _problem(
                422,
                "command_input",
                "Slash commands must use POST /api/v1/commands/execute.",
            )

        async def accept() -> Response:
            initial = control._sessions.get_session("web", session_id)
            if initial is None:
                return _problem(404, "not_found", f"Session `{session_id}` not found.")
            route_key = f"web:{initial.chat_id}"
            async with control.routing_guard(route_key):
                if control._engine.has_pending_session_durability(route_key):
                    control._engine.request_session_durability_retry(route_key)
                    return _problem(
                        503,
                        "persistence_pending",
                        "Earlier session history is still awaiting durable storage.",
                        headers={"Retry-After": "1"},
                    )
                target, selection_error = await _select_session(control, session_id)
                if selection_error is not None:
                    return selection_error
                from nanocat.application.turns import TurnState

                live_turns = [
                    record
                    for record in control._engine.turns.snapshot()
                    if record.session_key == route_key
                    and record.state
                    not in {TurnState.COMPLETED, TurnState.CANCELLED, TurnState.FAILED}
                ]
                if not steer and live_turns:
                    response = _problem(
                        409,
                        "turn_in_progress",
                        "A turn is already active for this session.",
                    )
                    response.headers["Retry-After"] = "1"
                    return response
                if steer and any(
                    record.state is TurnState.CANCELLING for record in live_turns
                ):
                    response = _problem(
                        409,
                        "turn_cancelling",
                        "The active turn is still cancelling.",
                    )
                    response.headers["Retry-After"] = "1"
                    return response
                lease_id: str | None = None
                artifact_refs: list[dict[str, Any]] = []
                try:
                    media, lease_id = (
                        await artifact_registry.lease_paths_async(body.attachment_ids)
                        if artifact_registry is not None
                        else ([], None)
                    )
                    if artifact_registry is not None and body.attachment_ids:
                        artifact_refs = await artifact_registry.public_refs_async(
                            body.attachment_ids
                        )
                except KeyError as exc:
                    if artifact_registry is not None:
                        artifact_registry.release_lease(lease_id)
                    return _problem(
                        404,
                        "artifact_not_found",
                        f"Artifact `{exc.args[0]}` not found.",
                    )
                except BaseException:
                    if artifact_registry is not None:
                        artifact_registry.release_lease(lease_id)
                    raise
                if body.attachment_ids and artifact_registry is None:
                    return _problem(
                        503,
                        "artifact_unavailable",
                        "Artifact storage is unavailable.",
                    )
                unsupported = [
                    ref for ref in artifact_refs if ref.get("kind") == "binary"
                ]
                if unsupported:
                    if artifact_registry is not None:
                        artifact_registry.release_lease(lease_id)
                    return _problem(
                        415,
                        "unsupported_artifact_type",
                        "One or more attachments cannot be read by the agent.",
                    )
                try:
                    accepted = await channel.submit(
                        body.content,
                        chat_id=target.chat_id,
                        media=media,
                        artifact_refs=artifact_refs,
                        session_id=session_id,
                        steer=steer,
                    )
                except WebIngressRejectedError as exc:
                    if artifact_registry is not None:
                        artifact_registry.release_lease(lease_id)
                    status_code = (
                        422
                        if exc.reason == "command_input"
                        else
                        503
                        if exc.reason
                        in {"busy", "unavailable", "capacity_timeout", "durability_pending"}
                        else 404
                        if exc.reason == "session_missing"
                        else 403
                    )
                    response = _problem(
                        status_code,
                        "ingress_rejected",
                        "The runtime could not accept this turn.",
                    )
                    if exc.retry_after is not None:
                        response.headers["Retry-After"] = str(exc.retry_after)
                    return response
                except BaseException:
                    if artifact_registry is not None:
                        artifact_registry.release_lease(lease_id)
                    raise
                if lease_id is not None and artifact_registry is not None:
                    _bind_artifact_lease(
                        control._engine.turns,
                        artifact_registry,
                        accepted["turnId"],
                        lease_id,
                    )
            headers = {"Location": f"/api/v1/turns/{accepted['turnId']}"}
            return JSONResponse(status_code=202, content=accepted, headers=headers)

        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "sessionId": session_id,
                    "steer": steer,
                    "body": body.model_dump(mode="json"),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        return await idempotency.execute(
            request.headers.get("idempotency-key"), fingerprint, accept
        )

    @app.post("/api/v1/sessions/{session_id}/turns", dependencies=[Depends(auth)])
    async def turn_submit(session_id: str, body: TurnBody, request: Request) -> Response:
        return await submit_turn(session_id, body, request, steer=False)

    @app.post("/api/v1/sessions/{session_id}/steer", dependencies=[Depends(auth)])
    async def turn_steer(session_id: str, body: TurnBody, request: Request) -> Response:
        return await submit_turn(session_id, body, request, steer=True)

    @app.get("/api/v1/turns/{turn_id}", dependencies=[Depends(auth)])
    async def turn_status(turn_id: str) -> Response:
        record = control._engine.turns.get(turn_id)
        if record is None:
            return _problem(404, "not_found", f"Turn `{turn_id}` is not retained.")
        return JSONResponse(
            {
                "turnId": record.turn_id,
                "requestId": record.request_id,
                "sessionId": record.conversation_id,
                "status": record.state.value,
                "detail": record.detail,
                "startedAt": record.started_at.isoformat(),
                "updatedAt": record.updated_at.isoformat(),
                "endedAt": record.ended_at.isoformat() if record.ended_at else None,
                "durationMs": record.duration_ms,
            }
        )

    @app.post("/api/v1/sessions/{session_id}/cancel", dependencies=[Depends(auth)])
    async def turn_cancel(session_id: str) -> Response:
        from nanocat.application.turns import TurnState

        async with _guard_session_route(control, session_id) as (target, route_error):
            if route_error is not None:
                return route_error
            route_key = f"web:{target.chat_id}"
            record = control._engine.turns.active_for_session(route_key)
            if record is None or record.conversation_id != session_id:
                return _problem(
                    409,
                    "no_active_turn",
                    "This session has no active turn to cancel.",
                )
            result = await control.execute(
                "turn.cancel_exact",
                {
                    "channel": "web",
                    "chat_id": target.chat_id,
                    "principal_id": "web:local",
                    "turn_id": record.turn_id,
                },
            )
        if result.get("ok"):
            turn_id = record.turn_id

            def publish_terminal() -> None:
                terminal = control._engine.turns.get(turn_id)
                failed = terminal is not None and terminal.state is TurnState.FAILED
                channel.schedule_feed_event(
                    "turn.failed" if failed else "turn.cancelled",
                    session_id=session_id,
                    turn_id=turn_id,
                    request_id=record.request_id,
                    status="failed" if failed else "cancelled",
                    summary="Stop failed" if failed else "Turn stopped",
                    node_id=f"stop:{turn_id}",
                    source="control",
                    output=(
                        {
                            "error": {
                                "code": "stop_failed",
                                "title": "Stop failed",
                                "message": terminal.detail or "Cancellation cleanup failed.",
                            }
                        }
                        if failed and terminal is not None
                        else {"control": {"kind": "stop", "phase": "completed"}}
                    ),
                    started_at=terminal.started_at if terminal is not None else None,
                    ended_at=terminal.ended_at if terminal is not None else None,
                    duration_ms=terminal.duration_ms if terminal is not None else None,
                )

            control._engine.turns.add_terminal_finalizer(turn_id, publish_terminal)
            await channel.publish_feed_event(
                "turn.cancelling",
                session_id=session_id,
                turn_id=turn_id,
                request_id=record.request_id,
                status="cancelling",
                summary="Stop requested",
                node_id=f"stop:{turn_id}",
                source="control",
                output={"control": {"kind": "stop", "phase": "requested"}},
                started_at=record.started_at,
            )
            return JSONResponse(
                status_code=202,
                content=_camelize(result.get("data") or {}),
                headers={"Location": f"/api/v1/turns/{turn_id}"},
            )
        return _control_response(result)

    @app.post("/api/v1/turns/{turn_id}/cancel", dependencies=[Depends(auth)])
    async def turn_cancel_by_id(turn_id: str) -> Response:
        from nanocat.application.turns import TurnState

        record = control._engine.turns.get(turn_id)
        if record is None or not record.conversation_id:
            return _problem(404, "not_found", f"Turn `{turn_id}` is not retained.")
        if record.state in {TurnState.COMPLETED, TurnState.CANCELLED, TurnState.FAILED}:
            return _problem(409, "turn_terminal", f"Turn `{turn_id}` is already terminal.")
        session_id = record.conversation_id
        async with _guard_session_route(control, session_id) as (target, route_error):
            if route_error is not None:
                return route_error
            current_record = control._engine.turns.get(turn_id)
            if current_record is None or current_record.conversation_id != session_id:
                return _problem(404, "not_found", f"Turn `{turn_id}` is not retained.")
            if current_record.state in {
                TurnState.COMPLETED,
                TurnState.CANCELLED,
                TurnState.FAILED,
            }:
                return _problem(
                    409,
                    "turn_terminal",
                    f"Turn `{turn_id}` is already terminal.",
                )
            record = current_record
            result = await control.execute(
                "turn.cancel_exact",
                {
                    "channel": "web",
                    "chat_id": target.chat_id,
                    "principal_id": "web:local",
                    "turn_id": turn_id,
                },
            )
        if result.get("ok"):
            def publish_terminal() -> None:
                terminal = control._engine.turns.get(turn_id)
                failed = terminal is not None and terminal.state is TurnState.FAILED
                channel.schedule_feed_event(
                    "turn.failed" if failed else "turn.cancelled",
                    session_id=session_id,
                    turn_id=turn_id,
                    request_id=record.request_id,
                    status="failed" if failed else "cancelled",
                    summary="Stop failed" if failed else "Turn stopped",
                    node_id=f"stop:{turn_id}",
                    source="control",
                    output=(
                        {
                            "error": {
                                "code": "stop_failed",
                                "title": "Stop failed",
                                "message": terminal.detail or "Cancellation cleanup failed.",
                            }
                        }
                        if failed and terminal is not None
                        else {"control": {"kind": "stop", "phase": "completed"}}
                    ),
                    started_at=terminal.started_at if terminal is not None else None,
                    ended_at=terminal.ended_at if terminal is not None else None,
                    duration_ms=terminal.duration_ms if terminal is not None else None,
                )

            control._engine.turns.add_terminal_finalizer(
                turn_id,
                publish_terminal,
            )
            await channel.publish_feed_event(
                "turn.cancelling",
                session_id=session_id,
                turn_id=turn_id,
                request_id=record.request_id,
                status="cancelling",
                summary="Stop requested",
                node_id=f"stop:{turn_id}",
                source="control",
                output={"control": {"kind": "stop", "phase": "requested"}},
                started_at=record.started_at,
            )
            return JSONResponse(
                status_code=202,
                content=_camelize(result.get("data") or {}),
                headers={"Location": f"/api/v1/turns/{turn_id}"},
            )
        return _control_response(result)

    @app.get("/api/v1/sessions/{session_id}/approvals", dependencies=[Depends(auth)])
    async def approvals(session_id: str) -> Response:
        async with _guard_session_route(control, session_id) as (target, route_error):
            if route_error is not None:
                return route_error
            result = await control.execute(
                "approvals.pending",
                {
                    "channel": "web",
                    "chat_id": target.chat_id,
                    "principal_id": "web:local",
                },
            )
        return _control_response(result)

    @app.get("/api/v1/approvals", dependencies=[Depends(auth)])
    async def approvals_global(
        session_id: str | None = Query(default=None, alias="sessionId")
    ) -> Response:
        params: dict[str, Any] = {
            "channel": "web",
            "principal_id": "web:local",
        }
        if session_id:
            target = control._sessions.get_session("web", session_id)
            if target is None:
                return _problem(404, "not_found", f"Session `{session_id}` not found.")
            params["chat_id"] = target.chat_id
        return _control_response(await control.execute("approvals.pending", params))

    @app.post("/api/v1/sessions/{session_id}/approvals/respond", dependencies=[Depends(auth)])
    async def approval_respond(
        session_id: str, body: ApprovalBody, request: Request
    ) -> Response:
        async def respond() -> Response:
            async with _guard_session_route(control, session_id) as (
                target,
                route_error,
            ):
                if route_error is not None:
                    return route_error
                result = await control.execute(
                    "approval.respond",
                    {
                        "channel": "web",
                        "chat_id": target.chat_id,
                        "principal_id": "web:local",
                        "request_id": body.request_id,
                        "approval_action": body.action,
                    },
                )
                return _control_response(result)

        fingerprint = hashlib.sha256(
            f"session:{session_id}:{body.request_id}:{body.action}".encode()
        ).hexdigest()
        return await idempotency.execute(
            request.headers.get("idempotency-key"), fingerprint, respond
        )

    @app.post("/api/v1/approvals/{request_id}", dependencies=[Depends(auth)])
    async def approval_respond_global(
        request_id: str, body: ApprovalDecisionBody, request: Request
    ) -> Response:
        async def respond() -> Response:
            pending = await control.execute(
                "approvals.pending", {"channel": "web", "principal_id": "web:local"}
            )
            if not pending.get("ok"):
                return _control_response(pending)
            match = next(
                (
                    item
                    for item in pending.get("data", {}).get("approvals", [])
                    if str(item.get("request_id") or "") == request_id
                ),
                None,
            )
            if match is None or not match.get("session_id"):
                return _problem(404, "not_found", "Approval request is no longer pending.")
            target = control._sessions.get_session("web", str(match["session_id"]))
            if target is None:
                return _problem(404, "not_found", "Approval session is unavailable.")
            async with _guard_session_route(control, target.id) as (
                current,
                route_error,
            ):
                if route_error is not None:
                    return route_error
                return _control_response(
                    await control.execute(
                        "approval.respond",
                        {
                            "channel": "web",
                            "chat_id": current.chat_id,
                            "principal_id": "web:local",
                            "request_id": request_id,
                            "approval_action": body.action,
                        },
                    )
                )

        fingerprint = hashlib.sha256(
            f"global:{request_id}:{body.action}".encode()
        ).hexdigest()
        return await idempotency.execute(
            request.headers.get("idempotency-key"), fingerprint, respond
        )

    @app.get("/api/v1/models", dependencies=[Depends(auth)])
    async def models() -> Response:
        return _control_response(await control.execute("models.state", {}))

    @app.post("/api/v1/models/select", dependencies=[Depends(auth)])
    async def model_select(body: ModelSelectBody) -> Response:
        return _control_response(
            await control.execute("model.select", {"slot": body.slot, "model": body.model})
        )

    @app.post("/api/v1/models/effort", dependencies=[Depends(auth)])
    async def model_effort(body: EffortBody) -> Response:
        return _control_response(
            await control.execute("model.set_effort", {"value": body.value})
        )

    @app.post("/api/v1/agent/pulse", dependencies=[Depends(auth)])
    async def agent_pulse(body: PulseBody) -> Response:
        return _control_response(
            await control.execute("agent.set_pulse", {"enabled": body.enabled})
        )

    @app.post("/api/v1/models/catalog", dependencies=[Depends(auth)])
    async def model_catalog_add(body: ModelCatalogBody) -> Response:
        return _control_response(
            await control.execute(
                "model.add",
                {"provider": body.provider, "model": body.model},
            )
        )

    @app.delete("/api/v1/models/catalog/{model_id:path}", dependencies=[Depends(auth)])
    async def model_catalog_remove(model_id: str) -> Response:
        return _control_response(
            await control.execute("model.remove", {"model": model_id})
        )

    @app.get("/api/v1/sessions/{session_id}/compact", dependencies=[Depends(auth)])
    async def compact_status(session_id: str) -> Response:
        async with _guard_session_route(control, session_id) as (target, route_error):
            if route_error is not None:
                return route_error
            return _control_response(
                await control.execute(
                    "compact.status",
                    {
                        "channel": "web",
                        "chat_id": target.chat_id,
                        "session_id": session_id,
                    },
                )
            )

    @app.post("/api/v1/sessions/{session_id}/compact", dependencies=[Depends(auth)])
    async def compact_run(session_id: str) -> Response:
        async with _guard_session_route(control, session_id) as (target, route_error):
            if route_error is not None:
                return route_error
            target, selection_error = await _select_session(control, session_id)
            if selection_error is not None:
                return selection_error
            return _control_response(
                await control.execute(
                    "compact.run", {"channel": "web", "chat_id": target.chat_id}
                )
            )

    @app.get("/api/v1/commands", dependencies=[Depends(auth)])
    async def command_catalog() -> Response:
        return _control_response(await control.execute("commands.catalog", {}))

    @app.post("/api/v1/commands/execute", dependencies=[Depends(auth)])
    async def command_execute(body: CommandBody) -> Response:
        chat_id = "api"
        if body.session_id:
            target = control._sessions.get_session("web", body.session_id)
            if target is None:
                return _problem(404, "not_found", f"Session `{body.session_id}` not found.")
            async with _guard_session_route(control, body.session_id) as (
                current,
                route_error,
            ):
                if route_error is not None:
                    return route_error
                current, selection_error = await _select_session(
                    control,
                    body.session_id,
                )
                if selection_error is not None:
                    return selection_error
                chat_id = current.chat_id
                result = await control.execute(
                    "command.execute",
                    {
                        "channel": "web",
                        "chat_id": chat_id,
                        "principal_id": "web:local",
                        "text": body.text,
                    },
                )
        else:
            async with control.routing_guard(f"web:{chat_id}"):
                result = await control.execute(
                    "command.execute",
                    {
                        "channel": "web",
                        "chat_id": chat_id,
                        "principal_id": "web:local",
                        "text": body.text,
                    },
                )
        return _control_response(result)

    @app.get("/api/v1/logs", dependencies=[Depends(auth)])
    async def logs(count: int = 100) -> Response:
        return _control_response(
            await control.execute("logs.tail", {"count": min(max(count, 1), 200)})
        )

    @app.post("/api/v1/media", dependencies=[Depends(auth)], status_code=201)
    async def upload_media(file: UploadFile = File(...)) -> Response:
        if artifact_registry is None:
            return _problem(503, "artifact_unavailable", "Artifact storage is unavailable.")
        try:
            entry = await artifact_registry.upload(file)
        except ValueError as exc:
            return _problem(413, "media_too_large", str(exc))
        except RuntimeError:
            response = _problem(
                503,
                "media_capacity",
                "Media storage capacity is temporarily unavailable.",
            )
            response.headers["Retry-After"] = "1"
            return response
        except OSError:
            return _problem(507, "media_storage_failed", "Media storage is unavailable.")
        return JSONResponse(status_code=201, content=entry.public())

    @app.get("/api/v1/artifacts/{artifact_id}/metadata", dependencies=[Depends(auth)])
    async def artifact_metadata(artifact_id: str) -> Response:
        resolved = (
            await artifact_registry.resolve_async(artifact_id)
            if artifact_registry is not None
            else None
        )
        if resolved is None:
            return _problem(404, "artifact_not_found", "Artifact not found or expired.")
        return JSONResponse(resolved[0].public())

    @app.get("/api/v1/artifacts/{artifact_id}", dependencies=[Depends(auth)])
    async def artifact_content(
        artifact_id: str, request: Request, download: bool = False
    ) -> Response:
        opened = (
            await artifact_registry.open_for_read_async(artifact_id)
            if artifact_registry is not None
            else None
        )
        if opened is None:
            return _problem(404, "artifact_not_found", "Artifact not found or expired.")
        entry, handle, lease_id = opened
        active_content = entry.media_type in {"image/svg+xml", "text/html", "application/xhtml+xml"}
        disposition = "attachment" if download or active_content else "inline"
        filename = quote(entry.name, safe="")
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{filename}",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox",
        }
        range_header = request.headers.get("range")
        download_closed = False

        async def close_download() -> None:
            nonlocal download_closed
            if download_closed:
                return
            download_closed = True
            await artifact_registry.close_reader(handle, lease_id)

        if range_header:
            selected = parse_range(range_header, entry.size)
            if selected is None:
                await artifact_registry.close_reader(handle, lease_id)
                response = _problem(416, "invalid_range", "Requested byte range is invalid.")
                response.headers["Content-Range"] = f"bytes */{entry.size}"
                return response
            start, end = selected
            headers["Content-Range"] = f"bytes {start}-{end}/{entry.size}"
            headers["Content-Length"] = str(end - start + 1)
            return _ClosingStreamingResponse(
                read_range(
                    handle,
                    start,
                    end,
                    on_close=lambda: artifact_registry.release_lease(lease_id),
                ),
                status_code=206,
                media_type=entry.media_type,
                headers=headers,
                close_stream=close_download,
            )
        headers["Content-Length"] = str(entry.size)
        end = max(0, entry.size - 1)
        return _ClosingStreamingResponse(
            read_range(
                handle,
                0,
                end,
                on_close=lambda: artifact_registry.release_lease(lease_id),
            ),
            media_type=entry.media_type,
            headers=headers,
            close_stream=close_download,
        )

    def settings_payload(snapshot: Mapping[str, Any]) -> dict[str, Any]:
        stored = snapshot["config"]
        raw = stored.model_dump(by_alias=True, mode="json")
        return {
            "settings": _redact(raw),
            "fieldSchema": _setting_field_schema(raw),
            "revision": snapshot["revision"],
            "writable": True,
            "readOnlyPaths": snapshot.get("readOnlyPaths", []),
            "effectiveOverrides": snapshot.get("effectiveOverrides", {}),
            "restartRequiredPaths": restart_setting_paths(),
        }

    @app.get("/api/v1/settings", dependencies=[Depends(auth)])
    async def settings() -> Response:
        try:
            snapshot = await configuration.snapshot()
        except ConfigurationError as exc:
            return _problem(503, "configuration_unavailable", str(exc))
        response = JSONResponse(settings_payload(snapshot))
        response.headers["ETag"] = f'"{snapshot["revision"]}"'
        return response

    @app.patch("/api/v1/settings", dependencies=[Depends(auth)])
    async def settings_update(body: SettingsUpdateBody, request: Request) -> Response:
        encoded_size = len(
            json.dumps(body.values, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        if encoded_size > _MAX_SETTINGS_UPDATE_BYTES:
            return _problem(
                413,
                "configuration_too_large",
                "The configuration update exceeds the request limit.",
            )
        revision_header = request.headers.get("if-match", "").strip()
        if not revision_header:
            return _problem(
                428,
                "precondition_required",
                "If-Match is required for configuration updates.",
            )
        normalized_revision = revision_header.removeprefix("W/").strip().strip('"')
        try:
            expected_revision = int(normalized_revision)
        except ValueError:
            return _problem(400, "invalid_revision", "If-Match must contain a revision number.")
        try:
            updated = await configuration.update(
                body.values,
                expected_revision=expected_revision,
            )
        except ConfigurationConflictError as exc:
            return _problem(412, "configuration_conflict", str(exc))
        except ConfigurationValidationError as exc:
            return _problem(422, "configuration_invalid", str(exc))
        except ConfigurationError as exc:
            return _problem(503, "configuration_unavailable", str(exc))
        response = JSONResponse(settings_payload(updated) | {
            "changedPaths": updated["changedPaths"],
            "restartRequired": updated["restartRequired"],
            "appliedPaths": updated["appliedPaths"],
            "nextTurnPaths": updated["nextTurnPaths"],
            "reconnectedPaths": updated["reconnectedPaths"],
            "restartRequiredPaths": updated["restartRequiredPaths"],
        })
        response.headers["ETag"] = f'"{updated["revision"]}"'
        return response

    async def open_event_stream(
        request: Request, *, session_id: str | None = None
    ) -> Response:
        try:
            stream = await broker.open_subscription(
                last_event_id=request.headers.get("last-event-id"),
                session_id=session_id,
            )
        except SseCapacityError:
            response = _problem(
                503,
                "stream_capacity",
                "The event stream subscriber limit has been reached.",
            )
            response.headers["Retry-After"] = "5"
            return response
        except RuntimeError:
            return _problem(503, "stream_unavailable", "The event stream is unavailable.")
        return _ClosingStreamingResponse(
            stream,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            close_stream=stream.aclose,
        )

    @app.get("/api/v1/events", dependencies=[Depends(auth)])
    async def events(request: Request) -> Response:
        return await open_event_stream(request)

    @app.get("/api/v1/sessions/{session_id}/events", dependencies=[Depends(auth)])
    async def session_events(session_id: str, request: Request) -> Response:
        target = await asyncio.to_thread(
            control._sessions.get_session, "web", session_id
        )
        if target is None:
            return _problem(404, "not_found", f"Session `{session_id}` not found.")
        return await open_event_stream(request, session_id=session_id)

    app.add_middleware(
        _RequestBodyLimitMiddleware,
        default_limit=_MAX_JSON_BODY_BYTES,
        path_limits={"/api/v1/media": media_request_limit},
    )
    return app


def resolve_static_dir(static_dir: str | Path | None = None) -> Path | None:
    """Resolve development or packaged Web assets without assuming a source checkout."""
    candidates = []
    if static_dir is not None:
        candidates.append(Path(static_dir))
    package_root = Path(__file__).resolve().parents[1]
    candidates.extend((package_root.parent / "frontend" / "dist", package_root / "web" / "static"))
    return next((path for path in candidates if (path / "index.html").is_file()), None)


def create_web_app(
    *,
    core_api_url: str,
    api_token: str | Callable[[], str],
    auth: WebSessionAuth,
    static_dir: str | Path | None = None,
) -> FastAPI:
    """Create the browser-facing static server and authenticated API proxy."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.proxy_client = httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=5.0, write=30.0, pool=5.0),
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=0),
            follow_redirects=False,
            trust_env=False,
        )
        try:
            yield
        finally:
            await app.state.proxy_client.aclose()

    app = FastAPI(title="NanoCat Web", docs_url=None, redoc_url=None, lifespan=lifespan)
    assets = resolve_static_dir(static_dir)
    media_gate = asyncio.Semaphore(_MAX_CONCURRENT_MEDIA_REQUESTS)

    @app.middleware("http")
    async def guard_media_upload(
        request: Request, call_next: Callable[..., Awaitable[Response]]
    ):
        if request.method == "POST" and request.url.path == "/api/v1/media":
            length_error = _media_length_error(
                request,
                _DEFAULT_MEDIA_FILE_BYTES + _MULTIPART_OVERHEAD_BYTES,
            )
            if length_error is not None:
                return length_error
            if media_gate.locked():
                response = _problem(
                    503,
                    "media_capacity",
                    "Too many media uploads are already in progress.",
                )
                response.headers["Retry-After"] = "1"
                return response
            await media_gate.acquire()
            try:
                return await call_next(request)
            finally:
                media_gate.release()
        return await call_next(request)

    @app.middleware("http")
    async def guard_web_surface(
        request: Request, call_next: Callable[..., Awaitable[Response]]
    ) -> Response:
        path = request.url.path
        if path.startswith("/api/"):
            try:
                await auth.require(
                    request,
                    mutation=request.method not in {"GET", "HEAD", "OPTIONS"},
                )
            except HTTPException as exc:
                response = _problem(
                    exc.status_code,
                    "http_error",
                    str(exc.detail or "Authentication required"),
                )
                response.headers.update(exc.headers or {})
                return response
        response = await call_next(request)
        if path.startswith("/api/") or path.startswith("/auth/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.exception_handler(HTTPException)
    async def handle_http_exception(_request: Request, exc: HTTPException) -> Response:
        response = _problem(exc.status_code, "http_error", str(exc.detail or "Request failed"))
        response.headers.update(exc.headers or {})
        return response

    @app.get("/auth/status")
    async def auth_status(request: Request) -> dict[str, Any]:
        authenticated = not auth.protected
        if auth.protected:
            try:
                await auth.require(request)
                authenticated = True
            except HTTPException:
                authenticated = False
        return {"protected": auth.protected, "authenticated": authenticated}

    @app.post("/auth/login")
    async def login(request: Request, body: LoginBody) -> Response:
        if not auth.protected:
            return JSONResponse({"authenticated": True, "protected": False})
        cookie_response = Response()
        payload = await auth.login(request, cookie_response, body.password)
        response = JSONResponse({**payload, "protected": True})
        for header, value in cookie_response.raw_headers:
            if header.lower() == b"set-cookie":
                response.raw_headers.append((header, value))
        return response

    @app.post("/auth/logout")
    async def logout(request: Request) -> Response:
        await auth.require(request, mutation=True)
        response = JSONResponse({"authenticated": False})
        await auth.logout(request, response)
        return response

    @app.api_route(
        "/api/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    )
    async def proxy_api(path: str, request: Request) -> Response:
        await auth.require(request, mutation=request.method not in {"GET", "HEAD", "OPTIONS"})
        client: httpx.AsyncClient = request.app.state.proxy_client
        upstream = f"{core_api_url.rstrip('/')}/api/{path}"
        if request.url.query:
            upstream = f"{upstream}?{request.url.query}"
        forwarded_headers = {
            key: value
            for key, value in request.headers.items()
            if key.casefold()
            in {
                "accept",
                "content-length",
                "content-type",
                "idempotency-key",
                "if-match",
                "last-event-id",
                "range",
            }
        }
        current_api_token = api_token() if callable(api_token) else api_token
        forwarded_headers["authorization"] = f"Bearer {current_api_token}"
        content = None if request.method in {"GET", "HEAD"} else request.stream()
        upstream_request = client.build_request(
            request.method,
            upstream,
            headers=forwarded_headers,
            content=content,
        )
        if path == "v1/events" or re.fullmatch(r"v1/sessions/[^/]+/events", path):
            upstream_request.extensions["timeout"] = {
                "connect": 5.0,
                "read": None,
                "write": 30.0,
                "pool": 5.0,
            }
        try:
            upstream_response = await client.send(upstream_request, stream=True)
        except httpx.RequestError:
            return _problem(503, "core_api_unavailable", "NanoCat core API is unavailable.")
        except ClientDisconnect:
            return _problem(499, "client_disconnected", "The client disconnected.")
        response_headers = {
            key: value
            for key, value in upstream_response.headers.items()
            if key.casefold()
            in {
                "accept-ranges",
                "cache-control",
                "content-encoding",
                "content-length",
                "content-disposition",
                "content-range",
                "content-type",
                "etag",
                "last-modified",
                "location",
                "retry-after",
                "vary",
                "x-accel-buffering",
            }
        }
        streaming = (
            path == "v1/events"
            or re.fullmatch(r"v1/sessions/[^/]+/events", path) is not None
            or (
                request.method == "GET"
                and re.fullmatch(r"v1/artifacts/[^/]+", path) is not None
            )
        )
        if not streaming:
            try:
                body = await upstream_response.aread()
            finally:
                await upstream_response.aclose()
            return Response(
                content=body,
                status_code=upstream_response.status_code,
                headers=response_headers,
            )
        return _ClosingStreamingResponse(
            upstream_response.aiter_raw(),
            status_code=upstream_response.status_code,
            headers=response_headers,
            close_stream=upstream_response.aclose,
        )

    @app.get("/{path:path}")
    async def static_files(path: str) -> Response:
        if assets is None:
            return _problem(503, "web_assets_unavailable", "NanoCat Web assets are not built.")
        security_headers = {
            "Content-Security-Policy": (
                "default-src 'self'; connect-src 'self'; img-src 'self' data: blob:; "
                "style-src 'self' 'unsafe-inline'; script-src 'self'; object-src 'none'; "
                "base-uri 'none'; frame-ancestors 'none'"
            ),
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
        }
        requested = (assets / path).resolve() if path else assets / "index.html"
        root = assets.resolve()
        if requested.is_file() and (requested == root or root in requested.parents):
            headers = {
                **security_headers,
                "Cache-Control": (
                    "public, max-age=31536000, immutable"
                    if requested.parent.name == "assets"
                    else "no-store"
                ),
            }
            return FileResponse(requested, headers=headers)
        return FileResponse(
            root / "index.html", headers={**security_headers, "Cache-Control": "no-store"}
        )

    app.add_middleware(
        _RequestBodyLimitMiddleware,
        default_limit=_MAX_JSON_BODY_BYTES,
        path_limits={
            "/auth/login": _MAX_LOGIN_BODY_BYTES,
            "/api/v1/media": _DEFAULT_MEDIA_FILE_BYTES + _MULTIPART_OVERHEAD_BYTES,
        },
    )
    return app
