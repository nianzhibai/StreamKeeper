from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import mimetypes
import random
import re
import secrets
import string
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime
from pathlib import Path

import httpx
from Crypto.Cipher import AES

from .base import CloudUploadError, CredentialUpdate, RemoteEntry, UploadProgress, split_remote_file

logger = logging.getLogger(__name__)

_BASE_URL = "https://panservice.mail.wo.cn"
_DEFAULT_UPLOAD_URL = "https://tjupload.pan.wo.cn"
_CLIENT_ID = "1001000021"
_CLIENT_SECRET = "XFmi9GS2hzk98jGX"
_APP_ID = "10000001"
_IV = b"wNSOYIB1k1DjY5lA"
_PART_SIZE = 8 * 1024 * 1024
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/114.0.0.0 Safari/537.36 Edg/114.0.1823.37"
)
_VIDEO_FILE_TYPES = frozenset({".flv", ".mkv", ".mp4", ".ts"})
_Sleep = Callable[[float], Awaitable[None]]
# Control characters plus the reserved set most cloud drives reject.
_INVALID_NAME_CHARS = re.compile(r'[\x00-\x1f\x7f\\/:*?"<>|]')
# Emoji and the rest of the supplementary plane are rejected by the WoPan API
# with code=1009 名称中含有非法字符, so fold them into "_". CJK and ASCII
# letters/digits are safe.
_ASTRAL_CHARS = re.compile(r"[\U00010000-\U0010FFFF]")


class WoPanClient:
    """Native China Unicom WoPan encrypted API and upload2C client."""

    def __init__(
        self,
        access_token: str = "",
        refresh_token: str = "",
        *,
        root_id: str = "0",
        family_id: str = "",
        timeout_seconds: int = 300,
        on_credential_update: CredentialUpdate | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: _Sleep = asyncio.sleep,
    ) -> None:
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.root_id = root_id
        self.family_id = family_id
        self._on_credential_update = on_credential_update
        self._sleep = sleep
        self._zone_url: str | None = None
        self._refresh_lock = asyncio.Lock()
        self._client = httpx.AsyncClient(
            follow_redirects=False,
            headers={"User-Agent": _USER_AGENT},
            timeout=httpx.Timeout(float(timeout_seconds)),
            transport=transport,
        )

    @property
    def _space_type(self) -> str:
        return "1" if self.family_id else "0"

    def _aes_key(self, channel: str) -> bytes:
        if channel == "api-user":
            return _CLIENT_SECRET.encode()
        token = self.access_token.encode()
        if len(token) < 16:
            raise CloudUploadError("联通云盘 access token 无效，且无法加密请求")
        return token[:16]

    def _encrypt(self, value: object, channel: str) -> str:
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        padding = AES.block_size - len(raw) % AES.block_size
        padded = raw + bytes([padding]) * padding
        encrypted = AES.new(self._aes_key(channel), AES.MODE_CBC, iv=_IV).encrypt(padded)
        return base64.b64encode(encrypted).decode()

    def _decrypt(self, value: str, channel: str) -> object:
        try:
            encrypted = base64.b64decode(value, validate=True)
            padded = AES.new(self._aes_key(channel), AES.MODE_CBC, iv=_IV).decrypt(encrypted)
            padding = padded[-1]
            if padding < 1 or padding > AES.block_size or padded[-padding:] != bytes([padding]) * padding:
                raise ValueError("invalid PKCS#7 padding")
            return json.loads(padded[:-padding])
        except (ValueError, IndexError, json.JSONDecodeError) as exc:
            raise CloudUploadError("联通云盘返回的数据解密失败") from exc

    @staticmethod
    def _response_detail(response: httpx.Response) -> str:
        return " ".join(response.text.split())[:300]

    async def _ensure_access_token(self) -> None:
        if len(self.access_token.encode()) >= 16:
            return
        if not self.refresh_token:
            raise CloudUploadError("联通云盘没有可用的 access token 或 refresh token")
        await self._refresh_access_token(force=False)

    async def _refresh_access_token(self, *, force: bool = True) -> None:
        async with self._refresh_lock:
            if not force and len(self.access_token.encode()) >= 16:
                return
            if not self.refresh_token:
                raise CloudUploadError("联通云盘 access token 已失效，但未配置 refresh token")
            data = await self._dispatcher(
                "api-user",
                "AppRefreshToken",
                {"refreshToken": self.refresh_token, "clientSecret": _CLIENT_SECRET},
                {"clientId": _CLIENT_ID, "secret": True},
                allow_refresh=False,
            )
            if not isinstance(data, dict):
                raise CloudUploadError("联通云盘刷新令牌响应格式错误")
            access_token = str(data.get("access_token") or "")
            refresh_token = str(data.get("refresh_token") or "")
            if len(access_token.encode()) < 16 or not refresh_token:
                raise CloudUploadError("联通云盘刷新令牌响应缺少新 token")
            self.access_token = access_token
            self.refresh_token = refresh_token
            if self._on_credential_update is not None:
                await self._on_credential_update(
                    {"access_token": self.access_token, "refresh_token": self.refresh_token}
                )

    async def _dispatcher(
        self,
        channel: str,
        key: str,
        param: dict[str, object],
        other: dict[str, object],
        *,
        allow_refresh: bool = True,
    ) -> object:
        if channel != "api-user":
            await self._ensure_access_token()
        timestamp = int(time.time() * 1000)
        request_sequence = random.randint(100000, 108998)
        version = ""
        sign_input = f"{key}{timestamp}{request_sequence}{channel}{version}".encode()
        body = dict(other)
        body["param"] = self._encrypt(param, channel)
        headers = {
            "Origin": "https://pan.wo.cn",
            "Referer": "https://pan.wo.cn/",
        }
        if self.access_token:
            headers["Accesstoken"] = self.access_token
        request_body = {
            "header": {
                "key": key,
                "resTime": timestamp,
                "reqSeq": request_sequence,
                "channel": channel,
                "sign": hashlib.md5(sign_input).hexdigest(),
                "version": version,
            },
            "body": body,
        }
        try:
            response = await self._client.post(
                f"{_BASE_URL}/{channel}/dispatcher",
                headers=headers,
                json=request_body,
            )
        except httpx.HTTPError as exc:
            raise CloudUploadError(f"联通云盘接口 {key} 请求失败：{exc}") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise CloudUploadError(f"联通云盘接口 {key} 返回无效 JSON：{self._response_detail(response)}") from exc
        if response.is_error or not isinstance(payload, dict) or str(payload.get("STATUS")) != "200":
            message = payload.get("MSG") if isinstance(payload, dict) else self._response_detail(response)
            raise CloudUploadError(f"联通云盘接口 {key} 失败（HTTP {response.status_code}）：{message}")
        result = payload.get("RSP")
        if not isinstance(result, dict):
            raise CloudUploadError(f"联通云盘接口 {key} 响应缺少 RSP")
        response_code = str(result.get("RSP_CODE") or "")
        if channel != "api-user" and allow_refresh and response_code == "9999":
            await self._refresh_access_token()
            return await self._dispatcher(channel, key, param, other, allow_refresh=False)
        if response_code != "0000":
            raise CloudUploadError(f"联通云盘接口 {key} 失败（code={response_code}）：{result.get('RSP_DESC') or ''}")
        data = result.get("DATA")
        if isinstance(data, str):
            if not data:
                return None
            return self._decrypt(data, channel)
        return data

    async def _list_directory(self, parent_id: str) -> list[RemoteEntry]:
        entries: list[RemoteEntry] = []
        page = 0
        page_size = 100
        while True:
            param: dict[str, object] = {
                "spaceType": self._space_type,
                "parentDirectoryId": parent_id,
                "pageNum": page,
                "pageSize": page_size,
                "sortRule": 1,
                "clientId": _CLIENT_ID,
            }
            if self.family_id:
                param["familyId"] = self.family_id
            data = await self._dispatcher("wohome", "QueryAllFiles", param, {"secret": True})
            if not isinstance(data, dict) or not isinstance(data.get("files"), list):
                raise CloudUploadError("联通云盘目录列表响应缺少 files")
            raw_files = data["files"]
            for raw in raw_files:
                if not isinstance(raw, dict):
                    continue
                try:
                    entries.append(
                        RemoteEntry(
                            id=str(raw["id"]),
                            name=str(raw["name"]),
                            size=int(raw.get("size", 0)),
                            is_directory=int(raw.get("type", 1)) == 0,
                        )
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    raise CloudUploadError("联通云盘目录列表包含无效文件记录") from exc
            if len(raw_files) < page_size:
                return entries
            page += 1

    @staticmethod
    def _named_entry(entries: list[RemoteEntry], name: str) -> RemoteEntry | None:
        return next((entry for entry in entries if entry.name == name), None)

    @staticmethod
    def _sanitize_name(value: str, fallback: str = "未命名") -> str:
        """Fold characters the WoPan API rejects into "_".

        Anchor names may carry emoji (e.g. ``兔兔兔奶糖🍬``) that CreateDirectory
        answers with code=1009 名称中含有非法字符. Everything else, including CJK,
        is kept so remote names stay recognizable.
        """

        cleaned = _INVALID_NAME_CHARS.sub("_", value)
        cleaned = _ASTRAL_CHARS.sub("_", cleaned)
        cleaned = re.sub(r"_+", "_", cleaned).strip(" ._")
        return cleaned or fallback

    async def _walk_directories(self, parts: list[str], *, create: bool) -> str | None:
        parent_id = self.root_id
        for name in parts:
            entry = self._named_entry(await self._list_directory(parent_id), name)
            if entry is not None:
                if not entry.is_directory:
                    raise CloudUploadError(f"联通云盘远端路径冲突，{name} 已存在且不是目录")
                parent_id = entry.id
                continue
            if not create:
                return None
            param: dict[str, object] = {
                "spaceType": self._space_type,
                "parentDirectoryId": parent_id,
                "directoryName": name,
                "clientId": _CLIENT_ID,
            }
            # Personal space rejects the key outright: an empty familyId answers 9999 系统异常.
            if self.family_id:
                param["familyId"] = self.family_id
            try:
                data = await self._dispatcher("wohome", "CreateDirectory", param, {"secret": True})
            except CloudUploadError:
                entry = self._named_entry(await self._list_directory(parent_id), name)
                if entry is None or not entry.is_directory:
                    raise
                parent_id = entry.id
                continue
            created_id = data.get("id") if isinstance(data, dict) else None
            if created_id:
                parent_id = str(created_id)
                continue
            entry = self._named_entry(await self._list_directory(parent_id), name)
            if entry is None or not entry.is_directory:
                raise CloudUploadError(f"联通云盘创建目录后无法找到目录：{name}")
            parent_id = entry.id
        return parent_id

    async def remote_size(self, remote_path: str) -> int | None:
        directory_parts, filename = split_remote_file(remote_path)
        directory_parts = [self._sanitize_name(part) for part in directory_parts]
        filename = self._sanitize_name(filename, fallback="recording")
        parent_id = await self._walk_directories(directory_parts, create=False)
        if parent_id is None:
            return None
        entry = self._named_entry(await self._list_directory(parent_id), filename)
        if entry is None:
            return None
        if entry.is_directory:
            raise CloudUploadError(f"联通云盘远端路径冲突，目标是目录：{remote_path}")
        return entry.size

    async def _get_zone_url(self) -> str:
        if self._zone_url is not None:
            return self._zone_url
        try:
            data = await self._dispatcher(
                "wohome",
                "GetZoneInfo",
                {"appId": _APP_ID},
                {"key": True},
            )
            zone_url = str(data.get("url") or "") if isinstance(data, dict) else ""
            if not zone_url.startswith("https://"):
                raise CloudUploadError("联通云盘 GetZoneInfo 未返回 HTTPS 地址")
            self._zone_url = zone_url.rstrip("/")
        except CloudUploadError as exc:
            logger.warning("获取联通云盘上传区域失败，使用默认区域：%s", exc)
            self._zone_url = _DEFAULT_UPLOAD_URL
        return self._zone_url

    @staticmethod
    def _multipart_prefix(fields: dict[str, str], boundary: str, filename: str, content_type: str) -> bytes:
        chunks: list[bytes] = []
        for name, value in fields.items():
            chunks.append(
                (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n').encode()
            )
        safe_filename = filename.replace("\r", "_").replace("\n", "_").replace('"', "%22")
        chunks.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="file"; filename="{safe_filename}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n"
            ).encode()
        )
        return b"".join(chunks)

    @staticmethod
    async def _multipart_content(
        local_path: Path,
        offset: int,
        size: int,
        prefix: bytes,
        suffix: bytes,
        on_streamed: Callable[[int], None] | None = None,
    ) -> AsyncIterator[bytes]:
        yield prefix
        stream = local_path.open("rb")
        remaining = size
        try:
            stream.seek(offset)
            while remaining:
                chunk = await asyncio.to_thread(stream.read, min(1024 * 1024, remaining))
                if not chunk:
                    raise CloudUploadError(f"读取录像分片时提前到达文件末尾：{local_path}")
                remaining -= len(chunk)
                if on_streamed is not None:
                    on_streamed(size - remaining)
                yield chunk
        finally:
            stream.close()
        yield suffix

    async def _upload_part(
        self,
        upload_url: str,
        local_path: Path,
        *,
        fields: dict[str, str],
        filename: str,
        content_type: str,
        offset: int,
        size: int,
        part_index: int,
        progress: UploadProgress | None = None,
    ) -> None:
        on_streamed = None
        if progress is not None:
            # Each attempt restarts the part, so the reported total rewinds with it.
            def on_streamed(streamed: int) -> None:
                progress("uploading", offset + streamed)

        last_error: Exception | None = None
        for attempt in range(3):
            boundary = f"----DouYinStreamKeeper{secrets.token_hex(12)}"
            prefix = self._multipart_prefix(fields, boundary, filename, content_type)
            suffix = f"\r\n--{boundary}--\r\n".encode()
            try:
                response = await self._client.post(
                    upload_url,
                    headers={
                        "Content-Length": str(len(prefix) + size + len(suffix)),
                        "Content-Type": f"multipart/form-data; boundary={boundary}",
                        "Origin": "https://pan.wo.cn",
                        "Referer": "https://pan.wo.cn/",
                    },
                    content=self._multipart_content(local_path, offset, size, prefix, suffix, on_streamed),
                )
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise CloudUploadError(
                        f"联通云盘第 {part_index} 片返回无效 JSON：{self._response_detail(response)}"
                    ) from exc
                if response.is_error or not isinstance(payload, dict) or str(payload.get("code")) != "0000":
                    message = payload.get("msg") if isinstance(payload, dict) else self._response_detail(response)
                    raise CloudUploadError(
                        f"联通云盘第 {part_index} 片上传失败（HTTP {response.status_code}）：{message}"
                    )
                return
            except (CloudUploadError, httpx.HTTPError) as exc:
                last_error = exc
                if attempt < 2:
                    await self._sleep(2**attempt)
        assert last_error is not None
        if isinstance(last_error, CloudUploadError):
            raise last_error
        raise CloudUploadError(f"联通云盘第 {part_index} 片上传失败：{last_error}") from last_error

    @staticmethod
    def _file_type(filename: str) -> str:
        return "2" if Path(filename).suffix.lower() in _VIDEO_FILE_TYPES else "5"

    async def _upload(
        self,
        local_path: Path,
        filename: str,
        parent_id: str,
        progress: UploadProgress | None = None,
    ) -> None:
        if progress is not None:
            progress("preparing", 0)
        await self._ensure_access_token()
        size = local_path.stat().st_size
        file_info: dict[str, object] = {
            "spaceType": self._space_type,
            "directoryId": parent_id,
            "batchNo": datetime.now().strftime("%Y%m%d%H%M%S"),
            "fileName": filename,
            "fileSize": size,
            "fileType": self._file_type(filename),
        }
        if self.family_id:
            file_info["familyId"] = self.family_id
        encrypted_file_info = self._encrypt(file_info, "wohome")
        total_parts = max(size // _PART_SIZE, 1)
        unique_id = f"{int(time.time() * 1000)}_{''.join(secrets.choice(string.ascii_letters) for _ in range(6))}"
        base_fields = {
            "uniqueId": unique_id,
            "accessToken": self.access_token,
            "fileName": filename,
            "psToken": "undefined",
            "fileSize": str(size),
            "totalPart": str(total_parts),
            "channel": "wocloud",
            "directoryId": parent_id,
            "fileInfo": encrypted_file_info,
        }
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        upload_url = f"{await self._get_zone_url()}/openapi/client/upload2C"
        offset = 0
        for part_index in range(1, total_parts + 1):
            part_size = _PART_SIZE if part_index < total_parts else size - offset
            fields = dict(base_fields)
            fields["partSize"] = str(part_size)
            fields["partIndex"] = str(part_index)
            await self._upload_part(
                upload_url,
                local_path,
                fields=fields,
                filename=filename,
                content_type=content_type,
                offset=offset,
                size=part_size,
                part_index=part_index,
                progress=progress,
            )
            offset += part_size
            if progress is not None:
                progress("uploading", offset)

    async def upload_verified(
        self,
        local_path: Path,
        remote_path: str,
        *,
        progress: UploadProgress | None = None,
    ) -> bool:
        local_size = local_path.stat().st_size
        if progress is not None:
            progress("preparing", 0)
        existing_size = await self.remote_size(remote_path)
        if existing_size == local_size:
            logger.info("联通云盘远端文件已存在且大小一致，跳过重复上传：%s", remote_path)
            if progress is not None:
                progress("verifying", local_size)
            return False
        if existing_size is not None:
            raise CloudUploadError(
                f"联通云盘远端文件已存在但大小不一致：{remote_path}（本地 {local_size}，远端 {existing_size}）"
            )

        directory_parts, filename = split_remote_file(remote_path)
        directory_parts = [self._sanitize_name(part) for part in directory_parts]
        filename = self._sanitize_name(filename, fallback="recording")
        sanitized_path = "/" + "/".join([*directory_parts, filename])
        if sanitized_path != remote_path:
            logger.info("联通云盘不支持远端名称中的部分字符，已替换后上传：%s -> %s", remote_path, sanitized_path)
        parent_id = await self._walk_directories(directory_parts, create=True)
        assert parent_id is not None
        await self._upload(local_path, filename, parent_id, progress)
        if progress is not None:
            progress("verifying", local_size)
        uploaded_size: int | None = None
        for attempt in range(5):
            uploaded_size = await self.remote_size(remote_path)
            if uploaded_size == local_size:
                return True
            if attempt < 4:
                await self._sleep(1)
        raise CloudUploadError(
            f"联通云盘上传后文件大小校验失败：{remote_path}（本地 {local_size}，远端 {uploaded_size}）"
        )

    async def aclose(self) -> None:
        await self._client.aclose()
