from __future__ import annotations

import asyncio
import logging
import secrets
import time
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

_ACCOUNT_BASE_URL = "https://account.guangyapan.com"
_API_BASE_URL = "https://api.guangyapan.com"
_Sleep = Callable[[float], Awaitable[None]]


class GuangYaPanClient:
    """光鸭网盘 API adapter with refreshable bearer credentials and OSS upload."""

    def __init__(
        self,
        access_token: str = "",
        refresh_token: str = "",
        client_id: str = "",
        device_id: str = "",
        *,
        root_id: str = "",
        timeout_seconds: int = 300,
        on_credential_update: CredentialUpdate | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: _Sleep = asyncio.sleep,
        account_base_url: str = _ACCOUNT_BASE_URL,
        api_base_url: str = _API_BASE_URL,
    ) -> None:
        self.access_token = access_token.strip()
        self.refresh_token = refresh_token.strip()
        self.client_id = client_id.strip()
        self.device_id = self._normalize_device_id(device_id)
        if not self.device_id:
            self.device_id = secrets.token_hex(16)
        self.root_id = root_id.strip()
        self._account_base_url = account_base_url.rstrip("/")
        self._api_base_url = api_base_url.rstrip("/")
        self._timeout_seconds = max(30, timeout_seconds)
        self._on_credential_update = on_credential_update
        self._sleep = sleep
        self._refresh_state = CredentialRefreshCoordinator()
        self._device_save_lock = asyncio.Lock()
        self._device_saved = False
        self._last_api_request: dict[str, float] = {}
        self._api_rate_lock = asyncio.Lock()
        self._client = httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(float(timeout_seconds)),
            headers={
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "X-Device-Model": "chrome%2F147.0.0.0",
                "X-Device-Name": "PC-Chrome",
                "X-Device-Sign": f"wdi10.{self.device_id}",
                "X-Device-Id": self.device_id,
                "X-Net-Work-Type": "NONE",
                "X-OS-Version": "MacIntel",
                "X-Platform-Version": "1",
                "X-Protocol-Version": "301",
                "X-Provider-Name": "NONE",
                "X-SDK-Version": "9.0.2",
                "X-Client-Id": self.client_id,
                "X-Client-Version": "0.0.1",
            },
            transport=transport,
        )

    @staticmethod
    def _normalize_device_id(value: str) -> str:
        value = value.strip().lower().replace("-", "")
        if len(value) != 32 or any(char not in "0123456789abcdef" for char in value):
            return ""
        return value

    @staticmethod
    def _detail(response: httpx.Response) -> str:
        return " ".join(response.text.split())[:300]

    @staticmethod
    def _json_object(response: httpx.Response, operation: str) -> dict[str, object]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise CloudUploadError(f"光鸭网盘{operation}返回无效 JSON：{GuangYaPanClient._detail(response)}") from exc
        if not isinstance(payload, dict):
            raise CloudUploadError(f"光鸭网盘{operation}返回格式错误")
        return payload

    async def _save_credentials(self) -> None:
        if self._on_credential_update is not None:
            await self._on_credential_update(
                {
                    "access_token": self.access_token,
                    "refresh_token": self.refresh_token,
                    "client_id": self.client_id,
                    "device_id": self.device_id,
                }
            )

    async def _ensure_device_saved(self) -> None:
        if self._device_saved:
            return
        async with self._device_save_lock:
            if self._device_saved:
                return
            await self._save_credentials()
            self._device_saved = True

    async def _refresh_access_token(self, *, rejected_generation: int | None = None) -> None:
        async def refresh() -> None:
            if not self.refresh_token:
                raise CloudUploadError("光鸭网盘没有可用的 Access Token 或 Refresh Token")
            try:
                response = await self._client.post(
                    f"{self._account_base_url}/v1/auth/token",
                    json={
                        "client_id": self.client_id,
                        "grant_type": "refresh_token",
                        "refresh_token": self.refresh_token,
                    },
                )
            except httpx.HTTPError as exc:
                raise CloudUploadError(f"光鸭网盘刷新 Token 请求失败：{exc}") from exc
            payload = self._json_object(response, "刷新 Token")
            access_token = payload.get("access_token")
            refresh_token = payload.get("refresh_token")
            if response.is_error or payload.get("error") or not isinstance(access_token, str) or not access_token:
                message = payload.get("error_description") or payload.get("error") or self._detail(response)
                raise CloudUploadError(f"光鸭网盘刷新 Token 失败：{message}")
            self.access_token = access_token.strip()
            if isinstance(refresh_token, str) and refresh_token.strip():
                self.refresh_token = refresh_token.strip()
            await self._save_credentials()

        await self._refresh_state.refresh(rejected_generation, refresh)

    async def _ensure_access_token(self) -> None:
        await self._ensure_device_saved()
        if not self.access_token:
            await self._refresh_access_token(rejected_generation=self._refresh_state.generation)

    async def _throttle(self, path: str) -> None:
        async with self._api_rate_lock:
            now = time.monotonic()
            previous = self._last_api_request.get(path)
            if previous is not None:
                delay = 0.5 - (now - previous)
                if delay > 0:
                    await self._sleep(delay)
            self._last_api_request[path] = time.monotonic()

    @staticmethod
    def _success(payload: dict[str, object]) -> bool:
        try:
            code = int(payload.get("code", 0))
        except (TypeError, ValueError):
            code = -1
        message = str(payload.get("msg") or "").strip().lower()
        return code in {0, 200} or message in {"", "success"}

    async def _api_request(
        self,
        path: str,
        body: dict[str, object] | None = None,
        *,
        accepted_codes: frozenset[int] = frozenset(),
        accepted_messages: frozenset[str] = frozenset(),
    ) -> dict[str, object]:
        await self._ensure_access_token()
        for auth_attempt in range(2):
            await self._throttle(path)
            access_token = self.access_token
            token_generation = self._refresh_state.generation
            headers = {
                "Authorization": f"Bearer {access_token}",
                "Did": self.device_id,
                "Dt": "4",
            }
            try:
                response = await self._client.post(
                    f"{self._api_base_url}{path}",
                    headers=headers,
                    json=body,
                )
            except httpx.HTTPError as exc:
                raise CloudUploadError(f"光鸭网盘接口 {path} 请求失败：{exc}") from exc
            payload = self._json_object(response, f"接口 {path}")
            if response.status_code in {401, 403} and auth_attempt == 0 and self.refresh_token:
                await self._refresh_access_token(rejected_generation=token_generation)
                continue
            try:
                response_code = int(payload.get("code", 0))
            except (TypeError, ValueError):
                response_code = -1
            response_message = str(payload.get("msg") or "").strip().lower()
            accepted = response_code in accepted_codes or response_message in accepted_messages
            if response.is_error or (not self._success(payload) and not accepted):
                message = payload.get("msg") or payload.get("error_description") or self._detail(response)
                raise CloudUploadError(f"光鸭网盘接口 {path} 失败（HTTP {response.status_code}）：{message}")
            return payload
        raise CloudUploadError("光鸭网盘 Token 刷新后仍无法访问接口")

    async def _list_directory(self, parent_id: str) -> list[RemoteEntry]:
        entries: list[RemoteEntry] = []
        page_size = 100
        for page in range(10000):
            payload = await self._api_request(
                "/nd.bizuserres.s/v1/file/get_file_list",
                {"parentId": parent_id, "page": page, "pageSize": page_size, "orderBy": 3, "sortType": 1},
            )
            data = payload.get("data")
            if not isinstance(data, dict) or not isinstance(data.get("list"), list):
                raise CloudUploadError("光鸭网盘目录列表响应缺少 data.list")
            raw_entries = data["list"]
            for raw in raw_entries:
                if not isinstance(raw, dict):
                    continue
                try:
                    entries.append(
                        RemoteEntry(
                            id=str(raw["fileId"]),
                            name=str(raw["fileName"]),
                            size=int(raw.get("fileSize", 0)),
                            is_directory=int(raw.get("resType", 1)) == 2,
                        )
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    raise CloudUploadError("光鸭网盘目录列表包含无效文件记录") from exc
            try:
                total = int(data.get("total", len(entries)) or 0)
            except (TypeError, ValueError) as exc:
                raise CloudUploadError("光鸭网盘目录列表响应包含无效总数") from exc
            if len(raw_entries) < page_size or (total and len(entries) >= total):
                return entries
        raise CloudUploadError("光鸭网盘目录列表分页超过安全上限")

    @staticmethod
    def _named_entry(entries: list[RemoteEntry], name: str) -> RemoteEntry | None:
        return next((entry for entry in entries if entry.name == name), None)

    async def _walk_directories(self, parts: list[str], *, create: bool) -> str | None:
        parent_id = self.root_id
        for name in parts:
            entry = self._named_entry(await self._list_directory(parent_id), name)
            if entry is not None:
                if not entry.is_directory:
                    raise CloudUploadError(f"光鸭网盘远端路径冲突，{name} 已存在且不是目录")
                parent_id = entry.id
                continue
            if not create:
                return None
            try:
                payload = await self._api_request(
                    "/nd.bizuserres.s/v1/file/create_dir",
                    {"parentId": parent_id, "dirName": name},
                )
            except CloudUploadError:
                entry = self._named_entry(await self._list_directory(parent_id), name)
                if entry is None or not entry.is_directory:
                    raise
                parent_id = entry.id
                continue
            data = payload.get("data")
            created_id = data.get("fileId") if isinstance(data, dict) else None
            if created_id:
                parent_id = str(created_id)
                continue
            entry = self._named_entry(await self._list_directory(parent_id), name)
            if entry is None or not entry.is_directory:
                raise CloudUploadError(f"光鸭网盘创建目录后无法找到目录：{name}")
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
            raise CloudUploadError(f"光鸭网盘远端路径冲突，目标是目录：{remote_path}")
        return entry.size

    async def _get_upload_token(self, parent_id: str, name: str, size: int) -> tuple[dict[str, object], bool]:
        payload = await self._api_request(
            "/nd.bizuserres.s/v1/get_res_center_token",
            {"capacity": 2, "name": name, "parentId": parent_id, "res": {"fileSize": size}},
            accepted_codes=frozenset({156}),
            accepted_messages=frozenset({"上传已完成", "upload completed", "already uploaded", "秒传成功"}),
        )
        data = payload.get("data")
        if not isinstance(data, dict):
            raise CloudUploadError("光鸭网盘上传 Token 响应缺少 data")
        try:
            code = int(payload.get("code", 0))
        except (TypeError, ValueError):
            code = 0
        message = str(payload.get("msg") or "").strip().lower()
        already_done = code == 156 or message in {"上传已完成", "upload completed", "already uploaded", "秒传成功"}
        if not self._success(payload) and not already_done:
            raise CloudUploadError(f"光鸭网盘获取上传 Token 失败：{payload.get('msg') or code}")
        if not data.get("taskId"):
            raise CloudUploadError("光鸭网盘上传 Token 缺少任务 ID")
        creds = data.get("creds")
        if isinstance(creds, dict):
            for target, aliases in (
                ("accessKeyID", ("accessKeyID", "AccessKeyID")),
                ("secretAccessKey", ("secretAccessKey", "SecretAccessKey")),
                ("sessionToken", ("sessionToken", "SessionToken")),
            ):
                if not data.get(target):
                    for alias in aliases:
                        if creds.get(alias):
                            data[target] = creds[alias]
                            break
        for target, aliases in (
            ("accessKeyID", ("AccessKeyID",)),
            ("secretAccessKey", ("SecretAccessKey",)),
            ("sessionToken", ("SecurityToken", "SessionToken")),
        ):
            if not data.get(target):
                for alias in aliases:
                    if data.get(alias):
                        data[target] = data[alias]
                        break
        if not data.get("endPoint") and data.get("fullEndPoint"):
            data["endPoint"] = data["fullEndPoint"]
        return data, already_done

    @staticmethod
    def _oss_credentials(token: dict[str, object]) -> OssCredentials:
        nested = token.get("creds") if isinstance(token.get("creds"), dict) else {}
        expiration = next(
            (
                source.get(key)
                for source in (token, nested)
                for key in ("Expiration", "expiration", "expiresAt", "expires_at", "expireTime", "expire_time")
                if source.get(key) is not None
            ),
            None,
        )
        return OssCredentials(
            access_key_id=str(token.get("accessKeyID") or ""),
            access_key_secret=str(token.get("secretAccessKey") or ""),
            security_token=str(token.get("sessionToken") or ""),
            expires_at=parse_oss_expiration(expiration),
            endpoint=str(token.get("endPoint") or ""),
        )

    async def _wait_upload_task(self, task_id: str) -> None:
        deadline = time.monotonic() + self._timeout_seconds
        while time.monotonic() < deadline:
            payload = await self._api_request(
                "/nd.bizuserres.s/v1/file/get_info_by_task_id",
                {"taskId": task_id},
                accepted_codes=frozenset({145, 146, 147, 155, 163}),
            )
            data = payload.get("data")
            if isinstance(data, dict) and data.get("fileId"):
                return
            try:
                code = int(payload.get("code", 0))
            except (TypeError, ValueError):
                code = 0
            if code not in {0, 145, 146, 147, 155, 163}:
                raise CloudUploadError(f"光鸭网盘上传任务失败：{payload.get('msg') or code}")
            await self._sleep(1)
        raise CloudUploadError(f"光鸭网盘上传任务超时：{task_id}")

    @staticmethod
    def _part_size(size: int) -> int:
        mb = 1024 * 1024
        gb = 1024 * mb
        if size <= 100 * mb:
            return 1 * mb
        if size <= 16 * gb:
            return 2 * mb
        if size <= 160 * gb:
            return 4 * mb
        return 8 * mb

    async def _upload(self, local_path: Path, parent_id: str, filename: str, progress: UploadProgress | None) -> None:
        size = local_path.stat().st_size
        token, already_done = await self._get_upload_token(parent_id, filename, size)
        task_id = str(token.get("taskId") or "")
        if already_done:
            await self._wait_upload_task(task_id)
            return
        credentials = self._oss_credentials(token)
        bucket = str(token.get("bucketName") or "")
        object_path = str(token.get("objectPath") or "")
        if not all(
            (
                credentials.endpoint,
                bucket,
                credentials.access_key_id,
                credentials.access_key_secret,
                object_path,
            )
        ):
            raise CloudUploadError("光鸭网盘上传 Token 缺少 OSS 参数")

        async def refresh_oss_credentials() -> OssCredentials:
            refreshed, refreshed_done = await self._get_upload_token(parent_id, filename, size)
            if refreshed_done:
                raise CloudUploadError("光鸭网盘在 OSS 续期时将上传任务标记为已完成")
            stable_fields = {
                "taskId": task_id,
                "bucketName": bucket,
                "objectPath": object_path,
            }
            changed = [
                key for key, expected in stable_fields.items() if str(refreshed.get(key) or "") != expected
            ]
            if changed:
                raise CloudUploadError(
                    f"光鸭网盘 OSS 续期返回了不同上传任务，无法安全续传：{', '.join(changed)}"
                )
            return self._oss_credentials(refreshed)

        oss = AliyunOssUploader(
            self._client,
            endpoint=credentials.endpoint,
            bucket=bucket,
            access_key_id=credentials.access_key_id,
            access_key_secret=credentials.access_key_secret,
            security_token=credentials.security_token,
            expires_at=credentials.expires_at,
            credential_provider=refresh_oss_credentials,
            sleep=self._sleep,
        )
        await oss.upload(local_path, object_path, part_size=self._part_size(size), progress=progress)
        if task_id:
            await self._wait_upload_task(task_id)

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
            logger.info("光鸭网盘远端文件已存在且大小一致，跳过重复上传：%s", remote_path)
            if progress is not None:
                progress("verifying", local_size)
            return False
        if existing_size is not None:
            raise CloudUploadError(
                f"光鸭网盘远端文件已存在但大小不一致：{remote_path}（本地 {local_size}，远端 {existing_size}）"
            )
        directory_parts, filename = split_remote_file(remote_path)
        parent_id = await self._walk_directories(directory_parts, create=True)
        if parent_id is None:
            raise CloudUploadError("光鸭网盘无法确定上传目录")
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
        raise CloudUploadError(
            f"光鸭网盘上传后文件大小校验失败：{remote_path}（本地 {local_size}，远端 {uploaded_size}）"
        )

    async def aclose(self) -> None:
        await self._client.aclose()
