"""OpenAI Codex Responses Provider."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, AsyncGenerator

import httpx
from loguru import logger
from oauth_cli_kit import get_token as get_codex_token
from oauth_cli_kit.models import OAuthToken
from oauth_cli_kit.providers import OPENAI_CODEX_PROVIDER
from oauth_cli_kit.storage import FileTokenStorage

from nanocat.providers.base import LLMProvider, LLMResponse, ToolCallRequest

DEFAULT_CODEX_URL = "https://chatgpt.com/backend-api/codex/responses"
DEFAULT_ORIGINATOR = "nanocat"
DEFAULT_MODEL = "openai-codex/gpt-5.3-codex"
CODEX_AUTH_PORTS = (1455, 1457)
CODEX_AUTH_SCOPE = "openid profile email offline_access api.connectors.read api.connectors.invoke"
AUTH_PROMPT_PREFIX = "Codex login required"


@dataclass(frozen=True)
class _AuthorizationInput:
    code: str | None
    state: str | None = None
    error: str | None = None


@dataclass
class _CodexLoginFlow:
    verifier: str
    state: str
    redirect_uri: str
    auth_url: str
    server: asyncio.AbstractServer | None
    callback: asyncio.Future[_AuthorizationInput]


class OpenAICodexProvider(LLMProvider):
    """Use Codex OAuth to call the Responses API."""

    def __init__(self, default_model: str = DEFAULT_MODEL):
        super().__init__(api_key=None, api_base=None)
        self.default_model = default_model
        self._auth_lock = asyncio.Lock()
        self._login_flow: _CodexLoginFlow | None = None
        self._token_storage = FileTokenStorage(
            token_filename=OPENAI_CODEX_PROVIDER.token_filename
        )

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int | None = 4096,
        temperature: float | None = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> LLMResponse:
        async with self._auth_lock:
            token = await self._load_token()
            request_messages = _strip_auth_messages(messages)
            if token is None:
                login_result = await self._advance_login(messages)
                if isinstance(login_result, LLMResponse):
                    return login_result
                token, request_messages = login_result

        return await self._request_chat(
            request_messages,
            token,
            tools=tools,
            model=model,
            reasoning_effort=reasoning_effort,
            tool_choice=tool_choice,
        )

    async def aclose(self) -> None:
        """Close the provider-owned OAuth callback server and HTTP resources."""
        async with self._auth_lock:
            await self._close_login_flow()
        await super().aclose()

    async def _load_token(self) -> OAuthToken | None:
        try:
            token = await asyncio.to_thread(get_codex_token)
            if not token.account_id:
                logger.info("Codex OAuth credentials have no account identity")
                return None
            return token
        except Exception as exc:
            logger.info("Codex OAuth credentials unavailable: {}", type(exc).__name__)
            return None

    async def _advance_login(
        self,
        messages: list[dict[str, Any]],
    ) -> LLMResponse | tuple[OAuthToken, list[dict[str, Any]]]:
        flow = self._login_flow
        if flow is None:
            self._login_flow = await self._start_login_flow()
            return self._login_prompt(self._login_flow)

        authorization = await self._next_authorization(flow, messages)
        if authorization is None:
            return self._login_prompt(
                flow,
                "Complete the browser login, then reply `continue`. "
                "If the browser cannot return to NanoCat, paste the full callback URL "
                "or authorization code here.",
            )
        if authorization.state and authorization.state != flow.state:
            return self._login_prompt(
                flow,
                "The callback state did not match this login attempt. "
                "Use the current login URL and try again.",
            )
        if authorization.error:
            logger.warning("Codex OAuth authorization was rejected: {}", authorization.error)
            await self._close_login_flow()
            self._login_flow = await self._start_login_flow()
            return self._login_prompt(
                self._login_flow,
                "The previous login attempt was rejected. A new login flow is ready.",
            )
        if not authorization.code:
            return self._login_prompt(flow, "No authorization code was received.")

        try:
            token = await _exchange_codex_code(
                authorization.code,
                flow.verifier,
                flow.redirect_uri,
            )
            await asyncio.to_thread(self._token_storage.save, token)
        except Exception as exc:
            logger.warning("Codex OAuth token exchange failed: {}", type(exc).__name__)
            await self._close_login_flow()
            self._login_flow = await self._start_login_flow()
            return self._login_prompt(
                self._login_flow,
                "The authorization code could not be exchanged. "
                "A new login flow is ready; please try again.",
            )

        await self._close_login_flow()
        return token, _strip_auth_messages(messages)

    async def _start_login_flow(self) -> _CodexLoginFlow:
        verifier = _base64url(secrets.token_bytes(32))
        challenge = _base64url(hashlib.sha256(verifier.encode("utf-8")).digest())
        state = _base64url(secrets.token_bytes(32))
        server: asyncio.AbstractServer | None = None
        port = CODEX_AUTH_PORTS[0]
        for candidate in CODEX_AUTH_PORTS:
            try:
                server = await asyncio.start_server(self._handle_callback, "127.0.0.1", candidate)
                port = candidate
                break
            except OSError:
                continue

        redirect_uri = f"http://localhost:{port}/auth/callback"
        provider = OPENAI_CODEX_PROVIDER
        query = urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": provider.client_id,
                "redirect_uri": redirect_uri,
                "scope": CODEX_AUTH_SCOPE,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "id_token_add_organizations": "true",
                "codex_cli_simplified_flow": "true",
                "state": state,
                "originator": DEFAULT_ORIGINATOR,
            }
        )
        loop = asyncio.get_running_loop()
        return _CodexLoginFlow(
            verifier=verifier,
            state=state,
            redirect_uri=redirect_uri,
            auth_url=f"{provider.authorize_url}?{query}",
            server=server,
            callback=loop.create_future(),
        )

    async def _handle_callback(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        status = "200 OK"
        body = "Login received. Return to NanoCat."
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
            request_line = raw.splitlines()[0].decode("ascii", "ignore")
            _method, target, _version = request_line.split(" ", 2)
            parsed = urllib.parse.urlsplit(target)
            flow = self._login_flow
            params = urllib.parse.parse_qs(parsed.query)
            state = params.get("state", [None])[0]
            if parsed.path != "/auth/callback" or flow is None:
                status, body = "404 Not Found", "No active NanoCat login."
            elif state != flow.state:
                status, body = "400 Bad Request", "Login state mismatch."
            elif params.get("error", [None])[0]:
                flow.callback.set_result(
                    _AuthorizationInput(
                        code=None,
                        state=state,
                        error=params["error"][0],
                    )
                )
                body = "Login was not completed. Return to NanoCat."
            elif params.get("code", [None])[0]:
                flow.callback.set_result(
                    _AuthorizationInput(
                        code=params["code"][0],
                        state=state,
                    )
                )
            else:
                status, body = "400 Bad Request", "Authorization code is missing."
        except Exception:
            status, body = "400 Bad Request", "Invalid login callback."
        finally:
            payload = body.encode("utf-8")
            response_headers = (
                f"HTTP/1.1 {status}\r\n"
                "Content-Type: text/plain; charset=utf-8\r\n"
                f"Content-Length: {len(payload)}\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii")
            writer.write(response_headers + payload)
            try:
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

    async def _next_authorization(
        self,
        flow: _CodexLoginFlow,
        messages: list[dict[str, Any]],
    ) -> _AuthorizationInput | None:
        if flow.callback.done():
            return flow.callback.result()
        text = _last_user_text(messages)
        return _parse_authorization_input(text)

    async def _close_login_flow(self) -> None:
        flow = self._login_flow
        self._login_flow = None
        if flow is None or flow.server is None:
            return
        flow.server.close()
        await flow.server.wait_closed()

    @staticmethod
    def _login_prompt(flow: _CodexLoginFlow, detail: str = "") -> LLMResponse:
        lines = [AUTH_PROMPT_PREFIX, "", "Open this URL in a browser:", flow.auth_url]
        if detail:
            lines.extend(["", detail])
        lines.extend(
            [
                "",
                "After signing in, reply `continue`. Do not send your password or refresh token.",
            ]
        )
        return LLMResponse(content="\n".join(lines), finish_reason="stop")

    async def _request_chat(
        self,
        messages: list[dict[str, Any]],
        token: OAuthToken,
        *,
        tools: list[dict[str, Any]] | None,
        model: str | None,
        reasoning_effort: str | None,
        tool_choice: str | dict[str, Any] | None,
    ) -> LLMResponse:
        model_name = model or self.default_model
        model_name = model_name.split("/")[-1] if "/" in model_name else model_name
        system_prompt, input_items = _convert_messages(messages)
        if not token.account_id:
            return LLMResponse(
                content="Codex login succeeded, but the account identity was missing. "
                "Please sign in again.",
                finish_reason="error",
            )

        headers = _build_headers(token.account_id, token.access)
        body: dict[str, Any] = {
            "model": model_name,
            "store": False,
            "stream": True,
            "instructions": system_prompt,
            "input": input_items,
            "text": {"verbosity": "medium"},
            "include": ["reasoning.encrypted_content"],
            "prompt_cache_key": _prompt_cache_key(messages),
            "tool_choice": tool_choice or "auto",
            "parallel_tool_calls": True,
        }
        if reasoning_effort:
            body["reasoning"] = {"effort": reasoning_effort}
        if tools:
            body["tools"] = _convert_tools(tools)

        try:
            try:
                content, tool_calls, finish_reason = await _request_codex(
                    DEFAULT_CODEX_URL, headers, body, verify=True
                )
            except Exception as exc:
                if "CERTIFICATE_VERIFY_FAILED" not in str(exc):
                    raise
                logger.warning(
                    "SSL certificate verification failed for Codex API; retrying with verify=False"
                )
                content, tool_calls, finish_reason = await _request_codex(
                    DEFAULT_CODEX_URL, headers, body, verify=False
                )
            return LLMResponse(
                content=content,
                tool_calls=tool_calls,
                finish_reason=finish_reason,
            )
        except Exception as exc:
            return LLMResponse(
                content=f"Error calling Codex: {exc}",
                finish_reason="error",
            )

    def get_default_model(self) -> str:
        return self.default_model




def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


async def _exchange_codex_code(
    code: str,
    verifier: str,
    redirect_uri: str,
) -> OAuthToken:
    provider = OPENAI_CODEX_PROVIDER
    payload = {
        "grant_type": "authorization_code",
        "client_id": provider.client_id,
        "code": code,
        "code_verifier": verifier,
        "redirect_uri": redirect_uri,
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            provider.token_url,
            data=payload,
            headers={"Accept": "application/json"},
        )
    if response.status_code != 200:
        raise RuntimeError(f"OAuth token exchange returned HTTP {response.status_code}")

    token_data = response.json()
    access = token_data.get("access_token")
    refresh = token_data.get("refresh_token")
    if not isinstance(access, str) or not access:
        raise RuntimeError("OAuth token response did not contain an access token")
    if not isinstance(refresh, str) or not refresh:
        raise RuntimeError("OAuth token response did not contain a refresh token")

    expires_in = token_data.get("expires_in", 3600)
    try:
        expires = int(time.time() * 1000) + int(expires_in) * 1000
    except (TypeError, ValueError) as exc:
        raise RuntimeError("OAuth token response contained an invalid expiry") from exc

    account_id = _decode_account_id(token_data.get("id_token"))
    if not account_id:
        account_id = _decode_account_id(access)
    if not account_id:
        raise RuntimeError("OAuth token response did not contain a Codex account identity")
    return OAuthToken(
        access=access,
        refresh=refresh,
        expires=expires,
        account_id=account_id,
    )


def _decode_account_id(token: Any) -> str | None:
    if not isinstance(token, str) or not token:
        return None
    claims = _decode_jwt_payload(token)
    if not claims:
        return None
    auth_claims = claims.get("https://api.openai.com/auth")
    if isinstance(auth_claims, dict):
        account_id = auth_claims.get("chatgpt_account_id")
        if isinstance(account_id, str) and account_id:
            return account_id
    for key in ("chatgpt_account_id", "account_id"):
        account_id = claims.get(key)
        if isinstance(account_id, str) and account_id:
            return account_id
    return None


def _decode_jwt_payload(token: str) -> dict[str, Any] | None:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        padding = "=" * (-len(parts[1]) % 4)
        payload = base64.urlsafe_b64decode(parts[1] + padding)
        decoded = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _last_user_text(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            text_parts = [
                item.get("text", "")
                for item in content
                if isinstance(item, dict) and isinstance(item.get("text"), str)
            ]
            return "\n".join(text_parts)
    return ""


def _parse_authorization_input(text: str) -> _AuthorizationInput | None:
    value = text.strip()
    if not value:
        return None
    lowered = value.casefold()
    if lowered in {"continue", "继续", "done", "已登录", "完成", "继续登录"}:
        return None

    candidate = value
    if not candidate.startswith(("http://", "https://")) and "=" in candidate:
        candidate = "http://localhost/" + candidate.lstrip("?")
    if candidate.startswith(("http://", "https://")):
        parsed = urllib.parse.urlsplit(candidate)
        params = urllib.parse.parse_qs(parsed.query)
        error = params.get("error", [None])[0]
        code = params.get("code", [None])[0]
        state = params.get("state", [None])[0]
        if error or code or state:
            return _AuthorizationInput(code=code, state=state, error=error)
        return None

    if " " not in value and len(value) >= 20:
        return _AuthorizationInput(code=value)
    return None


def _is_login_input(text: str) -> bool:
    value = text.strip()
    if not value:
        return False
    if value.casefold() in {"continue", "继续", "done", "已登录", "完成", "继续登录"}:
        return True
    return _parse_authorization_input(value) is not None


def _strip_auth_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    remove_login_reply = False
    for message in messages:
        role = message.get("role")
        content = message.get("content")
        if role == "assistant" and isinstance(content, str):
            if content.startswith(AUTH_PROMPT_PREFIX):
                remove_login_reply = True
                continue
        if remove_login_reply and role == "user":
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text = "\n".join(
                    item.get("text", "")
                    for item in content
                    if isinstance(item, dict) and isinstance(item.get("text"), str)
                )
            else:
                text = ""
            if _is_login_input(text):
                remove_login_reply = False
                continue
        remove_login_reply = False
        cleaned.append(message)
    return cleaned


def _build_headers(account_id: str, token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "chatgpt-account-id": account_id,
        "OpenAI-Beta": "responses=experimental",
        "originator": DEFAULT_ORIGINATOR,
        "User-Agent": "nanocat (python)",
        "accept": "text/event-stream",
        "content-type": "application/json",
    }


async def _request_codex(
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    verify: bool,
) -> tuple[str, list[ToolCallRequest], str]:
    async with httpx.AsyncClient(timeout=60.0, verify=verify) as client:
        async with client.stream("POST", url, headers=headers, json=body) as response:
            if response.status_code != 200:
                text = await response.aread()
                raise RuntimeError(
                    _friendly_error(response.status_code, text.decode("utf-8", "ignore"))
                )
            return await _consume_sse(response)


def _convert_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert OpenAI function-calling schema to Codex flat format."""
    converted: list[dict[str, Any]] = []
    for tool in tools:
        fn = (tool.get("function") or {}) if tool.get("type") == "function" else tool
        name = fn.get("name")
        if not name:
            continue
        params = fn.get("parameters") or {}
        converted.append(
            {
                "type": "function",
                "name": name,
                "description": fn.get("description") or "",
                "parameters": params if isinstance(params, dict) else {},
            }
        )
    return converted


def _convert_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    system_prompt = ""
    input_items: list[dict[str, Any]] = []

    for idx, msg in enumerate(messages):
        role = msg.get("role")
        content = msg.get("content")

        if role == "system":
            system_prompt = content if isinstance(content, str) else ""
            continue

        if role == "user":
            input_items.append(_convert_user_message(content))
            continue

        if role == "assistant":
            # Handle text first.
            if isinstance(content, str) and content:
                input_items.append(
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": content}],
                        "status": "completed",
                        "id": f"msg_{idx}",
                    }
                )
            # Then handle tool calls.
            for tool_call in msg.get("tool_calls", []) or []:
                fn = tool_call.get("function") or {}
                call_id, item_id = _split_tool_call_id(tool_call.get("id"))
                call_id = call_id or f"call_{idx}"
                item_id = item_id or f"fc_{idx}"
                input_items.append(
                    {
                        "type": "function_call",
                        "id": item_id,
                        "call_id": call_id,
                        "name": fn.get("name"),
                        "arguments": fn.get("arguments") or "{}",
                    }
                )
            continue

        if role == "tool":
            call_id, _ = _split_tool_call_id(msg.get("tool_call_id"))
            output_text = (
                content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            )
            input_items.append(
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": output_text,
                }
            )
            continue

    return system_prompt, input_items


def _convert_user_message(content: Any) -> dict[str, Any]:
    if isinstance(content, str):
        return {"role": "user", "content": [{"type": "input_text", "text": content}]}
    if isinstance(content, list):
        converted: list[dict[str, Any]] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                converted.append({"type": "input_text", "text": item.get("text", "")})
            elif item.get("type") == "image_url":
                url = (item.get("image_url") or {}).get("url")
                if url:
                    converted.append({"type": "input_image", "image_url": url, "detail": "auto"})
        if converted:
            return {"role": "user", "content": converted}
    return {"role": "user", "content": [{"type": "input_text", "text": ""}]}


def _split_tool_call_id(tool_call_id: Any) -> tuple[str, str | None]:
    if isinstance(tool_call_id, str) and tool_call_id:
        if "|" in tool_call_id:
            call_id, item_id = tool_call_id.split("|", 1)
            return call_id, item_id or None
        return tool_call_id, None
    return "call_0", None


def _prompt_cache_key(messages: list[dict[str, Any]]) -> str:
    raw = json.dumps(messages, ensure_ascii=True, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def _iter_sse(response: httpx.Response) -> AsyncGenerator[dict[str, Any], None]:
    buffer: list[str] = []
    async for line in response.aiter_lines():
        if line == "":
            if buffer:
                data_lines = [line[5:].strip() for line in buffer if line.startswith("data:")]
                buffer = []
                if not data_lines:
                    continue
                data = "\n".join(data_lines).strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    yield json.loads(data)
                except Exception:
                    continue
            continue
        buffer.append(line)


async def _consume_sse(response: httpx.Response) -> tuple[str, list[ToolCallRequest], str]:
    content = ""
    tool_calls: list[ToolCallRequest] = []
    tool_call_buffers: dict[str, dict[str, Any]] = {}
    finish_reason = "stop"

    async for event in _iter_sse(response):
        event_type = event.get("type")
        if event_type == "response.output_item.added":
            item = event.get("item") or {}
            if item.get("type") == "function_call":
                call_id = item.get("call_id")
                if not call_id:
                    continue
                tool_call_buffers[call_id] = {
                    "id": item.get("id") or "fc_0",
                    "name": item.get("name"),
                    "arguments": item.get("arguments") or "",
                }
        elif event_type == "response.output_text.delta":
            content += event.get("delta") or ""
        elif event_type == "response.function_call_arguments.delta":
            call_id = event.get("call_id")
            if call_id and call_id in tool_call_buffers:
                tool_call_buffers[call_id]["arguments"] += event.get("delta") or ""
        elif event_type == "response.function_call_arguments.done":
            call_id = event.get("call_id")
            if call_id and call_id in tool_call_buffers:
                tool_call_buffers[call_id]["arguments"] = event.get("arguments") or ""
        elif event_type == "response.output_item.done":
            item = event.get("item") or {}
            if item.get("type") == "function_call":
                call_id = item.get("call_id")
                if not call_id:
                    continue
                buf = tool_call_buffers.get(call_id) or {}
                args_raw = buf.get("arguments") or item.get("arguments") or "{}"
                try:
                    args = json.loads(args_raw)
                except Exception:
                    args = {"raw": args_raw}
                tool_calls.append(
                    ToolCallRequest(
                        id=f"{call_id}|{buf.get('id') or item.get('id') or 'fc_0'}",
                        name=buf.get("name") or item.get("name"),
                        arguments=args,
                    )
                )
        elif event_type == "response.completed":
            status = (event.get("response") or {}).get("status")
            finish_reason = _map_finish_reason(status)
        elif event_type in {"error", "response.failed"}:
            raise RuntimeError("Codex response failed")

    return content, tool_calls, finish_reason


_FINISH_REASON_MAP = {
    "completed": "stop",
    "incomplete": "length",
    "failed": "error",
    "cancelled": "error",
}


def _map_finish_reason(status: str | None) -> str:
    return _FINISH_REASON_MAP.get(status or "completed", "stop")


def _friendly_error(status_code: int, raw: str) -> str:
    if status_code == 429:
        return "ChatGPT usage quota exceeded or rate limit triggered. Please try again later."
    return f"HTTP {status_code}: {raw}"
