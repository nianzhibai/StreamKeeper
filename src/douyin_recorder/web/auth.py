from __future__ import annotations

import secrets
from datetime import timedelta
from http.cookies import CookieError, SimpleCookie
from urllib.parse import quote

from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from .store import TaskStore, WebSession, utc_now

SESSION_COOKIE_NAME = "douyin_session"
CSRF_HEADER_NAME = b"x-csrf-token"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
PUBLIC_PATHS = {"/health", "/login", "/api/auth/login", "/favicon.ico"}


def set_session_cookie(
    response: Response,
    token: str,
    session: WebSession,
    ttl_seconds: int,
    *,
    secure: bool,
) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=ttl_seconds,
        expires=session.expires_at,
        path="/",
        secure=secure,
        httponly=True,
        samesite="strict",
    )


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
    """Authenticate requests and renew active sessions halfway through their TTL."""

    def __init__(
        self,
        app: ASGIApp,
        store: TaskStore,
        enabled: bool = True,
        session_ttl_seconds: int = 7 * 24 * 3600,
    ) -> None:
        self.app = app
        self.store = store
        self.enabled = enabled
        self.session_ttl_seconds = session_ttl_seconds
        self.renew_before_seconds = max(1, session_ttl_seconds // 2)

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

        method = str(scope.get("method", "GET")).upper()
        if path.startswith("/api/") and method not in SAFE_METHODS:
            headers = dict(scope.get("headers", []))
            csrf_value = headers.get(CSRF_HEADER_NAME, b"").decode("latin-1")
            valid_token = bool(csrf_value) and secrets.compare_digest(csrf_value, session.csrf_token)
            if not valid_token and not _is_same_origin(scope, headers):
                response = JSONResponse({"detail": "CSRF 校验失败，请刷新页面后重试"}, status_code=403)
                await response(scope, receive, send)
                return

        renewed = False
        if path != "/api/auth/logout" and session.expires_at <= utc_now() + timedelta(
            seconds=self.renew_before_seconds
        ):
            session, renewed = await self.store.renew_session_if_needed(
                token,
                self.session_ttl_seconds,
                self.renew_before_seconds,
            )
            if session is None:
                response = JSONResponse({"detail": "登录已过期，请重新登录"}, status_code=401)
                await response(scope, receive, send)
                return

        state = scope.setdefault("state", {})
        state["auth_session"] = session
        state["auth_token"] = token

        if not renewed:
            await self.app(scope, receive, send)
            return

        cookie_response = Response()
        set_session_cookie(
            cookie_response,
            token,
            session,
            self.session_ttl_seconds,
            secure=scope.get("scheme") == "https",
        )
        renewal_header = next(value for name, value in cookie_response.raw_headers if name == b"set-cookie")

        async def send_with_renewed_cookie(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                session_prefix = f"{SESSION_COOKIE_NAME}=".encode("latin-1")
                has_session_cookie = any(
                    name == b"set-cookie" and value.startswith(session_prefix) for name, value in headers
                )
                if not has_session_cookie:
                    headers.append((b"set-cookie", renewal_header))
                    message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_renewed_cookie)


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
                        (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), fullscreen=(self)"),
                        (b"content-security-policy", content_security_policy),
                    ]
                )
                if scope.get("scheme") == "https":
                    headers.append((b"strict-transport-security", b"max-age=31536000; includeSubDomains"))
                path = str(scope.get("path", ""))
                if path.startswith("/static/"):
                    headers.append((b"cache-control", b"no-cache, must-revalidate"))
                elif path == "/login" or path.startswith("/api/"):
                    headers.append((b"cache-control", b"no-store"))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)
