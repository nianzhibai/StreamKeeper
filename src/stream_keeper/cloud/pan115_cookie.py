from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import struct
import time
import zlib
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import urlencode

import httpx
from Crypto.Cipher import AES
from Crypto.PublicKey import ECC

from .base import CloudUploadError, CredentialUpdate, RemoteEntry, UploadProgress, split_remote_file
from .oss import AliyunOssUploader

logger = logging.getLogger(__name__)

_API_FILE_LIST = "https://webapi.115.com/files"
_API_DIR_ADD = "https://webapi.115.com/files/add"
_API_UPLOAD_INFO = "https://proapi.115.com/app/uploadinfo"
_API_UPLOAD_INIT = "https://uplb.115.com/4.0/initupload.php"
_API_OSS_TOKEN = "https://uplb.115.com/3.0/gettoken.php"
_OSS_ENDPOINT = "https://cn-shenzhen.oss.aliyuncs.com"
_APP_VERSION = "27.0.5.7"
_MD5_SALT = "Qclm8MGWUv59TnrR0XPg"
_REMOTE_PUBLIC_KEY = bytes(
    (
        0x57,
        0xA2,
        0x92,
        0x57,
        0xCD,
        0x23,
        0x20,
        0xE5,
        0xD6,
        0xD1,
        0x43,
        0x32,
        0x2F,
        0xA4,
        0xBB,
        0x8A,
        0x3C,
        0xF9,
        0xD3,
        0xCC,
        0x62,
        0x3E,
        0xF5,
        0xED,
        0xAC,
        0x62,
        0xB7,
        0x67,
        0x8A,
        0x89,
        0xC9,
        0x1A,
        0x83,
        0xBA,
        0x80,
        0x0D,
        0x61,
        0x29,
        0xF5,
        0x22,
        0xD0,
        0x34,
        0xC8,
        0x95,
        0xDD,
        0x24,
        0x65,
        0x24,
        0x3A,
        0xDD,
        0xC2,
        0x50,
        0x95,
        0x3B,
        0xEE,
        0xBA,
    )
)
_CRC_SALT = b"^j>WD3Kr?J2gLFjD4W2y@"
_LZ4_OUTPUT_LIMIT = 0x2000
_Sleep = Callable[[float], Awaitable[None]]


def _lz4_block_decompress(data: bytes) -> bytes:
    """Decode the raw LZ4 block used by the legacy 115 upload endpoint."""

    output = bytearray()
    index = 0
    while index < len(data):
        token = data[index]
        index += 1
        literal_length = token >> 4
        if literal_length == 15:
            while True:
                if index >= len(data):
                    raise CloudUploadError("115 上传接口返回了不完整的 LZ4 字面量长度")
                extra = data[index]
                index += 1
                literal_length += extra
                if extra != 255:
                    break
        if index + literal_length > len(data):
            raise CloudUploadError("115 上传接口返回了损坏的压缩数据")
        if len(output) + literal_length > _LZ4_OUTPUT_LIMIT:
            raise CloudUploadError("115 上传接口返回的解压数据过大")
        output.extend(data[index : index + literal_length])
        index += literal_length
        if index >= len(data):
            break
        if index + 2 > len(data):
            raise CloudUploadError("115 上传接口返回了不完整的 LZ4 数据")
        offset = data[index] | (data[index + 1] << 8)
        index += 2
        if offset <= 0 or offset > len(output):
            raise CloudUploadError("115 上传接口返回了无效的 LZ4 回溯距离")
        match_length = token & 0x0F
        if match_length == 15:
            while True:
                if index >= len(data):
                    raise CloudUploadError("115 上传接口返回了不完整的 LZ4 匹配长度")
                extra = data[index]
                index += 1
                match_length += extra
                if extra != 255:
                    break
        match_length += 4
        if len(output) + match_length > _LZ4_OUTPUT_LIMIT:
            raise CloudUploadError("115 上传接口返回的解压数据过大")
        for _ in range(match_length):
            output.append(output[-offset])
    return bytes(output)


class _Ec115Cipher:
    def __init__(self) -> None:
        remote = ECC.construct(
            curve="P-224",
            point_x=int.from_bytes(_REMOTE_PUBLIC_KEY[:28], "big"),
            point_y=int.from_bytes(_REMOTE_PUBLIC_KEY[28:], "big"),
        )
        private = ECC.generate(curve="P-224")
        shared = remote.pointQ * private.d
        x_value = int(shared.x)
        secret = x_value.to_bytes(max(1, (x_value.bit_length() + 7) // 8), "big")
        if len(secret) < 16:
            secret = secret.rjust(16, b"\0")
        self.key = secret[:16]
        self.iv = secret[-16:]
        x_bytes = int(private.pointQ.x).to_bytes(28, "big")
        prefix = b"\x03" if int(private.pointQ.y) & 1 else b"\x02"
        self.public_key = b"\x1d" + prefix + x_bytes

    def encrypt(self, value: bytes) -> bytes:
        padding = AES.block_size - len(value) % AES.block_size
        value += bytes([padding]) * padding
        block = AES.new(self.key, AES.MODE_ECB)
        xor_key = self.iv
        output = bytearray()
        for offset in range(0, len(value), AES.block_size):
            mixed = bytes(value[offset + i] ^ xor_key[i] for i in range(AES.block_size))
            xor_key = block.encrypt(mixed)
            output.extend(xor_key)
        return bytes(output)

    def decrypt(self, value: bytes) -> bytes:
        value = value[: len(value) - len(value) % AES.block_size]
        if not value:
            raise CloudUploadError("115 上传接口返回了空加密响应")
        decrypted = AES.new(self.key, AES.MODE_CBC, iv=self.iv).decrypt(value)
        compressed_length = int.from_bytes(decrypted[:2], "little")
        if compressed_length <= 0 or compressed_length > len(decrypted) - 2:
            raise CloudUploadError("115 上传接口返回了无效压缩数据长度")
        compressed = decrypted[2 : 2 + compressed_length]
        return _lz4_block_decompress(compressed)

    def encode_token(self, timestamp: int) -> str:
        timestamp_bytes = struct.pack(">I", timestamp & 0xFFFFFFFF)
        r1 = secrets_byte()
        r2 = secrets_byte()
        data = bytearray()
        for value in _Ec115Cipher._public_first(self.public_key):
            data.append(value ^ r1)
        data.extend((r1, 0x73 ^ r1, r1, r1, r1))
        data.extend(r1 ^ timestamp_bytes[3 - index] for index in range(4))
        for value in self.public_key[15:]:
            data.append(value ^ r2)
        data.extend((r2, 0x01 ^ r2, r2, r2, r2))
        crc = zlib.crc32(_CRC_SALT + data) & 0xFFFFFFFF
        data.extend(struct.pack(">I", crc)[::-1])
        return base64.b64encode(data).decode()

    @staticmethod
    def _public_first(value: bytes) -> bytes:
        return value[:15]


def secrets_byte() -> int:
    # os.urandom avoids exposing a predictable token in this legacy protocol.
    import os

    return os.urandom(1)[0]


class Pan115CookieClient:
    """Legacy 115 web API client authenticated by UID/CID/SEID/KID cookies."""

    def __init__(
        self,
        cookie: str,
        *,
        root_id: str = "0",
        timeout_seconds: int = 300,
        on_credential_update: CredentialUpdate | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: _Sleep = asyncio.sleep,
    ) -> None:
        self.cookie = cookie.strip()
        self.root_id = root_id or "0"
        self._on_credential_update = on_credential_update
        self._sleep = sleep
        self._user_id = ""
        self._user_key = ""
        self._upload_size_limit = 0
        self._client = httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(float(timeout_seconds)),
            headers={
                "Accept": "application/json, text/plain, */*",
                "Cookie": self.cookie,
                "User-Agent": f"Mozilla/5.0 115Browser/{_APP_VERSION}",
            },
            transport=transport,
        )

    @staticmethod
    def _detail(response: httpx.Response) -> str:
        return " ".join(response.text.split())[:300]

    async def _json(self, response: httpx.Response, operation: str) -> dict[str, object]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise CloudUploadError(f"115 {operation}返回无效 JSON：{self._detail(response)}") from exc
        if not isinstance(payload, dict):
            raise CloudUploadError(f"115 {operation}返回格式错误")
        state = payload.get("state")
        if response.is_error or state is False or state == 0:
            detail = payload.get("msg") or payload.get("message") or self._detail(response)
            raise CloudUploadError(f"115 {operation}失败：{detail}")
        return payload

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str | int] | None = None,
        form: dict[str, str] | None = None,
    ) -> dict[str, object]:
        try:
            response = await self._client.request(method, url, params=params, data=form)
        except httpx.HTTPError as exc:
            raise CloudUploadError(f"115 请求失败：{exc}") from exc
        return await self._json(response, url)

    async def _upload_info(self) -> None:
        payload = await self._request("POST", _API_UPLOAD_INFO)
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        self._user_id = str(data.get("user_id") or data.get("userid") or "")
        self._user_key = str(data.get("userkey") or data.get("user_key") or "")
        try:
            self._upload_size_limit = int(data.get("size_limit") or 0)
        except (TypeError, ValueError) as exc:
            raise CloudUploadError("115 上传信息响应包含无效大小限制") from exc
        if not self._user_id or not self._user_key:
            raise CloudUploadError("115 上传信息响应缺少用户签名参数")
        if data.get("upload_allowed") is False:
            raise CloudUploadError(str(data.get("upload_allowed_msg") or "115 账号当前不允许上传"))

    async def _ensure_upload_info(self) -> None:
        if not self._user_id or not self._user_key:
            await self._upload_info()

    async def _list_directory(self, parent_id: str) -> list[RemoteEntry]:
        entries: list[RemoteEntry] = []
        offset = 0
        limit = 1150
        while True:
            payload = await self._request(
                "GET",
                _API_FILE_LIST,
                params={
                    "aid": "1",
                    "cid": parent_id or "0",
                    "o": "user_ptime",
                    "asc": "1",
                    "offset": offset,
                    "show_dir": "1",
                    "limit": limit,
                    "snap": "0",
                    "natsort": "0",
                    "record_open_time": "1",
                    "format": "json",
                    "fc_mix": "0",
                },
            )
            raw_entries = payload.get("data")
            if not isinstance(raw_entries, list):
                raise CloudUploadError("115 目录列表响应缺少 data")
            for raw in raw_entries:
                if not isinstance(raw, dict):
                    continue
                file_id = str(raw.get("fid") or "")
                directory_id = str(raw.get("cid") or raw.get("aid") or "")
                try:
                    entries.append(
                        RemoteEntry(
                            id=file_id or directory_id,
                            name=str(raw.get("n") or ""),
                            size=int(raw.get("s", 0) or 0),
                            is_directory=not bool(file_id),
                        )
                    )
                except (TypeError, ValueError) as exc:
                    raise CloudUploadError("115 目录列表包含无效文件记录") from exc
            try:
                total = int(payload["count"]) if "count" in payload else None
            except (TypeError, ValueError) as exc:
                raise CloudUploadError("115 目录列表响应包含无效总数") from exc
            if len(raw_entries) < limit or (total is not None and len(entries) >= total):
                return entries
            offset += limit

    @staticmethod
    def _named_entry(entries: list[RemoteEntry], name: str) -> RemoteEntry | None:
        return next((entry for entry in entries if entry.name == name), None)

    async def _walk_directories(self, parts: list[str], *, create: bool) -> str | None:
        parent_id = self.root_id or "0"
        for name in parts:
            entry = self._named_entry(await self._list_directory(parent_id), name)
            if entry is not None:
                if not entry.is_directory:
                    raise CloudUploadError(f"115 远端路径冲突，{name} 已存在且不是目录")
                parent_id = entry.id
                continue
            if not create:
                return None
            try:
                payload = await self._request("POST", _API_DIR_ADD, form={"pid": parent_id, "cname": name})
            except CloudUploadError:
                entry = self._named_entry(await self._list_directory(parent_id), name)
                if entry is None or not entry.is_directory:
                    raise
                parent_id = entry.id
                continue
            data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
            created_id = data.get("cid") or data.get("file_id")
            if not created_id:
                raise CloudUploadError(f"115 创建目录响应缺少目录 ID：{name}")
            parent_id = str(created_id)
        return parent_id

    async def remote_size(self, remote_path: str) -> int | None:
        directory_parts, filename = split_remote_file(remote_path)
        parent_id = await self._walk_directories(directory_parts, create=False)
        if parent_id is None:
            return None
        entry = self._named_entry(await self._list_directory(parent_id), filename)
        if entry is None:
            return None
        if entry.is_directory:
            raise CloudUploadError(f"115 远端路径冲突，目标是目录：{remote_path}")
        return entry.size

    @staticmethod
    def _hash_file(path: Path) -> tuple[str, str]:
        full = hashlib.sha1()
        prefix = hashlib.sha1()
        remaining = 128 * 1024
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                full.update(chunk)
                if remaining:
                    piece = chunk[:remaining]
                    prefix.update(piece)
                    remaining -= len(piece)
        return full.hexdigest().upper(), prefix.hexdigest().upper()

    def _signature(self, file_id: str, target: str) -> str:
        first = hashlib.sha1(f"{self._user_id}{file_id}{target}0".encode()).hexdigest()
        return hashlib.sha1(f"{self._user_key}{first}000000".encode()).hexdigest().upper()

    def _token(self, file_id: str, pre_id: str, timestamp: str, size: str, sign_key: str, sign_val: str) -> str:
        user_md5 = hashlib.md5(self._user_id.encode()).hexdigest()
        value = f"{_MD5_SALT}{file_id}{size}{sign_key}{sign_val}{self._user_id}{timestamp}{user_md5}{_APP_VERSION}"
        return hashlib.md5(value.encode()).hexdigest()

    async def _upload_init(
        self,
        filename: str,
        size: int,
        parent_id: str,
        file_id: str,
        pre_id: str,
        *,
        cipher: _Ec115Cipher,
        sign_key: str = "",
        sign_val: str = "",
    ) -> dict[str, object]:
        target = f"U_1_{parent_id}"
        timestamp = int(time.time() * 1000)
        timestamp_text = str(timestamp)
        form = {
            "appid": "0",
            "appversion": _APP_VERSION,
            "userid": self._user_id,
            "filename": filename,
            "filesize": str(size),
            "fileid": file_id,
            "target": target,
            "sig": self._signature(file_id, target),
            "topupload": "true",
            "t": timestamp_text,
            "token": self._token(file_id, pre_id, timestamp_text, str(size), sign_key, sign_val),
        }
        if sign_key and sign_val:
            form["sign_key"] = sign_key
            form["sign_val"] = sign_val
        encrypted = cipher.encrypt(urlencode(form).encode())
        encoded_token = cipher.encode_token(timestamp)
        try:
            response = await self._client.post(
                _API_UPLOAD_INIT,
                params={"k_ec": encoded_token},
                content=encrypted,
                headers={"Content-Type": "application/x-www-form-urlencoded", "Content-Length": str(len(encrypted))},
            )
        except httpx.HTTPError as exc:
            raise CloudUploadError(f"115 上传初始化请求失败：{exc}") from exc
        if response.is_error:
            raise CloudUploadError(f"115 上传初始化失败（HTTP {response.status_code}）：{self._detail(response)}")
        try:
            decoded = cipher.decrypt(response.content)
            payload = json.loads(decoded)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise CloudUploadError("115 上传初始化响应解密失败") from exc
        if not isinstance(payload, dict):
            raise CloudUploadError("115 上传初始化响应格式错误")
        try:
            status = int(payload.get("status", 0) or 0)
        except (TypeError, ValueError) as exc:
            raise CloudUploadError("115 上传初始化响应包含无效状态") from exc
        if status not in {1, 2, 7}:
            raise CloudUploadError(str(payload.get("statusmsg") or "115 上传初始化被拒绝"))
        return payload

    @staticmethod
    async def _hash_range(path: Path, start: int, end: int) -> str:
        size = path.stat().st_size
        if start < 0 or end < start or end >= size:
            raise CloudUploadError("115 上传二次校验范围超出本地文件")
        with path.open("rb") as stream:
            stream.seek(start)
            value = await asyncio.to_thread(stream.read, end - start + 1)
        return hashlib.sha1(value).hexdigest().upper()

    async def _upload(self, local_path: Path, parent_id: str, filename: str, progress: UploadProgress | None) -> None:
        await self._ensure_upload_info()
        size = local_path.stat().st_size
        if self._upload_size_limit and size > self._upload_size_limit:
            raise CloudUploadError("115 账号限制了该录像的上传大小")
        file_id, pre_id = await asyncio.to_thread(self._hash_file, local_path)
        cipher = _Ec115Cipher()
        sign_key = ""
        sign_val = ""
        for _ in range(3):
            payload = await self._upload_init(
                filename,
                size,
                parent_id,
                file_id,
                pre_id,
                cipher=cipher,
                sign_key=sign_key,
                sign_val=sign_val,
            )
            try:
                status = int(payload.get("status", 0) or 0)
            except (TypeError, ValueError) as exc:
                raise CloudUploadError("115 上传初始化响应包含无效状态") from exc
            if status == 2:
                if progress is not None:
                    progress("uploading", size)
                return
            if status != 7:
                break
            sign_key = str(payload.get("sign_key") or "")
            sign_check = str(payload.get("sign_check") or "")
            try:
                start_text, end_text = sign_check.split("-", 1)
                start, end = int(start_text), int(end_text)
            except (ValueError, TypeError) as exc:
                raise CloudUploadError("115 上传初始化返回了无效二次校验范围") from exc
            sign_val = await self._hash_range(local_path, start, end)
        else:
            raise CloudUploadError("115 上传二次校验次数过多")

        token_payload = await self._request("GET", _API_OSS_TOKEN)
        token = token_payload.get("data") if isinstance(token_payload.get("data"), dict) else token_payload
        if not isinstance(token, dict):
            raise CloudUploadError("115 OSS Token 响应格式错误")
        token_status = token.get("StatusCode") or token_payload.get("StatusCode")
        if token_status is not None and str(token_status) != "200":
            raise CloudUploadError(f"115 OSS Token 获取失败：{token_status}")
        data = payload
        bucket = str(data.get("bucket") or "")
        object_key = str(data.get("object") or "")
        callback = data.get("callback")
        if isinstance(callback, list):
            callback = callback[0] if callback else None
        callback_pair = None
        if (
            isinstance(callback, dict)
            and isinstance(callback.get("callback"), str)
            and isinstance(callback.get("callback_var"), str)
        ):
            callback_pair = (callback["callback"], callback["callback_var"])
        access_key = str(token.get("AccessKeyID") or token.get("AccessKeyId") or "")
        access_secret = str(token.get("AccessKeySecret") or "")
        security_token = str(token.get("SecurityToken") or "")
        if not all((bucket, object_key, access_key, access_secret)):
            raise CloudUploadError("115 OSS 上传参数不完整")
        oss = AliyunOssUploader(
            self._client,
            endpoint=_OSS_ENDPOINT,
            bucket=bucket,
            access_key_id=access_key,
            access_key_secret=access_secret,
            security_token=security_token,
            sleep=self._sleep,
        )
        await oss.upload(local_path, object_key, part_size=20 * 1024 * 1024, callback=callback_pair, progress=progress)

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
            logger.info("115 远端文件已存在且大小一致，跳过重复上传：%s", remote_path)
            if progress is not None:
                progress("verifying", local_size)
            return False
        if existing_size is not None:
            raise CloudUploadError(
                f"115 远端文件已存在但大小不一致：{remote_path}（本地 {local_size}，远端 {existing_size}）"
            )
        directory_parts, filename = split_remote_file(remote_path)
        parent_id = await self._walk_directories(directory_parts, create=True)
        if parent_id is None:
            raise CloudUploadError("115 无法确定上传目录")
        await self._upload(local_path, parent_id, filename, progress)
        if progress is not None:
            progress("verifying", local_size)
        uploaded_size: int | None = None
        for attempt in range(5):
            uploaded_size = await self.remote_size(remote_path)
            if uploaded_size == local_size:
                return True
            if attempt < 4:
                await self._sleep(1)
        raise CloudUploadError(f"115 上传后文件大小校验失败：{remote_path}（本地 {local_size}，远端 {uploaded_size}）")

    async def aclose(self) -> None:
        await self._client.aclose()
