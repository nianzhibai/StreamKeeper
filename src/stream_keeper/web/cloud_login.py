from __future__ import annotations

import asyncio
import base64
import binascii
import io
import json
import secrets
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol
from urllib.parse import urlencode, urlsplit

import httpx
import qrcode
from qrcode.image.svg import SvgPathImage

_QUARK_CLIENT_ID = "532"
_QUARK_QR_URL = "https://su.quark.cn/4_eMHBJ"
_QUARK_TOKEN_URL = "https://uop.quark.cn/cas/ajax/getTokenForQrcodeLogin"
_QUARK_TICKET_URL = "https://uop.quark.cn/cas/ajax/getServiceTicketByQrcodeToken"
_QUARK_ACCOUNT_URL = "https://pan.quark.cn/account/info"
_QUARK_SUCCESS = 2_000_000
_QUARK_WAITING = 50_004_001
_QUARK_EXPIRED = 50_004_002
_WOPAN_BASE_URL = "https://panservice.mail.wo.cn"
_WOPAN_CLIENT_ID = "1001000021"
_115_QR_TOKEN_URL = "https://qrcodeapi.115.com/api/1.0/web/1.0/token"
_115_QR_STATUS_URL = "https://qrcodeapi.115.com/get/status/"
_115_QR_LOGIN_URL = "https://passportapi.115.com/app/1.0/web/1.0/login/qrcode"
_BAIDU_OPENLIST_API_BASE = "https://api.oplist.org"
# Public OOB application parameters published by OpenList-APIPages. They are
# intentionally embedded in its browser client and are not StreamKeeper secrets.
_BAIDU_OPENLIST_CLIENT_ID = "NqOMXF6XGhGRIGemsQ9nG0Na"
_BAIDU_OPENLIST_CLIENT_SECRET = "SVT6xpMdLcx6v4aCR4wT8BBOTbzFO8LM"
_GUANGYA_ACCOUNT_URL = "https://account.guangyapan.com/v1/auth"
_GUANGYA_CLIENT_ID = "aMe-8VSlkrbQXpUR"
_GUANGYA_DEVICE_CODE_URL = f"{_GUANGYA_ACCOUNT_URL}/device/code"
_GUANGYA_TOKEN_URL = f"{_GUANGYA_ACCOUNT_URL}/token"
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
)


_QR_IMAGE_MAX_BYTES = 1024 * 1024


class CloudLoginError(RuntimeError):
    """A QR login request could not be completed safely."""


@dataclass(frozen=True, slots=True)
class CloudLoginPoll:
    state: str
    credentials: dict[str, str] | None = None


class CloudLoginFlow(Protocol):
    qr_ttl_seconds: int

    async def start(self) -> str:
        """Return the QR code as a `data:` URI.

        The page CSP is `img-src 'self' data:`, so a remote image URL would reach the
        browser and render as a broken image. CloudLoginManager enforces this.
        """
        ...

    async def poll(self) -> CloudLoginPoll: ...

    async def aclose(self) -> None: ...


CloudLoginFlowFactory = Callable[[str], CloudLoginFlow]
CredentialSaver = Callable[[str, dict[str, str]], Awaitable[None]]


def _json_object(response: httpx.Response, provider_name: str) -> dict[str, object]:
    if response.is_error:
        raise CloudLoginError(f"{provider_name}扫码服务暂时不可用（HTTP {response.status_code}）")
    try:
        payload = response.json()
    except ValueError as exc:
        raise CloudLoginError(f"{provider_name}扫码服务返回格式错误") from exc
    if not isinstance(payload, dict):
        raise CloudLoginError(f"{provider_name}扫码服务返回格式错误")
    return payload


def _qr_svg_data_uri(content: str) -> str:
    code = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=4,
    )
    code.add_data(content)
    code.make(fit=True)
    image = code.make_image(image_factory=SvgPathImage)
    output = io.BytesIO()
    image.save(output)
    encoded = base64.b64encode(output.getvalue()).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"


def _png_data_uri(payload: bytes, provider_name: str) -> str:
    """Wrap PNG bytes for a provider that renders the QR code itself instead of exposing its payload."""
    if not payload.startswith(b"\x89PNG\r\n\x1a\n") or len(payload) > _QR_IMAGE_MAX_BYTES:
        raise CloudLoginError(f"{provider_name}返回了无效二维码")
    encoded = base64.b64encode(payload).decode("ascii")
    return f"data:image/png;base64,{encoded}"


class BaiduOpenListAuth:
    """Acquire Baidu OAuth credentials through OpenList's public OOB API."""

    def __init__(
        self,
        *,
        api_base: str = _BAIDU_OPENLIST_API_BASE,
        timeout_seconds: float = 40,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_base = api_base.rstrip("/")
        self._client = httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
            headers={"Accept": "application/json", "User-Agent": _USER_AGENT},
        )

    @staticmethod
    def _payload(response: httpx.Response, operation: str) -> dict[str, object]:
        if response.is_error:
            raise CloudLoginError(f"OpenList 在线 API {operation}失败（HTTP {response.status_code}）")
        try:
            payload = response.json()
        except ValueError as exc:
            raise CloudLoginError(f"OpenList 在线 API {operation}返回格式错误") from exc
        if not isinstance(payload, dict):
            raise CloudLoginError(f"OpenList 在线 API {operation}返回格式错误")
        return payload

    async def authorization_url(self) -> str:
        try:
            response = await self._client.get(
                f"{self._api_base}/baiduyun/requests",
                params={
                    "client_uid": "",
                    "client_key": _BAIDU_OPENLIST_CLIENT_ID,
                    "secret_key": _BAIDU_OPENLIST_CLIENT_SECRET,
                    "driver_txt": "baiduyun_ob",
                    "server_use": "false",
                },
            )
        except httpx.HTTPError as exc:
            raise CloudLoginError("OpenList 在线 API 连接失败") from exc
        payload = self._payload(response, "生成授权地址")
        value = payload.get("text")
        if not isinstance(value, str) or not value:
            raise CloudLoginError("OpenList 在线 API 没有返回百度授权地址")
        url = httpx.URL(value)
        if url.scheme != "https" or url.host != "openapi.baidu.com" or url.path != "/oauth/2.0/authorize":
            raise CloudLoginError("OpenList 在线 API 返回了不可信的百度授权地址")
        return str(url)

    async def exchange(self, authorization_code: str) -> dict[str, str]:
        code = authorization_code.strip()
        if not code or len(code) > 2048 or any(ord(char) < 33 for char in code):
            raise CloudLoginError("百度授权码格式错误")
        try:
            response = await self._client.get(
                f"{self._api_base}/baiduyun/callback",
                params={
                    "server_oob": "true",
                    "client_key": _BAIDU_OPENLIST_CLIENT_ID,
                    "secret_key": _BAIDU_OPENLIST_CLIENT_SECRET,
                    "code": code,
                },
            )
        except httpx.HTTPError as exc:
            raise CloudLoginError("OpenList 在线 API 交换百度凭据失败") from exc
        if not response.is_redirect:
            detail = " ".join(response.text.split())[:200]
            raise CloudLoginError(
                f"OpenList 在线 API 交换百度凭据失败（HTTP {response.status_code}）：{detail}"
            )
        fragment = urlsplit(response.headers.get("location", "")).fragment
        try:
            decoded = base64.b64decode(fragment, validate=True)
            payload = json.loads(decoded.decode("utf-8"))
        except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CloudLoginError("OpenList 在线 API 返回了无效的百度凭据") from exc
        if not isinstance(payload, dict):
            raise CloudLoginError("OpenList 在线 API 返回了无效的百度凭据")
        message = payload.get("message_err")
        if isinstance(message, str) and message:
            raise CloudLoginError(f"百度授权失败：{message[:200]}")
        access_token = payload.get("access_token")
        refresh_token = payload.get("refresh_token")
        if (
            not isinstance(access_token, str)
            or not access_token
            or not isinstance(refresh_token, str)
            or not refresh_token
            or len(access_token) > 4096
            or len(refresh_token) > 4096
        ):
            raise CloudLoginError("OpenList 在线 API 返回的百度 Token 不完整")
        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "client_id": _BAIDU_OPENLIST_CLIENT_ID,
            "client_secret": _BAIDU_OPENLIST_CLIENT_SECRET,
        }

    async def aclose(self) -> None:
        await self._client.aclose()


class QuarkQrLoginFlow:
    qr_ttl_seconds = 5 * 60

    def __init__(
        self,
        *,
        timeout_seconds: float = 15,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._request_id = str(uuid.uuid4())
        self._token = ""
        self._client = httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Origin": "https://pan.quark.cn",
                "Referer": "https://pan.quark.cn/",
                "User-Agent": _USER_AGENT,
            },
        )

    async def _get(self, url: str, *, params: dict[str, str]) -> httpx.Response:
        try:
            return await self._client.get(url, params=params)
        except httpx.HTTPError as exc:
            raise CloudLoginError("夸克扫码服务连接失败") from exc

    async def start(self) -> str:
        response = await self._get(
            _QUARK_TOKEN_URL,
            params={
                "client_id": _QUARK_CLIENT_ID,
                "v": "1.2",
                "request_id": self._request_id,
            },
        )
        payload = _json_object(response, "夸克")
        if payload.get("status") != _QUARK_SUCCESS:
            raise CloudLoginError("夸克二维码生成失败")
        data = payload.get("data")
        members = data.get("members") if isinstance(data, dict) else None
        token = members.get("token") if isinstance(members, dict) else None
        if not isinstance(token, str) or not token:
            raise CloudLoginError("夸克二维码响应缺少登录令牌")
        self._token = token
        query = urlencode(
            {
                "token": token,
                "client_id": _QUARK_CLIENT_ID,
                "ssb": "weblogin",
                "uc_param_str": "",
                "uc_biz_str": "S:custom|OPT:SAREA@0|OPT:IMMERSIVE@1|OPT:BACK_BTN_STYLE@0",
            }
        )
        return _qr_svg_data_uri(f"{_QUARK_QR_URL}?{query}")

    def _cookie_header(self) -> str:
        values: dict[str, str] = {}
        for cookie in self._client.cookies.jar:
            domain = cookie.domain.lstrip(".").lower()
            if (domain == "quark.cn" or domain.endswith(".quark.cn")) and not cookie.is_expired():
                values[cookie.name] = cookie.value
        if not any(name in values for name in ("__pus", "__puus")):
            raise CloudLoginError("夸克登录成功，但没有收到可用的网盘 Cookie")
        return "; ".join(f"{name}={value}" for name, value in sorted(values.items()))

    async def poll(self) -> CloudLoginPoll:
        if not self._token:
            raise CloudLoginError("夸克扫码会话尚未初始化")
        response = await self._get(
            _QUARK_TICKET_URL,
            params={
                "client_id": _QUARK_CLIENT_ID,
                "v": "1.2",
                "request_id": self._request_id,
                "token": self._token,
            },
        )
        payload = _json_object(response, "夸克")
        provider_status = payload.get("status")
        if provider_status == _QUARK_WAITING:
            return CloudLoginPoll("waiting")
        if provider_status == _QUARK_EXPIRED:
            return CloudLoginPoll("expired")
        if provider_status != _QUARK_SUCCESS:
            raise CloudLoginError("夸克扫码确认失败，请刷新二维码重试")

        data = payload.get("data")
        members = data.get("members") if isinstance(data, dict) else None
        ticket = members.get("service_ticket") if isinstance(members, dict) else None
        if not isinstance(ticket, str) or not ticket:
            raise CloudLoginError("夸克扫码响应缺少登录票据")
        account_response = await self._get(
            _QUARK_ACCOUNT_URL,
            params={"st": ticket, "lw": "scan"},
        )
        account = _json_object(account_response, "夸克")
        if account.get("success") is not True:
            raise CloudLoginError("夸克登录票据交换失败")
        return CloudLoginPoll("success", {"cookie": self._cookie_header()})

    async def aclose(self) -> None:
        await self._client.aclose()


class Pan115QrLoginFlow:
    qr_ttl_seconds = 5 * 60

    def __init__(
        self,
        *,
        timeout_seconds: float = 15,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._session: dict[str, str] = {}
        self._client = httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Referer": "https://115.com/",
                "User-Agent": _USER_AGENT,
            },
        )

    async def _get(self, url: str, *, params: dict[str, str] | None = None) -> httpx.Response:
        try:
            return await self._client.get(url, params=params)
        except httpx.HTTPError as exc:
            raise CloudLoginError("115 扫码服务连接失败") from exc

    async def _post(self, url: str, *, data: dict[str, str]) -> httpx.Response:
        try:
            return await self._client.post(url, data=data)
        except httpx.HTTPError as exc:
            raise CloudLoginError("115 扫码服务连接失败") from exc

    async def start(self) -> str:
        payload = _json_object(await self._get(_115_QR_TOKEN_URL), "115")
        if payload.get("state") not in {1, "1"}:
            raise CloudLoginError(str(payload.get("message") or payload.get("error") or "115 二维码生成失败"))
        data = payload.get("data")
        if not isinstance(data, dict):
            raise CloudLoginError("115 二维码响应缺少 data")
        values = {key: str(data.get(key) or "") for key in ("qrcode", "sign", "time", "uid")}
        if not all(values.values()):
            raise CloudLoginError("115 二维码响应缺少必要数据")
        self._session = values
        return _qr_svg_data_uri(values["qrcode"])

    async def poll(self) -> CloudLoginPoll:
        if not self._session:
            raise CloudLoginError("115 扫码会话尚未初始化")
        payload = _json_object(
            await self._get(
                _115_QR_STATUS_URL,
                params={
                    "uid": self._session["uid"],
                    "time": self._session["time"],
                    "sign": self._session["sign"],
                    "_": str(int(datetime.now(timezone.utc).timestamp())),
                },
            ),
            "115",
        )
        if payload.get("state") not in {1, "1"}:
            raise CloudLoginError(str(payload.get("message") or payload.get("error") or "115 扫码状态查询失败"))
        data = payload.get("data")
        if not isinstance(data, dict):
            raise CloudLoginError("115 扫码状态响应缺少 data")
        try:
            state = int(data.get("status", -1))
        except (TypeError, ValueError):
            state = -1
        if state == 0:
            return CloudLoginPoll("waiting")
        if state == 1:
            return CloudLoginPoll("scanned")
        if state in {-1, -2}:
            return CloudLoginPoll("expired" if state == -1 else "cancelled")
        if state != 2:
            raise CloudLoginError("115 扫码返回了未知状态")
        login_payload = _json_object(
            await self._post(_115_QR_LOGIN_URL, data={"account": self._session["uid"], "app": "web"}),
            "115",
        )
        if login_payload.get("state") not in {1, "1"}:
            raise CloudLoginError(str(login_payload.get("message") or login_payload.get("error") or "115 登录失败"))
        login_data = login_payload.get("data")
        if not isinstance(login_data, dict):
            raise CloudLoginError("115 登录响应缺少 data")
        credential = login_data.get("cookie") or login_data.get("credential")
        if not isinstance(credential, dict):
            credential = login_data
        values = {
            key: str(credential.get(key) or credential.get(key.lower()) or "") for key in ("UID", "CID", "SEID", "KID")
        }
        if not values["UID"] or not values["CID"] or not values["SEID"]:
            raise CloudLoginError("115 登录响应缺少有效 Cookie")
        cookie = "; ".join(f"{key}={value}" for key, value in values.items() if value)
        return CloudLoginPoll("success", {"cookie": cookie})

    async def aclose(self) -> None:
        await self._client.aclose()


class WoPanQrLoginFlow:
    # The official Web client rotates its QR code every 60 seconds.
    qr_ttl_seconds = 60

    def __init__(
        self,
        *,
        timeout_seconds: float = 15,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._uuid = ""
        self._client = httpx.AsyncClient(
            base_url=_WOPAN_BASE_URL,
            follow_redirects=False,
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Access-Token": "",
                "Client-Id": _WOPAN_CLIENT_ID,
                "Origin": "https://pan.wo.cn",
                "Referer": "https://pan.wo.cn/",
                "User-Agent": _USER_AGENT,
                "X-YP-Access-Token": "",
                "X-YP-Client-Id": _WOPAN_CLIENT_ID,
            },
        )

    async def _get(self, path: str, *, params: dict[str, str] | None = None) -> httpx.Response:
        try:
            return await self._client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise CloudLoginError("联通云盘扫码服务连接失败") from exc

    @staticmethod
    def _result(payload: dict[str, object]) -> dict[str, object]:
        meta = payload.get("meta")
        if not isinstance(meta, dict) or str(meta.get("code")) != "200":
            raise CloudLoginError("联通云盘扫码服务返回失败")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise CloudLoginError("联通云盘扫码服务返回格式错误")
        return result

    async def start(self) -> str:
        response = await self._get("/wohome/open/v1/QRCode/generate")
        result = self._result(_json_object(response, "联通云盘"))
        image = result.get("image")
        session_uuid = result.get("uuid")
        if not isinstance(image, str) or not isinstance(session_uuid, str) or not session_uuid:
            raise CloudLoginError("联通云盘二维码响应缺少必要数据")
        try:
            decoded = base64.b64decode(image, validate=True)
        except ValueError as exc:
            raise CloudLoginError("联通云盘返回了无效二维码") from exc
        data_uri = _png_data_uri(decoded, "联通云盘")
        self._uuid = session_uuid
        return data_uri

    async def poll(self) -> CloudLoginPoll:
        if not self._uuid:
            raise CloudLoginError("联通云盘扫码会话尚未初始化")
        response = await self._get(
            "/wohome/open/v1/QRCode/query",
            params={"uuid": self._uuid},
        )
        result = self._result(_json_object(response, "联通云盘"))
        state = result.get("state")
        if state == 1:
            return CloudLoginPoll("waiting")
        if state == 2:
            return CloudLoginPoll("scanned")
        if state != 3:
            return CloudLoginPoll("expired")
        access_token = result.get("token")
        refresh_token = result.get("refreshToken")
        if not isinstance(access_token, str) or len(access_token.encode()) < 16:
            raise CloudLoginError("联通云盘扫码成功，但响应缺少有效 Access Token")
        if not isinstance(refresh_token, str):
            refresh_token = ""
        return CloudLoginPoll(
            "success",
            {"access_token": access_token, "refresh_token": refresh_token},
        )

    async def aclose(self) -> None:
        await self._client.aclose()


class GuangYaQrLoginFlow:
    # The server grants the device code for 120 seconds.
    qr_ttl_seconds = 120

    def __init__(
        self,
        *,
        timeout_seconds: float = 15,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._device_code = ""
        self._client = httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
            headers={
                "Accept": "*/*",
                "Origin": "https://www.guangyapan.com",
                "Referer": "https://www.guangyapan.com/",
                "User-Agent": _USER_AGENT,
            },
        )

    async def _post(self, path: str, body: dict[str, object]) -> httpx.Response:
        try:
            return await self._client.post(path, json=body)
        except httpx.HTTPError as exc:
            raise CloudLoginError("光鸭网盘扫码服务连接失败") from exc

    async def start(self) -> str:
        payload = _json_object(
            await self._post(
                _GUANGYA_DEVICE_CODE_URL,
                {"scope": "user", "client_id": _GUANGYA_CLIENT_ID},
            ),
            "光鸭网盘",
        )
        device_code = payload.get("device_code")
        verification_uri = payload.get("verification_uri_complete") or payload.get("verification_url")
        if (
            not isinstance(device_code, str)
            or not device_code
            or not isinstance(verification_uri, str)
            or not verification_uri
        ):
            raise CloudLoginError("光鸭网盘二维码响应缺少必要数据")
        self._device_code = device_code
        return _qr_svg_data_uri(verification_uri)

    async def poll(self) -> CloudLoginPoll:
        if not self._device_code:
            raise CloudLoginError("光鸭网盘扫码会话尚未初始化")
        response = await self._post(
            _GUANGYA_TOKEN_URL,
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": self._device_code,
                "client_id": _GUANGYA_CLIENT_ID,
            },
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise CloudLoginError("光鸭网盘扫码服务返回格式错误") from exc
        if not isinstance(payload, dict):
            raise CloudLoginError("光鸭网盘扫码服务返回格式错误")
        error = payload.get("error")
        if error == "authorization_pending" or payload.get("error_code") == 4050:
            return CloudLoginPoll("waiting")
        if error == "expired_token" or payload.get("error_code") == 4052:
            return CloudLoginPoll("expired")
        if error is not None or response.is_error:
            raise CloudLoginError("光鸭网盘扫码确认失败，请刷新二维码重试")
        access_token = payload.get("access_token")
        refresh_token = payload.get("refresh_token")
        if (
            not isinstance(access_token, str)
            or not access_token
            or not isinstance(refresh_token, str)
            or not refresh_token
        ):
            raise CloudLoginError("光鸭网盘扫码响应缺少登录令牌")
        return CloudLoginPoll(
            "success",
            {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "client_id": _GUANGYA_CLIENT_ID,
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()


def create_cloud_login_flow(provider: str) -> CloudLoginFlow:
    if provider == "quark":
        return QuarkQrLoginFlow()
    if provider == "wopan":
        return WoPanQrLoginFlow()
    if provider == "pan115":
        return Pan115QrLoginFlow()
    if provider == "guangya":
        return GuangYaQrLoginFlow()
    raise ValueError(f"不支持的扫码登录类型: {provider}")


@dataclass(frozen=True, slots=True)
class CloudLoginSnapshot:
    session_id: str
    provider: str
    state: str
    message: str
    qr_image: str | None
    expires_at: datetime


@dataclass(slots=True)
class _CloudLoginSession:
    session_id: str
    provider: str
    flow: CloudLoginFlow
    qr_image: str
    expires_at: datetime
    state: str = "waiting"
    message: str = "等待扫码"
    task: asyncio.Task[None] | None = None

    def snapshot(self) -> CloudLoginSnapshot:
        return CloudLoginSnapshot(
            session_id=self.session_id,
            provider=self.provider,
            state=self.state,
            message=self.message,
            qr_image=self.qr_image if self.state not in {"success", "cancelled"} else None,
            expires_at=self.expires_at,
        )


class CloudLoginManager:
    """Keep short-lived QR sessions in memory and persist only final credentials."""

    def __init__(
        self,
        save_credentials: CredentialSaver,
        *,
        flow_factory: CloudLoginFlowFactory = create_cloud_login_flow,
        poll_interval: float = 2,
    ) -> None:
        self._save_credentials = save_credentials
        self._flow_factory = flow_factory
        self._poll_interval = max(0.01, poll_interval)
        self._sessions: dict[str, _CloudLoginSession] = {}
        self._provider_sessions: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._start_locks = {provider: asyncio.Lock() for provider in ("quark", "wopan", "pan115", "guangya")}

    async def start(self, provider: str) -> CloudLoginSnapshot:
        if provider not in {"quark", "wopan", "pan115", "guangya"}:
            raise ValueError(f"不支持的扫码登录类型: {provider}")
        async with self._start_locks[provider]:
            return await self._start(provider)

    async def _start(self, provider: str) -> CloudLoginSnapshot:
        await self.cancel_provider(provider)
        flow = self._flow_factory(provider)
        try:
            qr_image = await flow.start()
            if not qr_image.startswith("data:image/"):
                raise CloudLoginError(f"{provider}扫码流程返回了无法在页面内渲染的二维码")
        except BaseException:
            await flow.aclose()
            raise
        now = datetime.now(timezone.utc)
        session = _CloudLoginSession(
            session_id=secrets.token_urlsafe(24),
            provider=provider,
            flow=flow,
            qr_image=qr_image,
            expires_at=now + timedelta(seconds=flow.qr_ttl_seconds),
        )
        async with self._lock:
            self._sessions[session.session_id] = session
            self._provider_sessions[provider] = session.session_id
            session.task = asyncio.create_task(
                self._run(session),
                name=f"cloud-qr-login-{provider}",
            )
        return session.snapshot()

    async def _run(self, session: _CloudLoginSession) -> None:
        failures = 0
        try:
            while datetime.now(timezone.utc) < session.expires_at:
                remaining = (session.expires_at - datetime.now(timezone.utc)).total_seconds()
                await asyncio.sleep(min(self._poll_interval, max(0.01, remaining)))
                try:
                    result = await session.flow.poll()
                    failures = 0
                except CloudLoginError as exc:
                    failures += 1
                    if failures < 3:
                        session.message = "连接波动，正在重试"
                        continue
                    session.state = "error"
                    session.message = str(exc)[:200]
                    return
                if result.state == "waiting":
                    session.state = "waiting"
                    session.message = "等待扫码"
                    continue
                if result.state == "scanned":
                    session.state = "scanned"
                    session.message = "已扫码，请在手机上确认"
                    continue
                if result.state == "expired":
                    session.state = "expired"
                    session.message = "二维码已过期"
                    return
                if result.state == "cancelled":
                    session.state = "cancelled"
                    session.message = "扫码登录已取消"
                    return
                if result.state != "success" or result.credentials is None:
                    session.state = "error"
                    session.message = "扫码登录返回了未知状态"
                    return
                credentials = dict(result.credentials)
                try:
                    await self._save_credentials(session.provider, credentials)
                finally:
                    credentials.clear()
                    result.credentials.clear()
                session.state = "success"
                session.message = "登录成功，凭证已保存"
                session.qr_image = ""
                return
            session.state = "expired"
            session.message = "二维码已过期"
        except asyncio.CancelledError:
            session.state = "cancelled"
            session.message = "扫码登录已取消"
            raise
        except Exception:
            session.state = "error"
            session.message = "保存登录凭证失败"
        finally:
            await session.flow.aclose()

    async def get(self, provider: str, session_id: str) -> CloudLoginSnapshot | None:
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None or session.provider != provider:
                return None
            return session.snapshot()

    async def cancel(self, provider: str, session_id: str) -> bool:
        async with self._lock:
            session = self._sessions.pop(session_id, None)
            if session is None or session.provider != provider:
                if session is not None:
                    self._sessions[session_id] = session
                return False
            if self._provider_sessions.get(provider) == session_id:
                self._provider_sessions.pop(provider, None)
        if session.task is not None and not session.task.done():
            session.task.cancel()
            await asyncio.gather(session.task, return_exceptions=True)
        return True

    async def cancel_provider(self, provider: str) -> None:
        async with self._lock:
            session_id = self._provider_sessions.get(provider)
        if session_id is not None:
            await self.cancel(provider, session_id)

    async def shutdown(self) -> None:
        async with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
            self._provider_sessions.clear()
        tasks = [session.task for session in sessions if session.task is not None and not session.task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
