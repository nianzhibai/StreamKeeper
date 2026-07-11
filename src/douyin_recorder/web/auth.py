from __future__ import annotations

import asyncio
import math
import secrets
import time
from collections import deque
from http.cookies import CookieError, SimpleCookie
from urllib.parse import quote

from starlette.responses import JSONResponse, RedirectResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .store import TaskStore

SESSION_COOKIE_NAME = "douyin_session"
CSRF_HEADER_NAME = b"x-csrf-token"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
PUBLIC_PATHS = {"/health", "/login", "/api/auth/login", "/favicon.ico"}


class LoginRateLimiter:
    """Small in-memory failure window for the single-process login endpoint."""

    def __init__(self, max_attempts: int, window_seconds: int, max_clients: int = 10000) -> None:
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self.max_clients = max_clients
        self._attempts: dict[str, deque[float]] = {}
        self._lock = asyncio.Lock()

    def _prune(self, attempts: deque[float], now: float) -> None:
        cutoff = now - self.window_seconds
        while attempts and attempts[0] <= cutoff:
            attempts.popleft()

    async def retry_after(self, key: str) -> int | None:
        now = time.monotonic()
        async with self._lock:
            attempts = self._attempts.get(key)
            if attempts is None:
                return None
            self._prune(attempts, now)
            if not attempts:
                self._attempts.pop(key, None)
                return None
            if len(attempts) < self.max_attempts:
                return None
            return max(1, math.ceil(self.window_seconds - (now - attempts[0])))

    async def register_failure(self, key: str) -> None:
        now = time.monotonic()
        async with self._lock:
            if key not in self._attempts and len(self._attempts) >= self.max_clients:
                cutoff = now - self.window_seconds
                stale_keys = [name for name, values in self._attempts.items() if not values or values[-1] <= cutoff]
                for name in stale_keys:
                    self._attempts.pop(name, None)
                while len(self._attempts) >= self.max_clients:
                    self._attempts.pop(next(iter(self._attempts)))
            attempts = self._attempts.setdefault(key, deque())
            self._prune(attempts, now)
            attempts.append(now)

    async def reset(self, key: str) -> None:
        async with self._lock:
            self._attempts.pop(key, None)


def read_session_cookie(scope: Scope) -> str | None:
    raw_cookie = dict(scope.get("headers", [])).get(b"cookie")
    if not raw_cookie:
        return None
    try:
        cookies = SimpleCookie()
        cookies.load(raw_cookie.decode("latin-1"))
        morsel = cookies.get(SESSION_COOKIE_NAME)
        return morsel.value if morsel else None
    except (CookieError, UnicodeDecodeError, ValueError):
        return None


def _is_public_path(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith("/static/")


def _is_same_origin(scope: Scope, headers: dict[bytes, bytes]) -> bool:
    origin = headers.get(b"origin")
    host = headers.get(b"host")
    if not origin or not host:
        return False
    try:
        expected = f"{scope.get('scheme', 'http')}://{host.decode('latin-1')}"
        return secrets.compare_digest(origin.decode("latin-1").rstrip("/"), expected.rstrip("/"))
    except UnicodeDecodeError:
        return False


class SessionAuthMiddleware:
    """Authenticate browser requests with an opaque, database-backed session."""

    def __init__(self, app: ASGIApp, store: TaskStore, enabled: bool = True) -> None:
        self.app = app
        self.store = store
        self.enabled = enabled

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.enabled:
            await self.app(scope, receive, send)
            return

        path = str(scope.get("path", ""))
        if _is_public_path(path):
            await self.app(scope, receive, send)
            return

        token = read_session_cookie(scope)
        session = await self.store.get_session(token) if token else None
        if session is None:
            if path.startswith("/api/") and path != "/api/docs":
                response = JSONResponse({"detail": "登录已过期，请重新登录"}, status_code=401)
            else:
                query = scope.get("query_string", b"").decode("latin-1")
                destination = path + (f"?{query}" if query else "")
                response = RedirectResponse(f"/login?next={quote(destination, safe='')}", status_code=303)
            await response(scope, receive, send)
            return

        state = scope.setdefault("state", {})
        state["auth_session"] = session
        state["auth_token"] = token

        method = str(scope.get("method", "GET")).upper()
        if path.startswith("/api/") and method not in SAFE_METHODS:
            headers = dict(scope.get("headers", []))
            csrf_value = headers.get(CSRF_HEADER_NAME, b"").decode("latin-1")
            valid_token = bool(csrf_value) and secrets.compare_digest(csrf_value, session.csrf_token)
            if not valid_token and not _is_same_origin(scope, headers):
                response = JSONResponse({"detail": "CSRF 校验失败，请刷新页面后重试"}, status_code=403)
                await response(scope, receive, send)
                return

        await self.app(scope, receive, send)


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                if scope.get("path") == "/api/docs":
                    content_security_policy = (
                        b"default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                        b"style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                        b"img-src 'self' data: https://fastapi.tiangolo.com; connect-src 'self'; "
                        b"frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
                    )
                else:
                    content_security_policy = (
                        b"default-src 'self'; script-src 'self'; style-src 'self'; "
                        b"img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
                        b"base-uri 'none'; form-action 'self'"
                    )
                headers.extend(
                    [
                        (b"x-content-type-options", b"nosniff"),
                        (b"x-frame-options", b"DENY"),
                        (b"referrer-policy", b"no-referrer"),
                        (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
                        (b"content-security-policy", content_security_policy),
                    ]
                )
                if scope.get("scheme") == "https":
                    headers.append((b"strict-transport-security", b"max-age=31536000; includeSubDomains"))
                path = str(scope.get("path", ""))
                if path == "/login" or path.startswith("/api/"):
                    headers.append((b"cache-control", b"no-store"))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)
