from __future__ import annotations

import base64
import binascii
import secrets

from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send


class BasicAuthMiddleware:
    """Protect every HTTP route except the container health check."""

    def __init__(self, app: ASGIApp, username: str, password: str) -> None:
        self.app = app
        self.username = username
        self.password = password

    @staticmethod
    def _read_credentials(scope: Scope) -> tuple[str, str] | None:
        headers = dict(scope.get("headers", []))
        value = headers.get(b"authorization", b"")
        if not value.lower().startswith(b"basic "):
            return None
        try:
            decoded = base64.b64decode(value.split(b" ", 1)[1], validate=True).decode("utf-8")
            username, password = decoded.split(":", 1)
        except (ValueError, UnicodeDecodeError, binascii.Error):
            return None
        return username, password

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") == "/health" or not self.password:
            await self.app(scope, receive, send)
            return

        credentials = self._read_credentials(scope)
        authenticated = (
            credentials is not None
            and secrets.compare_digest(credentials[0], self.username)
            and secrets.compare_digest(credentials[1], self.password)
        )
        if authenticated:
            await self.app(scope, receive, send)
            return

        response = PlainTextResponse(
            "Authentication required",
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="Douyin Recorder", charset="UTF-8"'},
        )
        await response(scope, receive, send)


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
                        (
                            b"content-security-policy",
                            content_security_policy,
                        ),
                    ]
                )
                if scope.get("scheme") == "https":
                    headers.append((b"strict-transport-security", b"max-age=31536000; includeSubDomains"))
                if str(scope.get("path", "")).startswith("/api/"):
                    headers.append((b"cache-control", b"no-store"))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)
