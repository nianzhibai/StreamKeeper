from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import json
import logging
import mimetypes
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import quote

import httpx

from .base import CloudUploadError, CredentialUpdate, RemoteEntry, UploadProgress, split_remote_file

logger = logging.getLogger(__name__)

_API_BASE_URL = "https://drive.quark.cn/1/clouddrive"
_REFERER = "https://pan.quark.cn"
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "quark-cloud-drive/2.5.20 Chrome/100.0.4896.160 Electron/18.3.5.4-b478491100 "
    "Safari/537.36 Channel/pckk_other_ch"
)
_OSS_USER_AGENT = "aliyun-sdk-js/6.6.1 Chrome 98.0.4758.80 on Windows 10 64-bit"
_Sleep = Callable[[float], Awaitable[None]]


class QuarkClient:
    """Native Quark cookie API and OSS multipart uploader."""

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
        self.cookie = cookie
        self.root_id = root_id
        self._on_credential_update = on_credential_update
        self._sleep = sleep
        # Quark rotates cookies in ordinary responses. Serializing API calls
        # prevents a slower, older response from overwriting a newer rotation.
        self._api_request_lock = asyncio.Lock()
        timeout = httpx.Timeout(float(timeout_seconds))
        self._api_client = httpx.AsyncClient(
            base_url=_API_BASE_URL,
            follow_redirects=False,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Cookie": cookie,
                "Referer": _REFERER,
                "User-Agent": _USER_AGENT,
            },
            timeout=timeout,
            transport=transport,
        )
        self._transfer_client = httpx.AsyncClient(
            follow_redirects=False,
            timeout=timeout,
            transport=transport,
        )

    @staticmethod
    def _replace_cookie(cookie_header: str, name: str, value: str) -> str:
        replacement = f"{name}={value}"
        parts = [part.strip() for part in cookie_header.split(";") if part.strip()]
        for index, part in enumerate(parts):
            if part.partition("=")[0].strip() == name:
                parts[index] = replacement
                break
        else:
            parts.append(replacement)
        return "; ".join(parts)

    async def _accept_response_cookies(self, response: httpx.Response) -> None:
        updated = self.cookie
        for item in response.cookies.jar:
            if item.name in {"__pus", "__puus"} and item.value:
                updated = self._replace_cookie(updated, item.name, item.value)
        if updated == self.cookie:
            return
        self.cookie = updated
        self._api_client.headers["Cookie"] = updated
        if self._on_credential_update is not None:
            await self._on_credential_update({"cookie": updated})

    @staticmethod
    def _response_detail(response: httpx.Response) -> str:
        return " ".join(response.text.split())[:300]

    async def _api_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str | int] | None = None,
        json_body: dict[str, object] | None = None,
    ) -> dict[str, object]:
        async with self._api_request_lock:
            return await self._api_request_unlocked(
                method,
                path,
                params=params,
                json_body=json_body,
            )

    async def _api_request_unlocked(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str | int] | None = None,
        json_body: dict[str, object] | None = None,
    ) -> dict[str, object]:
        query: dict[str, str | int] = {"pr": "ucpro", "fr": "pc"}
        if params:
            query.update(params)
        try:
            response = await self._api_client.request(method, path, params=query, json=json_body)
        except httpx.HTTPError as exc:
            raise CloudUploadError(f"夸克接口 {path} 请求失败：{exc}") from exc
        await self._accept_response_cookies(response)
        try:
            payload = response.json()
        except ValueError as exc:
            detail = self._response_detail(response)
            raise CloudUploadError(f"夸克接口 {path} 返回了无效 JSON：{detail}") from exc
        if not isinstance(payload, dict):
            raise CloudUploadError(f"夸克接口 {path} 返回格式错误")

        status = payload.get("status", 0)
        code = payload.get("code", 0)
        api_failed = (
            response.is_error
            or (isinstance(status, int) and status >= 400)
            or (isinstance(code, int) and code != 0)
            or (isinstance(code, str) and code not in {"", "0"})
        )
        if api_failed:
            message = str(payload.get("message") or payload.get("msg") or self._response_detail(response))
            raise CloudUploadError(f"夸克接口 {path} 失败（HTTP {response.status_code}，code={code}）：{message}")
        return payload

    async def _list_directory(self, parent_id: str) -> list[RemoteEntry]:
        entries: list[RemoteEntry] = []
        page = 1
        page_size = 100
        while True:
            payload = await self._api_request(
                "GET",
                "/file/sort",
                params={
                    "pdir_fid": parent_id,
                    "_size": page_size,
                    "_page": page,
                    "_fetch_total": "1",
                    "fetch_all_file": "1",
                    "fetch_risk_file_name": "1",
                },
            )
            data = payload.get("data")
            metadata = payload.get("metadata")
            if not isinstance(data, dict) or not isinstance(data.get("list"), list):
                raise CloudUploadError("夸克目录列表响应缺少 data.list")
            for raw in data["list"]:
                if not isinstance(raw, dict):
                    continue
                try:
                    entries.append(
                        RemoteEntry(
                            id=str(raw["fid"]),
                            name=html.unescape(str(raw["file_name"])),
                            size=int(raw.get("size", 0)),
                            is_directory=not bool(raw.get("file")),
                        )
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    raise CloudUploadError("夸克目录列表包含无效文件记录") from exc
            total = metadata.get("_total", len(entries)) if isinstance(metadata, dict) else len(entries)
            try:
                if page * page_size >= int(total):
                    return entries
            except (TypeError, ValueError) as exc:
                raise CloudUploadError("夸克目录列表返回了无效总数") from exc
            page += 1

    @staticmethod
    def _named_entry(entries: list[RemoteEntry], name: str) -> RemoteEntry | None:
        return next((entry for entry in entries if entry.name == name), None)

    async def _walk_directories(self, parts: list[str], *, create: bool) -> str | None:
        parent_id = self.root_id
        for name in parts:
            entries = await self._list_directory(parent_id)
            entry = self._named_entry(entries, name)
            if entry is not None:
                if not entry.is_directory:
                    raise CloudUploadError(f"夸克远端路径冲突，{name} 已存在且不是目录")
                parent_id = entry.id
                continue
            if not create:
                return None
            try:
                payload = await self._api_request(
                    "POST",
                    "/file",
                    json_body={
                        "dir_init_lock": False,
                        "dir_path": "",
                        "file_name": name,
                        "pdir_fid": parent_id,
                    },
                )
            except CloudUploadError:
                await self._sleep(1)
                entry = self._named_entry(await self._list_directory(parent_id), name)
                if entry is None or not entry.is_directory:
                    raise
                parent_id = entry.id
                continue
            data = payload.get("data")
            created_id = data.get("fid") if isinstance(data, dict) else None
            if created_id:
                parent_id = str(created_id)
                await self._sleep(1)
                continue
            await self._sleep(1)
            entry = self._named_entry(await self._list_directory(parent_id), name)
            if entry is None or not entry.is_directory:
                raise CloudUploadError(f"夸克创建目录后无法找到目录：{name}")
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
            raise CloudUploadError(f"夸克远端路径冲突，目标是目录：{remote_path}")
        return entry.size

    @staticmethod
    def _hash_file(path: Path) -> tuple[str, str]:
        md5 = hashlib.md5()
        sha1 = hashlib.sha1()
        with path.open("rb") as stream:
            while chunk := stream.read(4 * 1024 * 1024):
                md5.update(chunk)
                sha1.update(chunk)
        return md5.hexdigest(), sha1.hexdigest()

    @staticmethod
    async def _file_range(
        path: Path,
        offset: int,
        size: int,
        on_streamed: Callable[[int], None] | None = None,
    ) -> AsyncIterator[bytes]:
        stream = path.open("rb")
        remaining = size
        try:
            stream.seek(offset)
            while remaining:
                chunk = await asyncio.to_thread(stream.read, min(1024 * 1024, remaining))
                if not chunk:
                    raise CloudUploadError(f"读取录像分片时提前到达文件末尾：{path}")
                remaining -= len(chunk)
                if on_streamed is not None:
                    on_streamed(size - remaining)
                yield chunk
        finally:
            stream.close()

    @staticmethod
    def _oss_url(pre_data: dict[str, object]) -> str:
        bucket = str(pre_data.get("bucket") or "")
        upload_url = str(pre_data.get("upload_url") or "")
        obj_key = str(pre_data.get("obj_key") or "")
        if not bucket or not upload_url.startswith(("http://", "https://")) or not obj_key:
            raise CloudUploadError("夸克预上传响应缺少 OSS 地址")
        host_and_path = upload_url.split("://", 1)[1].rstrip("/")
        return f"https://{bucket}.{host_and_path}/{quote(obj_key, safe='/~')}"

    async def _upload_part(
        self,
        local_path: Path,
        pre_data: dict[str, object],
        *,
        content_type: str,
        part_number: int,
        offset: int,
        size: int,
        progress: UploadProgress | None = None,
    ) -> str:
        task_id = str(pre_data.get("task_id") or "")
        upload_id = str(pre_data.get("upload_id") or "")
        auth_info = str(pre_data.get("auth_info") or "")
        bucket = str(pre_data.get("bucket") or "")
        obj_key = str(pre_data.get("obj_key") or "")
        if not all((task_id, upload_id, auth_info, bucket, obj_key)):
            raise CloudUploadError("夸克预上传响应缺少分片参数")

        on_streamed = None
        if progress is not None:
            # Each attempt restarts the part, so the reported total rewinds with it.
            def on_streamed(streamed: int) -> None:
                progress("uploading", offset + streamed)

        last_error: Exception | None = None
        for attempt in range(3):
            date = format_datetime(datetime.now(timezone.utc), usegmt=True)
            auth_meta = (
                "PUT\n\n"
                f"{content_type}\n{date}\n"
                f"x-oss-date:{date}\n"
                f"x-oss-user-agent:{_OSS_USER_AGENT}\n"
                f"/{bucket}/{obj_key}?partNumber={part_number}&uploadId={upload_id}"
            )
            try:
                auth_payload = await self._api_request(
                    "POST",
                    "/file/upload/auth",
                    json_body={"auth_info": auth_info, "auth_meta": auth_meta, "task_id": task_id},
                )
                auth_data = auth_payload.get("data")
                auth_key = auth_data.get("auth_key") if isinstance(auth_data, dict) else None
                if not auth_key:
                    raise CloudUploadError("夸克分片授权响应缺少 auth_key")
                response = await self._transfer_client.put(
                    self._oss_url(pre_data),
                    params={"partNumber": part_number, "uploadId": upload_id},
                    headers={
                        "Authorization": str(auth_key),
                        "Content-Length": str(size),
                        "Content-Type": content_type,
                        "Referer": f"{_REFERER}/",
                        "x-oss-date": date,
                        "x-oss-user-agent": _OSS_USER_AGENT,
                    },
                    content=self._file_range(local_path, offset, size, on_streamed),
                )
                if response.status_code != 200:
                    raise CloudUploadError(
                        f"夸克 OSS 第 {part_number} 片上传失败（HTTP {response.status_code}）："
                        f"{self._response_detail(response)}"
                    )
                etag = response.headers.get("ETag")
                if not etag:
                    raise CloudUploadError(f"夸克 OSS 第 {part_number} 片响应缺少 ETag")
                return etag
            except (CloudUploadError, httpx.HTTPError) as exc:
                last_error = exc
                if attempt < 2:
                    await self._sleep(2**attempt)
        assert last_error is not None
        if isinstance(last_error, CloudUploadError):
            raise last_error
        raise CloudUploadError(f"夸克 OSS 第 {part_number} 片上传失败：{last_error}") from last_error

    async def _commit(self, pre_data: dict[str, object], etags: list[str]) -> None:
        task_id = str(pre_data.get("task_id") or "")
        upload_id = str(pre_data.get("upload_id") or "")
        auth_info = str(pre_data.get("auth_info") or "")
        bucket = str(pre_data.get("bucket") or "")
        obj_key = str(pre_data.get("obj_key") or "")
        callback = pre_data.get("callback")
        if not all((task_id, upload_id, auth_info, bucket, obj_key)) or not isinstance(callback, dict):
            raise CloudUploadError("夸克预上传响应缺少提交参数")

        lines = ['<?xml version="1.0" encoding="UTF-8"?>', "<CompleteMultipartUpload>"]
        for number, etag in enumerate(etags, start=1):
            lines.extend(("<Part>", f"<PartNumber>{number}</PartNumber>", f"<ETag>{etag}</ETag>", "</Part>"))
        lines.append("</CompleteMultipartUpload>")
        body = "\n".join(lines)
        content_md5 = base64.b64encode(hashlib.md5(body.encode()).digest()).decode()
        callback_json = json.dumps(callback, ensure_ascii=False, separators=(",", ":")).encode()
        callback_base64 = base64.b64encode(callback_json).decode()
        date = format_datetime(datetime.now(timezone.utc), usegmt=True)
        auth_meta = (
            f"POST\n{content_md5}\napplication/xml\n{date}\n"
            f"x-oss-callback:{callback_base64}\n"
            f"x-oss-date:{date}\n"
            f"x-oss-user-agent:{_OSS_USER_AGENT}\n"
            f"/{bucket}/{obj_key}?uploadId={upload_id}"
        )
        auth_payload = await self._api_request(
            "POST",
            "/file/upload/auth",
            json_body={"auth_info": auth_info, "auth_meta": auth_meta, "task_id": task_id},
        )
        auth_data = auth_payload.get("data")
        auth_key = auth_data.get("auth_key") if isinstance(auth_data, dict) else None
        if not auth_key:
            raise CloudUploadError("夸克提交授权响应缺少 auth_key")
        try:
            response = await self._transfer_client.post(
                self._oss_url(pre_data),
                params={"uploadId": upload_id},
                headers={
                    "Authorization": str(auth_key),
                    "Content-MD5": content_md5,
                    "Content-Type": "application/xml",
                    "Referer": f"{_REFERER}/",
                    "x-oss-callback": callback_base64,
                    "x-oss-date": date,
                    "x-oss-user-agent": _OSS_USER_AGENT,
                },
                content=body.encode(),
            )
        except httpx.HTTPError as exc:
            raise CloudUploadError(f"夸克 OSS 合并分片请求失败：{exc}") from exc
        if response.status_code != 200:
            raise CloudUploadError(
                f"夸克 OSS 合并分片失败（HTTP {response.status_code}）：{self._response_detail(response)}"
            )

    async def _upload(
        self,
        local_path: Path,
        filename: str,
        parent_id: str,
        progress: UploadProgress | None = None,
    ) -> None:
        size = local_path.stat().st_size
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        if progress is not None:
            progress("preparing", 0)
        md5, sha1 = await asyncio.to_thread(self._hash_file, local_path)
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        pre_payload = await self._api_request(
            "POST",
            "/file/upload/pre",
            json_body={
                "ccp_hash_update": True,
                "dir_name": "",
                "file_name": filename,
                "format_type": content_type,
                "l_created_at": now_ms,
                "l_updated_at": now_ms,
                "pdir_fid": parent_id,
                "size": size,
            },
        )
        pre_data = pre_payload.get("data")
        metadata = pre_payload.get("metadata")
        if not isinstance(pre_data, dict) or not isinstance(metadata, dict):
            raise CloudUploadError("夸克预上传响应格式错误")
        task_id = str(pre_data.get("task_id") or "")
        if not task_id:
            raise CloudUploadError("夸克预上传响应缺少 task_id")
        hash_payload = await self._api_request(
            "POST",
            "/file/update/hash",
            json_body={"md5": md5, "sha1": sha1, "task_id": task_id},
        )
        hash_data = hash_payload.get("data")
        if isinstance(hash_data, dict) and bool(hash_data.get("finish")):
            # The server matched the hash, so the whole file counts as transferred.
            if progress is not None:
                progress("uploading", size)
            return

        try:
            part_size = int(metadata["part_size"])
        except (KeyError, TypeError, ValueError) as exc:
            raise CloudUploadError("夸克预上传响应缺少有效 part_size") from exc
        if part_size <= 0:
            raise CloudUploadError("夸克预上传响应的 part_size 必须大于 0")
        etags: list[str] = []
        for index, offset in enumerate(range(0, size, part_size), start=1):
            etags.append(
                await self._upload_part(
                    local_path,
                    pre_data,
                    content_type=content_type,
                    part_number=index,
                    offset=offset,
                    size=min(part_size, size - offset),
                    progress=progress,
                )
            )
        if progress is not None:
            progress("uploading", size)
        await self._commit(pre_data, etags)
        await self._api_request(
            "POST",
            "/file/upload/finish",
            json_body={"obj_key": pre_data.get("obj_key"), "task_id": task_id},
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
            logger.info("夸克远端文件已存在且大小一致，跳过重复上传：%s", remote_path)
            if progress is not None:
                progress("verifying", local_size)
            return False
        if existing_size is not None:
            raise CloudUploadError(
                f"夸克远端文件已存在但大小不一致：{remote_path}（本地 {local_size}，远端 {existing_size}）"
            )

        directory_parts, filename = split_remote_file(remote_path)
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
        raise CloudUploadError(f"夸克上传后文件大小校验失败：{remote_path}（本地 {local_size}，远端 {uploaded_size}）")

    async def aclose(self) -> None:
        await asyncio.gather(self._api_client.aclose(), self._transfer_client.aclose())
