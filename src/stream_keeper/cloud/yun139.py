from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import secrets
import time
import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .base import CloudUploadError, CredentialUpdate, RemoteEntry, UploadProgress, split_remote_file
from .yun139_protocol import yun139_signature

_ROUTE_URL = "https://user-njs.yun.139.com/user/route/qryRoutePolicy"
_REFRESH_URL = "https://aas.caiyun.feixin.10086.cn:443/tellin/authTokenRefresh.do"
_Sleep = Callable[[float], Awaitable[None]]


class Yun139Client:
    """China Mobile's personal cloud API, following OpenList's 139Yun driver."""

    def __init__(
        self,
        authorization: str,
        *,
        root_id: str = "/",
        client_type: str = "web",
        timeout_seconds: int = 300,
        on_credential_update: CredentialUpdate | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: _Sleep = asyncio.sleep,
    ) -> None:
        authorization = authorization.strip()
        if authorization.lower().startswith("basic "):
            authorization = authorization[6:].strip()
        self.authorization = authorization
        self._prefix, self.account, self._token, self._expires_at = self._parse_authorization(authorization)
        self.root_id = root_id
        self.client_type = client_type
        self._on_credential_update = on_credential_update
        self._sleep = sleep
        self._refresh_lock = asyncio.Lock()
        self._route_lock = asyncio.Lock()
        self._next_refresh_at = 0.0
        self._personal_host = ""
        timeout = httpx.Timeout(float(timeout_seconds))
        # Signed storage URLs must never receive the account authorization.
        self._api_client = httpx.AsyncClient(timeout=timeout, transport=transport, follow_redirects=False)
        self._transfer_client = httpx.AsyncClient(timeout=timeout, transport=transport, follow_redirects=False)

    @staticmethod
    def _parse_authorization(authorization: str) -> tuple[str, str, str, float]:
        try:
            prefix, account, token = base64.b64decode(authorization, validate=True).decode().split(":", 2)
            expires_at = int(token.split("|")[3]) / 1000
            if not prefix or not account or not token or not expires_at:
                raise ValueError
            return prefix, account, token, expires_at
        except (ValueError, IndexError, UnicodeError, binascii.Error) as exc:
            raise CloudUploadError("移动云盘 Authorization 格式无效，请从已登录的网页请求中重新复制") from exc

    async def _ensure_authorization(self) -> None:
        async with self._refresh_lock:
            now = time.time()
            if self._expires_at <= now:
                raise CloudUploadError("移动云盘 Authorization 已过期，请重新登录网页并更新凭据")
            if now < self._next_refresh_at or self._expires_at - now > 15 * 86400:
                return
            root = ET.Element("root")
            ET.SubElement(root, "token").text = self._token
            ET.SubElement(root, "account").text = self.account
            ET.SubElement(root, "clienttype").text = "656"
            try:
                response = await self._api_client.post(
                    _REFRESH_URL,
                    content=ET.tostring(root, encoding="utf-8"),
                    headers={"Content-Type": "application/xml"},
                )
                response.raise_for_status()
                result = ET.fromstring(response.content)
            except (httpx.HTTPError, ET.ParseError) as exc:
                raise CloudUploadError("移动云盘凭据续期失败，请稍后重试或更新 Authorization") from exc
            token = result.findtext("token", "")
            if result.findtext("return") != "0" or not token:
                raise CloudUploadError("移动云盘拒绝凭据续期，请重新登录网页并更新 Authorization")
            authorization = base64.b64encode(f"{self._prefix}:{self.account}:{token}".encode()).decode()
            prefix, account, token, expires_at = self._parse_authorization(authorization)
            if expires_at <= time.time():
                raise CloudUploadError("移动云盘续期返回了已过期的凭据")
            self.authorization = authorization
            self._prefix, self.account, self._token, self._expires_at = prefix, account, token, expires_at
            self._next_refresh_at = min(time.time() + 12 * 3600, expires_at - 60)
            if self._on_credential_update is not None:
                await self._on_credential_update({"authorization": authorization})

    async def _post(self, url: str, body: dict[str, object], *, personal: bool = True) -> dict[str, object]:
        await self._ensure_authorization()
        text = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        timestamp = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
        nonce = secrets.token_hex(8)
        device_info = "||9|7.14.0|chrome|120.0.0.0|||windows 10||zh-CN|||"
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json;charset=UTF-8",
            "Authorization": f"Basic {self.authorization}",
            "Cms-Device": "default",
            "Mcloud-Channel": "1000101",
            "Mcloud-Client": "10701",
            "Mcloud-Sign": f"{timestamp},{nonce},{yun139_signature(text, timestamp, nonce)}",
            "Mcloud-Version": "7.14.0",
            "Origin": "https://yun.139.com",
            "Referer": "https://yun.139.com/w/",
            "x-DeviceInfo": device_info,
            "x-huawei-channelSrc": "10000034",
            "x-inner-ntwk": "2",
            "x-m4c-caller": "PC",
            "x-m4c-src": "10002",
            "x-SvcType": "1",
        }
        if personal:
            client = "13" if self.client_type == "pc" else "9"
            headers.update({
                "Caller": "web",
                "Mcloud-Route": "001",
                "X-Yun-Api-Version": "v1",
                "X-Yun-App-Channel": "10000034",
                "X-Yun-Channel-Source": "10000034",
                "X-Yun-Client-Info": f"||{client}|7.14.0|chrome|120.0.0.0|||windows 10||zh-CN|||dW5kZWZpbmVk||",
                "X-Yun-Module-Type": "100",
                "X-Yun-Svc-Type": "1",
            })
        else:
            headers["Inner-Hcy-Router-Https"] = "1"
        try:
            response = await self._api_client.post(url, content=text.encode(), headers=headers)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise CloudUploadError("移动云盘 API 请求失败或响应格式无效") from exc
        if not isinstance(payload, dict) or payload.get("success") is not True:
            code = payload.get("code", "未知") if isinstance(payload, dict) else "未知"
            raise CloudUploadError(f"移动云盘 API 拒绝请求（{code}），请检查凭据和网盘权限")
        data = payload.get("data")
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise CloudUploadError("移动云盘 API 响应缺少 data")
        return data

    @staticmethod
    def _https_url(value: object) -> str:
        url = str(value or "")
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise CloudUploadError("移动云盘返回了无效的 HTTPS 服务地址")
        return url

    async def _api_request(self, path: str, body: dict[str, object]) -> dict[str, object]:
        async with self._route_lock:
            if not self._personal_host:
                data = await self._post(
                    _ROUTE_URL,
                    {"userInfo": {"userType": 1, "accountType": 1, "accountName": self.account}, "modAddrType": 1},
                    personal=False,
                )
                policies = data.get("routePolicyList")
                if not isinstance(policies, list):
                    raise CloudUploadError("移动云盘路由响应缺少服务列表")
                for policy in policies:
                    if isinstance(policy, dict) and policy.get("modName") == "personal":
                        self._personal_host = self._https_url(policy.get("httpsUrl")).rstrip("/")
                        break
                if not self._personal_host:
                    raise CloudUploadError("移动云盘未返回新版个人云服务地址")
        return await self._post(self._personal_host + path, body)

    async def _list_directory(self, parent_id: str) -> list[RemoteEntry]:
        entries: list[RemoteEntry] = []
        cursor = ""
        seen: set[str] = set()
        while True:
            data = await self._api_request("/file/list", {
                "imageThumbnailStyleList": ["Small", "Large"],
                "orderBy": "updated_at",
                "orderDirection": "DESC",
                "pageInfo": {"pageCursor": cursor, "pageSize": 100},
                "parentFileId": parent_id,
            })
            items = data.get("items")
            if not isinstance(items, list):
                raise CloudUploadError("移动云盘目录响应缺少文件列表")
            for item in items:
                try:
                    if not isinstance(item, dict) or not item.get("fileId") or not item.get("name"):
                        raise ValueError
                    is_directory = item.get("type") == "folder"
                    size = 0 if is_directory else int(item["size"])
                    if size < 0 or item.get("type") not in {"file", "folder"}:
                        raise ValueError
                    entries.append(RemoteEntry(str(item["fileId"]), str(item["name"]), size, is_directory))
                except (KeyError, TypeError, ValueError) as exc:
                    raise CloudUploadError("移动云盘返回了无效的文件信息") from exc
            cursor = str(data.get("nextPageCursor") or "")
            if not cursor:
                return entries
            if cursor in seen:
                raise CloudUploadError("移动云盘目录分页游标重复")
            seen.add(cursor)

    async def _walk_directories(self, parts: list[str], *, create: bool) -> str | None:
        parent_id = self.root_id
        for name in parts:
            entry = next((item for item in await self._list_directory(parent_id) if item.name == name), None)
            if entry is None and create:
                try:
                    await self._api_request("/file/create", {
                        "parentFileId": parent_id, "name": name, "description": "",
                        "type": "folder", "fileRenameMode": "force_rename",
                    })
                except CloudUploadError:
                    # Another uploader may have created the same folder.
                    entry = next((item for item in await self._list_directory(parent_id) if item.name == name), None)
                    if entry is None or not entry.is_directory:
                        raise
                else:
                    entry = next((item for item in await self._list_directory(parent_id) if item.name == name), None)
                    if entry is None:
                        raise CloudUploadError(f"移动云盘创建目录后无法找到目录：{name}")
            if entry is None:
                return None
            if not entry.is_directory:
                raise CloudUploadError(f"移动云盘远端路径冲突，{name} 已存在且不是目录")
            parent_id = entry.id
        return parent_id

    async def remote_size(self, remote_path: str) -> int | None:
        parts, name = split_remote_file(remote_path)
        parent_id = await self._walk_directories(parts, create=False)
        if parent_id is None:
            return None
        entry = next((item for item in await self._list_directory(parent_id) if item.name == name), None)
        if entry is None:
            return None
        if entry.is_directory:
            raise CloudUploadError(f"移动云盘远端路径冲突，目标是目录：{remote_path}")
        return entry.size

    @staticmethod
    def _hash_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _part_size(size: int) -> int:
        return (512 if size // (1024 ** 3) > 30 else 100) * 1024 * 1024

    @staticmethod
    async def _content(path: Path, offset: int, size: int, progress: UploadProgress | None) -> AsyncIterator[bytes]:
        with path.open("rb") as stream:
            stream.seek(offset)
            transferred = 0
            while transferred < size:
                chunk = await asyncio.to_thread(stream.read, min(1024 * 1024, size - transferred))
                if not chunk:
                    raise CloudUploadError("移动云盘上传时本地文件被截断")
                transferred += len(chunk)
                if progress is not None:
                    progress("uploading", offset + transferred)
                yield chunk

    async def _upload_parts(
        self, path: Path, parts: list[dict[str, object]], urls: object, progress: UploadProgress | None,
    ) -> None:
        if not isinstance(urls, list) or len(urls) != len(parts):
            raise CloudUploadError("移动云盘返回的分片上传地址数量不完整")
        by_number = {}
        for item in urls:
            if not isinstance(item, dict) or type(item.get("partNumber")) is not int:
                raise CloudUploadError("移动云盘返回了无效的分片编号")
            number = item["partNumber"]
            if number in by_number:
                raise CloudUploadError("移动云盘返回了重复的分片编号")
            by_number[number] = self._https_url(item.get("uploadUrl"))
        if set(by_number) != {part["partNumber"] for part in parts}:
            raise CloudUploadError("移动云盘返回的分片编号不匹配")
        for part in parts:
            size = int(part["partSize"])
            offset = int(part["parallelHashCtx"]["partOffset"])
            for attempt in range(3):
                if progress is not None:
                    progress("uploading", offset)
                try:
                    response = await self._transfer_client.put(
                        by_number[part["partNumber"]],
                        content=self._content(path, offset, size, progress),
                        headers={
                            "Content-Type": "application/octet-stream", "Content-Length": str(size),
                            "Origin": "https://yun.139.com", "Referer": "https://yun.139.com/",
                        },
                    )
                    response.raise_for_status()
                    break
                except httpx.HTTPError as exc:
                    retryable = not isinstance(exc, httpx.HTTPStatusError) or exc.response.status_code in {
                        408, 429, 500, 502, 503, 504,
                    }
                    if attempt == 2 or not retryable:
                        raise CloudUploadError(f"移动云盘第 {part['partNumber']} 个分片上传失败") from exc
                    await self._sleep(2 ** attempt)

    async def _upload(self, path: Path, parent_id: str, name: str, progress: UploadProgress | None) -> None:
        size = path.stat().st_size
        digest = await asyncio.to_thread(self._hash_file, path)
        part_size = self._part_size(size)
        parts = [
            {"partNumber": number + 1, "partSize": min(part_size, size - offset),
             "parallelHashCtx": {"partOffset": offset}}
            for number, offset in enumerate(range(0, size, part_size))
        ]
        data = await self._api_request("/file/create", {
            "contentHash": digest, "contentHashAlgorithm": "SHA256", "contentType": "application/octet-stream",
            "parallelUpload": False, "partInfos": parts[:100], "size": size, "parentFileId": parent_id,
            "name": name, "type": "file", "fileRenameMode": "auto_rename",
        })
        if data.get("fileName") and data["fileName"] != name:
            raise CloudUploadError("移动云盘目标出现同名文件，已停止归档，请检查远端文件冲突")
        if data.get("exist") is True:
            return
        if data.get("rapidUpload") is True and not data.get("partInfos"):
            return
        file_id, upload_id = data.get("fileId"), data.get("uploadId")
        if not file_id or not upload_id or not parts:
            raise CloudUploadError("移动云盘上传响应缺少文件或上传任务 ID")
        await self._upload_parts(path, parts[:100], data.get("partInfos"), progress)
        for start in range(100, len(parts), 100):
            batch = parts[start:start + 100]
            urls = await self._api_request("/file/getUploadUrl", {
                "fileId": file_id, "uploadId": upload_id, "partInfos": batch,
                "commonAccountInfo": {"account": self.account, "accountType": 1},
            })
            await self._upload_parts(path, batch, urls.get("partInfos"), progress)
        await self._api_request("/file/complete", {
            "contentHash": digest, "contentHashAlgorithm": "SHA256", "fileId": file_id, "uploadId": upload_id,
        })

    async def upload_verified(
        self, local_path: Path, remote_path: str, *, progress: UploadProgress | None = None,
    ) -> bool:
        size = local_path.stat().st_size
        if progress is not None:
            progress("preparing", 0)
        existing_size = await self.remote_size(remote_path)
        if existing_size == size:
            if progress is not None:
                progress("verifying", size)
            return False
        if existing_size is not None:
            raise CloudUploadError(f"移动云盘远端文件已存在但大小不一致：{remote_path}")
        parts, name = split_remote_file(remote_path)
        parent_id = await self._walk_directories(parts, create=True)
        assert parent_id is not None
        await self._upload(local_path, parent_id, name, progress)
        if progress is not None:
            progress("verifying", size)
        for attempt in range(5):
            uploaded_size = await self.remote_size(remote_path)
            if uploaded_size == size:
                return True
            if attempt < 4:
                await self._sleep(1)
        raise CloudUploadError(
            f"移动云盘上传后文件大小校验失败：{remote_path}（本地 {size}，远端 {uploaded_size}）"
        )

    async def aclose(self) -> None:
        await asyncio.gather(self._api_client.aclose(), self._transfer_client.aclose())
