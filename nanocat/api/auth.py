"""Authentication and bounded login-abuse protection for NanoCat HTTP services."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request, Response, status

WEB_SESSION_COOKIE = "nanocat_web_session"
WEB_CSRF_COOKIE = "nanocat_web_csrf"


def token_fingerprint(token: str) -> str:
    """Return a log-safe stable fingerprint for an authentication token."""
    return hashlib.sha256(token.encode()).hexdigest()[:12]


class ApiAuthenticator:
    """Validate a runtime-scoped bearer token without exposing it to handlers."""

    def __init__(self, token: str | None) -> None:
        self.generated = token is None
        self.token = secrets.token_urlsafe(32) if token is None else token

    async def require(self, request: Request) -> str:
        authorization = request.headers.get("authorization", "")
        scheme, _, candidate = authorization.partition(" ")
        if scheme.casefold() != "bearer" or not hmac.compare_digest(candidate, self.token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return "web:local"

    def reconfigure(self, token: str | None) -> None:
        """Rotate bearer authentication without rebuilding the API listener."""
        self.generated = token is None
        self.token = secrets.token_urlsafe(32) if token is None else token


@dataclass(slots=True)
class _Bucket:
    tokens: float
    updated_at: float


@dataclass(slots=True)
class _AttemptState:
    failures: int
    locked_until: float
    expires_at: float
    bucket: _Bucket


@dataclass(slots=True)
class _WebSession:
    csrf_token: str
    created_at: float
    last_seen_at: float


class LoginRateLimiter:
    """Per-source and global token buckets plus exponential failure lockout."""

    def __init__(self, *, max_sources: int = 2048, source_ttl_s: float = 3600.0) -> None:
        self._states: OrderedDict[str, _AttemptState] = OrderedDict()
        self._max_sources = max_sources
        self._source_ttl_s = source_ttl_s
        now = time.monotonic()
        self._global = _Bucket(tokens=30.0, updated_at=now)
        self._lock = asyncio.Lock()

    @staticmethod
    def _refill(bucket: _Bucket, *, capacity: float, rate: float, now: float) -> None:
        elapsed = max(0.0, now - bucket.updated_at)
        bucket.tokens = min(capacity, bucket.tokens + elapsed * rate)
        bucket.updated_at = now

    def _prune(self, now: float) -> None:
        stale = [key for key, state in self._states.items() if state.expires_at <= now]
        for key in stale:
            self._states.pop(key, None)
        while len(self._states) > self._max_sources:
            self._states.popitem(last=False)

    async def before_attempt(self, source: str) -> float:
        """Return retry seconds, or zero after consuming available capacity."""
        async with self._lock:
            now = time.monotonic()
            self._prune(now)
            state = self._states.get(source)
            if state is not None and state.locked_until > now:
                return max(1.0, state.locked_until - now)

            self._refill(self._global, capacity=30.0, rate=1.0, now=now)
            if self._global.tokens < 1.0:
                return max(1.0, (1.0 - self._global.tokens))

            if state is None:
                state = _AttemptState(
                    failures=0,
                    locked_until=0.0,
                    expires_at=now + self._source_ttl_s,
                    bucket=_Bucket(tokens=5.0, updated_at=now),
                )
                self._states[source] = state
                self._prune(now)
            self._refill(state.bucket, capacity=5.0, rate=1.0 / 30.0, now=now)
            if state.bucket.tokens < 1.0:
                return max(1.0, (1.0 - state.bucket.tokens) * 30.0)

            self._global.tokens -= 1.0
            state.bucket.tokens -= 1.0
            state.expires_at = now + self._source_ttl_s
            self._states.move_to_end(source)
            return 0.0

    async def failure(self, source: str) -> float:
        async with self._lock:
            now = time.monotonic()
            state = self._states.get(source)
            if state is None:
                state = _AttemptState(
                    failures=0,
                    locked_until=0.0,
                    expires_at=now + self._source_ttl_s,
                    bucket=_Bucket(tokens=4.0, updated_at=now),
                )
                self._states[source] = state
            state.failures += 1
            delay = 0.5 if state.failures <= 3 else min(900.0, 2.0 ** (state.failures - 4))
            if state.failures >= 4:
                state.locked_until = now + delay
            state.expires_at = now + self._source_ttl_s
            self._states.move_to_end(source)
            self._prune(now)
            return delay

    async def success(self, source: str) -> None:
        async with self._lock:
            self._states.pop(source, None)


class WebSessionAuth:
    """In-memory single-user password sessions with CSRF and Origin checks."""

    def __init__(
        self,
        password: str | None,
        *,
        trusted_proxies: list[str] | None = None,
        max_sessions: int = 128,
        idle_ttl_s: float = 12 * 3600,
        absolute_ttl_s: float = 7 * 24 * 3600,
    ) -> None:
        self.password = password
        self.trusted_proxies = frozenset(trusted_proxies or [])
        self.max_sessions = max_sessions
        self.idle_ttl_s = idle_ttl_s
        self.absolute_ttl_s = absolute_ttl_s
        self.sessions: OrderedDict[str, _WebSession] = OrderedDict()
        self.limiter = LoginRateLimiter()
        self._lock = asyncio.Lock()

    @property
    def protected(self) -> bool:
        return self.password is not None

    def source(self, request: Request) -> str:
        peer = request.client.host if request.client else "unknown"
        if peer not in self.trusted_proxies:
            return peer
        forwarded = request.headers.get("x-forwarded-for", "")
        return forwarded.split(",", 1)[0].strip() or peer

    def is_secure(self, request: Request) -> bool:
        if request.url.scheme == "https":
            return True
        peer = request.client.host if request.client else "unknown"
        if peer not in self.trusted_proxies:
            return False
        forwarded = request.headers.get("x-forwarded-proto", "")
        return forwarded.split(",", 1)[0].strip().casefold() == "https"

    @staticmethod
    def validate_origin(request: Request) -> None:
        origin = request.headers.get("origin")
        host = request.headers.get("host")
        if not origin or not host:
            raise HTTPException(status_code=403, detail="Origin validation failed")
        if origin.rstrip("/") not in {f"http://{host}", f"https://{host}"}:
            raise HTTPException(status_code=403, detail="Origin validation failed")

    def _prune_sessions(self, now: float) -> None:
        expired = [
            token
            for token, item in self.sessions.items()
            if now - item.last_seen_at > self.idle_ttl_s
            or now - item.created_at > self.absolute_ttl_s
        ]
        for token in expired:
            self.sessions.pop(token, None)
        while len(self.sessions) > self.max_sessions:
            self.sessions.popitem(last=False)

    async def login(self, request: Request, response: Response, candidate: str) -> dict[str, Any]:
        self.validate_origin(request)
        source = self.source(request)
        retry_after = await self.limiter.before_attempt(source)
        if retry_after > 0:
            raise HTTPException(
                status_code=429,
                detail="Login temporarily unavailable",
                headers={"Retry-After": str(max(1, int(retry_after + 0.999)))},
            )
        expected = self.password or ""
        if not hmac.compare_digest(candidate, expected):
            delay = await self.limiter.failure(source)
            if delay <= 0.5:
                await asyncio.sleep(delay)
                raise HTTPException(status_code=401, detail="Invalid credentials")
            raise HTTPException(
                status_code=429,
                detail="Invalid credentials",
                headers={"Retry-After": str(max(1, int(delay + 0.999)))},
            )

        await self.limiter.success(source)
        now = time.monotonic()
        session_token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(24)
        async with self._lock:
            self._prune_sessions(now)
            self.sessions[session_token] = _WebSession(csrf_token, now, now)
            self._prune_sessions(now)
        secure = self.is_secure(request)
        response.set_cookie(
            WEB_SESSION_COOKIE,
            session_token,
            httponly=True,
            secure=secure,
            samesite="strict",
            max_age=int(self.absolute_ttl_s),
            path="/",
        )
        response.set_cookie(
            WEB_CSRF_COOKIE,
            csrf_token,
            httponly=False,
            secure=secure,
            samesite="strict",
            max_age=int(self.absolute_ttl_s),
            path="/",
        )
        return {"authenticated": True, "csrfToken": csrf_token}

    async def require(self, request: Request, *, mutation: bool = False) -> str:
        if mutation:
            self.validate_origin(request)
        if not self.protected:
            return "web:local"
        token = request.cookies.get(WEB_SESSION_COOKIE, "")
        now = time.monotonic()
        async with self._lock:
            self._prune_sessions(now)
            session = self.sessions.get(token)
            if session is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            session.last_seen_at = now
            self.sessions.move_to_end(token)
        if mutation:
            csrf = request.headers.get("x-csrf-token", "")
            if not csrf or not hmac.compare_digest(csrf, session.csrf_token):
                raise HTTPException(status_code=403, detail="CSRF validation failed")
        return "web:local"

    async def logout(self, request: Request, response: Response) -> None:
        token = request.cookies.get(WEB_SESSION_COOKIE, "")
        async with self._lock:
            self.sessions.pop(token, None)
        response.delete_cookie(WEB_SESSION_COOKIE, path="/")
        response.delete_cookie(WEB_CSRF_COOKIE, path="/")

    async def reconfigure(
        self,
        password: str | None,
        *,
        trusted_proxies: list[str] | None = None,
    ) -> None:
        """Apply Web authentication changes and revoke existing browser sessions."""
        async with self._lock:
            changed = password != self.password
            self.password = password
            self.trusted_proxies = frozenset(trusted_proxies or [])
            if changed:
                self.sessions.clear()
