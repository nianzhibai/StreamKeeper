from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
import posixpath
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx

from .base import (
    CloudUploadError,
    CredentialRefreshCoordinator,
    CredentialUpdate,
    RemoteEntry,
    UploadProgress,
    split_remote_file,
)

logger = logging.getLogger(__name__)

_API_BASE_URL = "https://pan.baidu.com/rest/2.0"
_WEB_API_BASE_URL = "https://pan.baidu.com/api"
_TOKEN_URL = "https://openapi.baidu.com/oauth/2.0/token"
_UPLOAD_BASE_URL = "https://d.pcs.baidu.com"
_BDSTOKEN_URL = "https://pan.baidu.com/api/gettemplatevariable"
_MAX_PARTS = 2048
_WEB_APP_ID = "250528"
_Sleep = Callable[[float], Awaitable[None]]


def _parse_cookie_header(cookie: str) -> dict[str, str]:
    """Parse a ``Name=Value; Name2=Value2`` cookie header into a cookie map."""

    values: dict[str, str] = {}
    for part in cookie.split(";"):
        name, separator, value = part.strip().partition("=")
        if separator and name:
            values[name] = value
    return values


class BaiduNetdiskClient:
    """Baidu Open Platform directory API and superfile2 multipart uploader."""

    def __init__(
        self,
        access_token: str = "",
        refresh_token: str = "",
        client_id: str = "",
        client_secret: str = "",
        cookie: str = "",
        *,
        timeout_seconds: int = 300,
        on_credential_update: CredentialUpdate | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: _Sleep = asyncio.sleep,
        api_base_url: str = _API_BASE_URL,
        token_url: str = _TOKEN_URL,
        upload_base_url: str = _UPLOAD_BASE_URL,
    ) -> None:
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.client_id = client_id
        self.client_secret = client_secret
        self.cookie = cookie
        self._on_credential_update = on_credential_update
        self._sleep = sleep
        self._api_base_url = api_base_url.rstrip("/")
        self._token_url = token_url
        self._upload_base_url = upload_base_url.rstrip("/")
        self._refresh_state = CredentialRefreshCoordinator()
        self._vip_type: int | None = None
        self._bdstoken: str | None = None
        self._client = httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(float(timeout_seconds)),
            transport=transport,
            headers={"Accept": "application/json", "User-Agent": "StreamKeeper/0.6"},
            cookies=_parse_cookie_header(cookie),
        )

    @property
    def _uses_cookie(self) -> bool:
        return bool(self.cookie)

    @staticmethod
    def _detail(response: httpx.Response) -> str:
        return " ".join(response.text.split())[:300]

    @staticmethod
    def _json_object(response: httpx.Response, operation: str) -> dict[str, object]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise CloudUploadError(f"百度网盘{operation}返回无效 JSON：{BaiduNetdiskClient._detail(response)}") from exc
        if not isinstance(payload, dict):
            raise CloudUploadError(f"百度网盘{operation}返回格式错误")
        return payload

    @staticmethod
    def _is_auth_expired(payload: dict[str, object]) -> bool:
        codes: list[int] = []
        for key in ("errno", "error_code", "code"):
            try:
                codes.append(int(payload.get(key, 0) or 0))
            except (TypeError, ValueError):
                continue
        if any(code in {6, 110, 111, 31023, 31024, 401, -6} or str(code).startswith("401") for code in codes):
            return True
        text = " ".join(
            str(payload.get(key) or "") for key in ("error", "error_msg", "errmsg", "error_description")
        ).lower()
        return "access token" in text and any(word in text for word in ("invalid", "expire", "过期", "失效"))

    async def _save_credentials(self) -> None:
        if self._on_credential_update is not None:
            await self._on_credential_update(
                {
                    "access_token": self.access_token,
                    "refresh_token": self.refresh_token,
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                }
            )

    async def _refresh_access_token(self, *, rejected_generation: int | None = None) -> None:
        async def refresh() -> None:
            if not self.refresh_token or not self.client_id or not self.client_secret:
                raise CloudUploadError("百度网盘 Access Token 已失效，且没有可用的客户端刷新凭据")
            try:
                response = await self._client.get(
                    self._token_url,
                    params={
                        "grant_type": "refresh_token",
                        "refresh_token": self.refresh_token,
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                    },
                )
            except httpx.HTTPError as exc:
                raise CloudUploadError(f"百度网盘刷新 Token 请求失败：{exc}") from exc
            payload = self._json_object(response, "刷新 Token")
            access_token = payload.get("access_token")
            refresh_token = payload.get("refresh_token")
            if response.is_error or not isinstance(access_token, str) or not access_token:
                message = payload.get("error_description") or payload.get("error") or self._detail(response)
                raise CloudUploadError(f"百度网盘刷新 Token 失败：{message}")
            self.access_token = access_token
            if isinstance(refresh_token, str) and refresh_token:
                self.refresh_token = refresh_token
            await self._save_credentials()

        await self._refresh_state.refresh(rejected_generation, refresh)

    async def _api_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str | int] | None = None,
        form: dict[str, str] | None = None,
    ) -> dict[str, object]:
        if self._uses_cookie:
            return await self._cookie_api_request(method, path, params=params, form=form)
        if not self.access_token:
            await self._refresh_access_token(rejected_generation=self._refresh_state.generation)
        for auth_attempt in range(2):
            access_token = self.access_token
            token_generation = self._refresh_state.generation
            query: dict[str, str | int] = {"access_token": access_token}
            if params:
                query.update(params)
            try:
                response = await self._client.request(
                    method,
                    f"{self._api_base_url}{path}",
                    params=query,
                    data=form,
                )
            except httpx.HTTPError as exc:
                raise CloudUploadError(f"百度网盘接口 {path} 请求失败：{exc}") from exc
            payload = self._json_object(response, f"接口 {path}")
            try:
                errno = int(payload.get("errno", 0))
            except (TypeError, ValueError):
                errno = -1
            if self._is_auth_expired(payload) and auth_attempt == 0:
                await self._refresh_access_token(rejected_generation=token_generation)
                continue
            if response.is_error or errno != 0:
                message = payload.get("errmsg") or payload.get("error_msg") or payload.get("error") or ""
                raise CloudUploadError(
                    f"百度网盘接口 {path} 失败（HTTP {response.status_code}，errno={errno}）：{message}"
                )
            return payload
        raise CloudUploadError("百度网盘 Token 刷新后仍无法访问接口")

    async def _ensure_bdstoken(self) -> str:
        if self._bdstoken:
            return self._bdstoken
        try:
            response = await self._client.get(
                _BDSTOKEN_URL,
                params={
                    "clienttype": 0,
                    "app_id": _WEB_APP_ID,
                    "web": 1,
                    "dp-logid": str(int(time.time())),
                    "fields": json.dumps(["bdstoken"], separators=(",", ":")),
                },
            )
        except httpx.HTTPError as exc:
            raise CloudUploadError(f"百度网盘获取 bdstoken 失败：{exc}") from exc
        payload = self._json_object(response, "获取 bdstoken")
        result = payload.get("result")
        token = result.get("bdstoken") if isinstance(result, dict) else None
        if not isinstance(token, str) or not token:
            raise CloudUploadError("百度网盘获取 bdstoken 失败")
        self._bdstoken = token
        return token

    @staticmethod
    def _web_query(operation: str, bdstoken: str, *, isdir: str | None = None) -> dict[str, str]:
        query = {
            "bdstoken": bdstoken,
            "app_id": _WEB_APP_ID,
            "channel": "chunlei",
            "web": "1",
            "clienttype": "0",
            "rtype": "1",
        }
        if isdir is not None:
            query["isdir"] = isdir
        return query

    async def _cookie_api_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str | int] | None = None,
        form: dict[str, str] | None = None,
    ) -> dict[str, object]:
        operation = str(params.get("method", "") if params else "")
        if operation in {"list", "uinfo"}:
            query: dict[str, str | int] = dict(params or {})
            if operation == "list":
                query["web"] = "web"
            else:
                query.update({"app_id": _WEB_APP_ID, "web": "1", "clienttype": "0"})
            try:
                response = await self._client.request(
                    method,
                    f"{self._api_base_url}{path}",
                    params=query,
                    data=form,
                )
            except httpx.HTTPError as exc:
                raise CloudUploadError(f"百度网盘接口 {path} 请求失败：{exc}") from exc
            payload = self._json_object(response, f"接口 {path}")
            if response.is_error or payload.get("errno") != 0:
                message = payload.get("errmsg") or payload.get("error_msg") or payload.get("error") or ""
                raise CloudUploadError(
                    f"百度网盘接口 {path} 失败（HTTP {response.status_code}，errno={payload.get('errno')}）：{message}"
                )
            return payload

        bdstoken = await self._ensure_bdstoken()
        if operation == "precreate":
            url = f"{_WEB_API_BASE_URL}/precreate"
            if not form or not isinstance(form.get("path"), str):
                raise CloudUploadError("百度网盘预上传缺少目标路径")
            web_form = {
                "path": form["path"],
                "autoinit": "1",
                "block_list": form.get("block_list", "[]"),
                "target_path": posixpath.dirname(form["path"]) or "/",
                "local_mtime": str(int(time.time())),
            }
        elif operation == "create":
            isdir = "1" if form and form.get("isdir") == "1" else "0"
            url = f"{_WEB_API_BASE_URL}/create"
            if not form or not isinstance(form.get("path"), str):
                raise CloudUploadError("百度网盘创建目录缺少目标路径")
            web_form: dict[str, str] = {
                "path": form["path"],
                "target_path": posixpath.dirname(form["path"]) or "/",
                "local_mtime": str(int(time.time())),
            }
            if isdir == "1":
                web_form.update({"size": "0", "block_list": "[]", "isdir": "1"})
            else:
                web_form.update(
                    {
                        "size": str(form.get("size", "0")),
                        "uploadid": str(form.get("uploadid", "")),
                        "block_list": str(form.get("block_list", "[]")),
                    }
                )
        else:
            raise CloudUploadError(f"百度网盘不支持网页版接口 {path}")

        query = self._web_query(operation, bdstoken, isdir=isdir if operation == "create" else None)
        try:
            response = await self._client.post(url, params=query, data=web_form)
        except httpx.HTTPError as exc:
            raise CloudUploadError(f"百度网盘接口 {operation} 请求失败：{exc}") from exc
        payload = self._json_object(response, f"接口 {operation}")
        if response.is_error or payload.get("errno") not in {0, None}:
            message = payload.get("errmsg") or payload.get("error_msg") or payload.get("error") or ""
            raise CloudUploadError(
                f"百度网盘接口 {operation} 失败（HTTP {response.status_code}，errno={payload.get('errno')}）：{message}"
            )
        return payload

    async def _list_directory(self, directory: str) -> list[RemoteEntry]:
        entries: list[RemoteEntry] = []
        start = 0
        page_size = 1000
        while True:
            payload = await self._api_request(
                "GET",
                "/xpan/file",
                params={
                    "method": "list",
                    "dir": directory,
                    "start": start,
                    "limit": page_size,
                    "order": "name",
                    "web": "web",
                },
            )
            raw_entries = payload.get("list")
            if not isinstance(raw_entries, list):
                raise CloudUploadError("百度网盘目录列表响应缺少 list")
            for raw in raw_entries:
                if not isinstance(raw, dict):
                    continue
                try:
                    entries.append(
                        RemoteEntry(
                            id=str(raw.get("fs_id") or raw["path"]),
                            name=str(raw.get("server_filename") or posixpath.basename(str(raw["path"]))),
                            size=int(raw.get("size", 0)),
                            is_directory=int(raw.get("isdir", 0)) == 1,
                        )
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    raise CloudUploadError("百度网盘目录列表包含无效文件记录") from exc
            if len(raw_entries) < page_size:
                return entries
            start += page_size

    @staticmethod
    def _named_entry(entries: list[RemoteEntry], name: str) -> RemoteEntry | None:
        return next((entry for entry in entries if entry.name == name), None)

    async def _walk_directories(self, parts: list[str], *, create: bool) -> str | None:
        current = "/"
        for name in parts:
            entry = self._named_entry(await self._list_directory(current), name)
            child = posixpath.join(current, name)
            if entry is not None:
                if not entry.is_directory:
                    raise CloudUploadError(f"百度网盘远端路径冲突，{name} 已存在且不是目录")
                current = child
                continue
            if not create:
                return None
            try:
                await self._api_request(
                    "POST",
                    "/xpan/file",
                    params={"method": "create"},
                    form={"path": child, "size": "0", "isdir": "1", "rtype": "3"},
                )
            except CloudUploadError:
                entry = self._named_entry(await self._list_directory(current), name)
                if entry is None or not entry.is_directory:
                    raise
            current = child
        return current

    async def remote_size(self, remote_path: str) -> int | None:
        directory_parts, filename = split_remote_file(remote_path)
        directory = await self._walk_directories(directory_parts, create=False)
        if directory is None:
            return None
        entry = self._named_entry(await self._list_directory(directory), filename)
        if entry is None:
            return None
        if entry.is_directory:
            raise CloudUploadError(f"百度网盘远端路径冲突，目标是目录：{remote_path}")
        return entry.size

    async def _part_size(self, file_size: int) -> int:
        if self._vip_type is None:
            payload = await self._api_request("GET", "/xpan/nas", params={"method": "uinfo"})
            try:
                self._vip_type = int(payload.get("vip_type", 0))
            except (TypeError, ValueError):
                logger.warning("百度网盘会员类型响应无效，按普通账号分片")
                self._vip_type = 0
        part_size = {0: 4, 1: 16, 2: 32}.get(self._vip_type, 4) * 1024 * 1024
        if (file_size + part_size - 1) // part_size > _MAX_PARTS:
            raise CloudUploadError("百度网盘账号允许的分片大小不足以上传该录像，请缩短录像分段时长")
        return part_size

    @staticmethod
    def _hash_file(path: Path, part_size: int) -> tuple[str, str, list[str]]:
        full = hashlib.md5()
        first_slice = hashlib.md5()
        first_remaining = 256 * 1024
        blocks: list[str] = []
        with path.open("rb") as stream:
            while chunk := stream.read(part_size):
                full.update(chunk)
                if first_remaining:
                    first = chunk[:first_remaining]
                    first_slice.update(first)
                    first_remaining -= len(first)
                blocks.append(hashlib.md5(chunk).hexdigest())
        return full.hexdigest(), first_slice.hexdigest(), blocks

    @staticmethod
    def _safe_upload_base(value: str) -> str:
        parsed = urlsplit(value.strip())
        if not parsed.hostname or parsed.username or parsed.password:
            raise CloudUploadError("百度网盘返回了无效上传地址")
        return urlunsplit(("https", parsed.netloc, "", "", ""))

    async def _locate_upload(self, remote_path: str, upload_id: str) -> str:
        for auth_attempt in range(2):
            access_token = self.access_token
            token_generation = self._refresh_state.generation
            try:
                response = await self._client.get(
                    f"{self._upload_base_url}/rest/2.0/pcs/file",
                    params={
                        "method": "locateupload",
                        "appid": "250528",
                        "path": remote_path,
                        "uploadid": upload_id,
                        "upload_version": "2.0",
                        "access_token": access_token,
                    },
                )
            except httpx.HTTPError as exc:
                logger.warning("百度网盘动态上传地址获取失败，使用默认地址：%s", exc)
                return self._upload_base_url
            try:
                payload = self._json_object(response, "获取上传地址")
            except CloudUploadError as exc:
                logger.warning("百度网盘动态上传地址响应无效，使用默认地址：%s", exc)
                return self._upload_base_url
            if self._is_auth_expired(payload) and auth_attempt == 0:
                await self._refresh_access_token(rejected_generation=token_generation)
                continue
            candidates = payload.get("servers") or payload.get("bak_servers")
            if not response.is_error and isinstance(candidates, list) and candidates:
                first = candidates[0]
                server = first.get("server") if isinstance(first, dict) else None
                if isinstance(server, str) and server:
                    return self._safe_upload_base(server)
            return self._upload_base_url
        return self._upload_base_url

    @staticmethod
    def _multipart_prefix(boundary: str, filename: str, content_type: str) -> bytes:
        safe_filename = filename.replace("\r", "_").replace("\n", "_").replace('"', "%22")
        return (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{safe_filename}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode()

    @staticmethod
    async def _multipart_content(
        local_path: Path,
        offset: int,
        size: int,
        prefix: bytes,
        suffix: bytes,
        on_streamed: Callable[[int], None] | None,
    ) -> AsyncIterator[bytes]:
        yield prefix
        stream = local_path.open("rb")
        remaining = size
        try:
            stream.seek(offset)
            while remaining:
                chunk = await asyncio.to_thread(stream.read, min(1024 * 1024, remaining))
                if not chunk:
                    raise CloudUploadError(f"读取百度上传分片时提前到达文件末尾：{local_path}")
                remaining -= len(chunk)
                if on_streamed is not None:
                    on_streamed(size - remaining)
                yield chunk
        finally:
            stream.close()
        yield suffix

    async def _upload_part(
        self,
        upload_base: str,
        local_path: Path,
        remote_path: str,
        upload_id: str,
        filename: str,
        *,
        part_number: int,
        offset: int,
        size: int,
        progress: UploadProgress | None,
    ) -> str | None:
        """Upload one part; cookie mode returns the part md5 from the server."""
        if self._uses_cookie:
            with local_path.open("rb") as stream:
                stream.seek(offset)
                content = stream.read(size)
            if len(content) != size:
                raise CloudUploadError(f"读取百度上传分片时提前到达文件末尾：{local_path}")
            response = await self._client.post(
                f"{upload_base}/rest/2.0/pcs/superfile2",
                params={
                    "method": "upload",
                    "app_id": _WEB_APP_ID,
                    "channel": "chunlei",
                    "web": "1",
                    "clienttype": "0",
                    "path": remote_path,
                    "uploadid": upload_id,
                    "uploadsign": "0",
                    "partseq": str(part_number),
                },
                files={"file": (filename, content, mimetypes.guess_type(filename)[0] or "application/octet-stream")},
            )
            payload = self._json_object(response, f"上传第 {part_number + 1} 片")
            md5 = payload.get("md5")
            if response.is_error or not isinstance(md5, str) or not md5:
                code = payload.get("error_code") or payload.get("errno") or payload.get("errmsg") or ""
                raise CloudUploadError(
                    f"百度网盘第 {part_number + 1} 片上传失败（HTTP {response.status_code}，code={code}）"
                )
            if progress is not None:
                progress("uploading", offset + size)
            return md5

        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        last_error: Exception | None = None
        for attempt in range(3):
            boundary = f"----StreamKeeper{secrets.token_hex(12)}"
            prefix = self._multipart_prefix(boundary, filename, content_type)
            suffix = f"\r\n--{boundary}--\r\n".encode()
            on_streamed = None
            if progress is not None:

                def on_streamed(streamed: int) -> None:
                    progress("uploading", offset + streamed)

            try:
                access_token = self.access_token
                token_generation = self._refresh_state.generation
                response = await self._client.post(
                    f"{upload_base}/rest/2.0/pcs/superfile2",
                    params={
                        "method": "upload",
                        "access_token": access_token,
                        "type": "tmpfile",
                        "path": remote_path,
                        "uploadid": upload_id,
                        "partseq": str(part_number),
                    },
                    headers={
                        "Content-Length": str(len(prefix) + size + len(suffix)),
                        "Content-Type": f"multipart/form-data; boundary={boundary}",
                    },
                    content=self._multipart_content(local_path, offset, size, prefix, suffix, on_streamed),
                )
                payload = self._json_object(response, f"上传第 {part_number + 1} 片")
                if self._is_auth_expired(payload) and attempt < 2:
                    await self._refresh_access_token(rejected_generation=token_generation)
                    continue
                try:
                    error_code = int(payload.get("error_code", payload.get("errno", 0)))
                except (TypeError, ValueError):
                    error_code = -1
                if response.is_error or error_code != 0:
                    raise CloudUploadError(
                        f"百度网盘第 {part_number + 1} 片上传失败（HTTP {response.status_code}，code={error_code}）"
                    )
                return None
            except (CloudUploadError, httpx.HTTPError, TypeError, ValueError) as exc:
                last_error = exc
                if attempt < 2:
                    await self._sleep(2**attempt)
        assert last_error is not None
        if isinstance(last_error, CloudUploadError):
            raise last_error
        raise CloudUploadError(f"百度网盘第 {part_number + 1} 片上传失败：{last_error}") from last_error

    async def _upload(
        self,
        local_path: Path,
        remote_path: str,
        filename: str,
        progress: UploadProgress | None,
    ) -> None:
        size = local_path.stat().st_size
        if size <= 0:
            raise CloudUploadError("百度网盘不允许上传空文件")
        part_size = await self._part_size(size)
        if progress is not None:
            progress("preparing", 0)
        content_md5, slice_md5, block_list = await asyncio.to_thread(self._hash_file, local_path, part_size)
        stat = local_path.stat()
        common_form = {
            "path": remote_path,
            "size": str(size),
            "isdir": "0",
            "rtype": "3",
            "block_list": json.dumps(block_list, separators=(",", ":")),
            "local_mtime": str(int(stat.st_mtime)),
            "local_ctime": str(int(stat.st_ctime)),
        }
        precreate = await self._api_request(
            "POST",
            "/xpan/file",
            params={"method": "precreate"},
            form={
                **common_form,
                "autoinit": "1",
                "content-md5": content_md5,
                "slice-md5": slice_md5,
            },
        )
        try:
            return_type = int(precreate.get("return_type", 0) or 0)
        except (TypeError, ValueError) as exc:
            raise CloudUploadError("百度网盘预上传响应包含无效 return_type") from exc
        if return_type == 2:
            if progress is not None:
                progress("uploading", size)
            return
        upload_id = str(precreate.get("uploadid") or "")
        needed = precreate.get("block_list")
        if not upload_id or not isinstance(needed, list):
            raise CloudUploadError("百度网盘预上传响应缺少 uploadid 或 block_list")
        if self._uses_cookie:
            upload_base = self._upload_base_url
        else:
            upload_base = await self._locate_upload(remote_path, upload_id)
        server_md5s: list[str] = []
        for raw_part in needed:
            try:
                part_number = int(raw_part)
            except (TypeError, ValueError) as exc:
                raise CloudUploadError("百度网盘预上传响应包含无效分片编号") from exc
            if not 0 <= part_number < len(block_list):
                raise CloudUploadError("百度网盘预上传响应的分片编号越界")
            offset = part_number * part_size
            part_md5 = await self._upload_part(
                upload_base,
                local_path,
                remote_path,
                upload_id,
                filename,
                part_number=part_number,
                offset=offset,
                size=min(part_size, size - offset),
                progress=progress,
            )
            if part_md5 is not None:
                server_md5s.append(part_md5)
        create_form = dict(common_form)
        if self._uses_cookie:
            create_form["block_list"] = json.dumps(server_md5s, separators=(",", ":"))
        create_form["uploadid"] = upload_id
        await self._api_request(
            "POST",
            "/xpan/file",
            params={"method": "create"},
            form=create_form,
        )
        if progress is not None:
            progress("uploading", size)

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
            logger.info("百度网盘远端文件已存在且大小一致，跳过重复上传：%s", remote_path)
            if progress is not None:
                progress("verifying", local_size)
            return False
        if existing_size is not None:
            raise CloudUploadError(
                f"百度网盘远端文件已存在但大小不一致：{remote_path}（本地 {local_size}，远端 {existing_size}）"
            )

        directory_parts, filename = split_remote_file(remote_path)
        await self._walk_directories(directory_parts, create=True)
        await self._upload(local_path, remote_path, filename, progress)
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
            f"百度网盘上传后文件大小校验失败：{remote_path}（本地 {local_size}，远端 {uploaded_size}）"
        )

    async def aclose(self) -> None:
        await self._client.aclose()
