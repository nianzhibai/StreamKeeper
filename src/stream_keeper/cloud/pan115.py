from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx

from .base import (
    CloudUploadError,
    CredentialRefreshCoordinator,
    CredentialUpdate,
    RemoteEntry,
    UploadProgress,
    split_remote_file,
)
from .oss import AliyunOssUploader, OssCredentials, parse_oss_expiration

logger = logging.getLogger(__name__)

_API_BASE_URL = "https://proapi.115.com"
_PASSPORT_BASE_URL = "https://passportapi.115.com"
_PART_SIZE = 20 * 1024 * 1024
_Sleep = Callable[[float], Awaitable[None]]


class Pan115Client:
    """115 Open API client with token refresh and callback-based OSS upload."""

    def __init__(
        self,
        access_token: str = "",
        refresh_token: str = "",
        *,
        root_id: str = "0",
        timeout_seconds: int = 300,
        on_credential_update: CredentialUpdate | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: _Sleep = asyncio.sleep,
        api_base_url: str = _API_BASE_URL,
        passport_base_url: str = _PASSPORT_BASE_URL,
    ) -> None:
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.root_id = root_id
        self._api_base_url = api_base_url.rstrip("/")
        self._passport_base_url = passport_base_url.rstrip("/")
        self._on_credential_update = on_credential_update
        self._sleep = sleep
        self._refresh_state = CredentialRefreshCoordinator()
        self._client = httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(float(timeout_seconds)),
            headers={"Accept": "application/json", "User-Agent": "StreamKeeper/0.6"},
            transport=transport,
        )

    @staticmethod
    def _detail(response: httpx.Response) -> str:
        return " ".join(response.text.split())[:300]

    @staticmethod
    def _json_object(response: httpx.Response, operation: str) -> dict[str, object]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise CloudUploadError(f"115 {operation}返回无效 JSON：{Pan115Client._detail(response)}") from exc
        if not isinstance(payload, dict):
            raise CloudUploadError(f"115 {operation}返回格式错误")
        return payload

    async def _save_credentials(self) -> None:
        if self._on_credential_update is not None:
            await self._on_credential_update({"access_token": self.access_token, "refresh_token": self.refresh_token})

    @staticmethod
    def _is_auth_expired(payload: dict[str, object]) -> bool:
        try:
            code = int(payload.get("code", 0))
        except (TypeError, ValueError):
            return False
        return code == 99 or str(code).startswith("401")

    async def _refresh_access_token(self, *, rejected_generation: int | None = None) -> None:
        async def refresh() -> None:
            if not self.refresh_token:
                raise CloudUploadError("115 Access Token 已失效，且没有 Refresh Token")
            try:
                response = await self._client.post(
                    f"{self._passport_base_url}/open/refreshToken",
                    data={"refresh_token": self.refresh_token},
                )
            except httpx.HTTPError as exc:
                raise CloudUploadError(f"115 刷新 Token 请求失败：{exc}") from exc
            payload = self._json_object(response, "刷新 Token")
            data = payload.get("data")
            data = data if isinstance(data, dict) else {}
            access_token = data.get("access_token")
            refresh_token = data.get("refresh_token")
            try:
                code = int(payload.get("code", 0) or 0)
            except (TypeError, ValueError):
                code = -1
            if response.is_error or code != 0 or not isinstance(access_token, str) or not access_token:
                message = payload.get("message") or payload.get("error") or self._detail(response)
                raise CloudUploadError(f"115 刷新 Token 失败：{message}")
            self.access_token = access_token
            if isinstance(refresh_token, str) and refresh_token:
                self.refresh_token = refresh_token
            await self._save_credentials()

        await self._refresh_state.refresh(rejected_generation, refresh)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str | int] | None = None,
        form: dict[str, str] | None = None,
        raw: bool = False,
    ) -> dict[str, object]:
        if not self.access_token:
            await self._refresh_access_token(rejected_generation=self._refresh_state.generation)
        for auth_attempt in range(2):
            access_token = self.access_token
            token_generation = self._refresh_state.generation
            try:
                response = await self._client.request(
                    method,
                    f"{self._api_base_url}{path}",
                    params=params,
                    data=form,
                    headers={"Authorization": f"Bearer {access_token}"},
                )
            except httpx.HTTPError as exc:
                raise CloudUploadError(f"115 接口 {path} 请求失败：{exc}") from exc
            payload = self._json_object(response, f"接口 {path}")
            try:
                state = int(payload.get("state", 0))
            except (TypeError, ValueError):
                state = 0
            if (response.is_error or state != 1) and auth_attempt == 0 and self._is_auth_expired(payload):
                await self._refresh_access_token(rejected_generation=token_generation)
                continue
            if response.is_error or state != 1:
                message = payload.get("message") or payload.get("error") or ""
                raise CloudUploadError(f"115 接口 {path} 失败（HTTP {response.status_code}）：{message}")
            return payload
        raise CloudUploadError("115 Token 刷新后仍无法访问接口")

    async def _list_directory(self, parent_id: str) -> list[RemoteEntry]:
        entries: list[RemoteEntry] = []
        offset = 0
        limit = 1150
        while True:
            payload = await self._request(
                "GET",
                "/open/ufile/files",
                params={
                    "cid": parent_id,
                    "limit": limit,
                    "offset": offset,
                    "asc": "1",
                    "o": "file_name",
                    "show_dir": "1",
                },
                raw=True,
            )
            raw_entries = payload.get("data")
            if not isinstance(raw_entries, list):
                raise CloudUploadError("115 目录列表响应缺少 data")
            for raw in raw_entries:
                if not isinstance(raw, dict):
                    continue
                try:
                    entries.append(
                        RemoteEntry(
                            id=str(raw["fid"]),
                            name=str(raw["fn"]),
                            size=int(raw.get("fs", 0)),
                            is_directory=str(raw.get("fc", "1")) == "0",
                        )
                    )
                except (KeyError, TypeError, ValueError) as exc:
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
        parent_id = self.root_id
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
                payload = await self._request(
                    "POST",
                    "/open/folder/add",
                    form={"pid": parent_id, "file_name": name},
                )
            except CloudUploadError:
                entry = self._named_entry(await self._list_directory(parent_id), name)
                if entry is None or not entry.is_directory:
                    raise
                parent_id = entry.id
                continue
            data = payload.get("data")
            created_id = data.get("file_id") if isinstance(data, dict) else None
            if created_id:
                parent_id = str(created_id)
                continue
            entry = self._named_entry(await self._list_directory(parent_id), name)
            if entry is None or not entry.is_directory:
                raise CloudUploadError(f"115 创建目录后无法找到目录：{name}")
            parent_id = entry.id
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

    @staticmethod
    def _callback(data: object) -> tuple[str, str] | None:
        if isinstance(data, list):
            data = data[0] if data else None
        if not isinstance(data, dict):
            return None
        body = data.get("callback")
        variables = data.get("callback_var")
        if isinstance(body, str) and isinstance(variables, str):
            return body, variables
        return None

    async def _get_oss_credentials(self) -> OssCredentials:
        token_payload = await self._request("GET", "/open/upload/get_token")
        token = token_payload.get("data")
        if not isinstance(token, dict):
            raise CloudUploadError("115 上传 Token 响应缺少 data")
        endpoint = str(token.get("endpoint") or "")
        access_key_id = str(token.get("AccessKeyId") or token.get("AccessKeyID") or "")
        access_key_secret = str(token.get("AccessKeySecret") or "")
        security_token = str(token.get("SecurityToken") or "")
        if not all((endpoint, access_key_id, access_key_secret)):
            raise CloudUploadError("115 上传响应缺少 OSS 凭据")
        expiration = next(
            (
                token.get(key)
                for key in ("Expiration", "expiration", "expires_at", "expire_time")
                if token.get(key) is not None
            ),
            None,
        )
        return OssCredentials(
            access_key_id=access_key_id,
            access_key_secret=access_key_secret,
            security_token=security_token,
            expires_at=parse_oss_expiration(expiration),
            endpoint=endpoint,
        )

    async def _upload(self, local_path: Path, parent_id: str, filename: str, progress: UploadProgress | None) -> None:
        size = local_path.stat().st_size
        full_hash, prefix_hash = await asyncio.to_thread(self._hash_file, local_path)
        payload = await self._request(
            "POST",
            "/open/upload/init",
            form={
                "file_name": filename,
                "file_size": str(size),
                "target": f"U_1_{parent_id}",
                "fileid": full_hash,
                "preid": prefix_hash,
                "pick_code": "",
                "topupload": "",
                "sign_key": "",
                "sign_val": "",
            },
        )
        data = payload.get("data")
        if not isinstance(data, dict):
            raise CloudUploadError("115 上传初始化响应缺少 data")
        try:
            status = int(data.get("status", 0) or 0)
        except (TypeError, ValueError) as exc:
            raise CloudUploadError("115 上传初始化响应包含无效状态") from exc
        if status == 2:
            if progress is not None:
                progress("uploading", size)
            return
        if status in {6, 7, 8}:
            sign_check = str(data.get("sign_check") or "")
            try:
                start_text, end_text = sign_check.split("-", 1)
                start, end = int(start_text), int(end_text)
            except (ValueError, TypeError) as exc:
                raise CloudUploadError("115 上传初始化返回了无效二次校验范围") from exc
            if start < 0 or end < start or end >= size:
                raise CloudUploadError("115 上传二次校验范围超出本地文件")
            with local_path.open("rb") as stream:
                stream.seek(start)
                sign_bytes = await asyncio.to_thread(stream.read, end - start + 1)
            sign_val = hashlib.sha1(sign_bytes).hexdigest().upper()
            payload = await self._request(
                "POST",
                "/open/upload/init",
                form={
                    "file_name": filename,
                    "file_size": str(size),
                    "target": f"U_1_{parent_id}",
                    "fileid": full_hash,
                    "preid": prefix_hash,
                    "pick_code": "",
                    "topupload": "",
                    "sign_key": str(data.get("sign_key") or ""),
                    "sign_val": sign_val,
                },
            )
            data = payload.get("data")
            if not isinstance(data, dict):
                raise CloudUploadError("115 二次校验响应缺少 data")
            try:
                status = int(data.get("status", 0) or 0)
            except (TypeError, ValueError) as exc:
                raise CloudUploadError("115 二次校验响应包含无效状态") from exc
            if status == 2:
                if progress is not None:
                    progress("uploading", size)
                return
        if status != 1:
            raise CloudUploadError(f"115 上传初始化返回了未知状态：{status}")

        credentials = await self._get_oss_credentials()
        bucket = str(data.get("bucket") or "")
        object_key = str(data.get("object") or "")
        if not all((bucket, object_key)):
            raise CloudUploadError("115 上传响应缺少 OSS 参数")
        oss = AliyunOssUploader(
            self._client,
            endpoint=credentials.endpoint,
            bucket=bucket,
            access_key_id=credentials.access_key_id,
            access_key_secret=credentials.access_key_secret,
            security_token=credentials.security_token,
            expires_at=credentials.expires_at,
            credential_provider=self._get_oss_credentials,
            sleep=self._sleep,
        )
        await oss.upload(
            local_path,
            object_key,
            part_size=_PART_SIZE,
            callback=self._callback(data.get("callback")),
            progress=progress,
        )

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
