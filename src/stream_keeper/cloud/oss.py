from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit
from xml.etree import ElementTree

import httpx

from .base import CloudUploadError, UploadProgress

logger = logging.getLogger(__name__)

_Sleep = Callable[[float], Awaitable[None]]


class AliyunOssUploader:
    """Small OSS Signature V1 multipart uploader shared by native providers.

    Provider APIs issue short-lived STS credentials and the object destination.
    Keeping signing and retry behavior here prevents each cloud adapter from
    growing a subtly different OSS implementation.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        endpoint: str,
        bucket: str,
        access_key_id: str,
        access_key_secret: str,
        security_token: str = "",
        sleep: _Sleep = asyncio.sleep,
    ) -> None:
        self._client = client
        self.bucket = bucket.strip()
        self.access_key_id = access_key_id.strip()
        self.access_key_secret = access_key_secret
        self.security_token = security_token.strip()
        self._sleep = sleep
        if not self.bucket or not self.access_key_id or not self.access_key_secret:
            raise CloudUploadError("OSS 上传凭据不完整")
        self._base_url = self._normalize_endpoint(endpoint, self.bucket)

    @staticmethod
    def _normalize_endpoint(endpoint: str, bucket: str) -> str:
        value = endpoint.strip()
        if not value:
            raise CloudUploadError("OSS 上传地址为空")
        if not value.startswith(("http://", "https://")):
            value = "https://" + value
        parsed = urlsplit(value)
        if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise CloudUploadError("OSS 上传地址无效")
        # Provider APIs occasionally return a bucket-prefixed endpoint while
        # others return the regional endpoint. Canonicalize both to virtual-host
        # style without ever downgrading an upload to plaintext HTTP.
        host = parsed.netloc
        prefix = f"{bucket}."
        if parsed.hostname.startswith(prefix):
            hostname = parsed.hostname[len(prefix) :]
            host = hostname if parsed.port is None else f"{hostname}:{parsed.port}"
        path = parsed.path.rstrip("/")
        if path:
            raise CloudUploadError("OSS 上传地址不能包含路径")
        return urlunsplit(("https", f"{bucket}.{host}", "", "", "")).rstrip("/")

    @staticmethod
    def _canonical_oss_headers(headers: Mapping[str, str]) -> str:
        items: list[tuple[str, str]] = []
        for name, value in headers.items():
            lowered = name.lower().strip()
            if lowered.startswith("x-oss-"):
                normalized = " ".join(value.strip().split())
                items.append((lowered, normalized))
        items.sort()
        return "".join(f"{name}:{value}\n" for name, value in items)

    @staticmethod
    def _query_string(query: Mapping[str, str | None]) -> str:
        values: list[str] = []
        for key in sorted(query):
            value = query[key]
            encoded_key = quote(key, safe="")
            values.append(encoded_key if value is None else f"{encoded_key}={quote(str(value), safe='')}")
        return "&".join(values)

    def _signed_headers(
        self,
        method: str,
        object_key: str,
        query: Mapping[str, str | None],
        *,
        content_type: str = "",
        content_md5: str = "",
        extra_headers: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        headers = dict(extra_headers or {})
        headers["Date"] = format_datetime(datetime.now(timezone.utc), usegmt=True)
        if content_type:
            headers["Content-Type"] = content_type
        if content_md5:
            headers["Content-MD5"] = content_md5
        if self.security_token:
            headers["x-oss-security-token"] = self.security_token

        canonical_resource = f"/{self.bucket}/{object_key}"
        if query:
            canonical_resource += "?" + self._query_string(query)
        canonical = (
            f"{method}\n{content_md5}\n{content_type}\n{headers['Date']}\n"
            f"{self._canonical_oss_headers(headers)}{canonical_resource}"
        )
        digest = hmac.new(self.access_key_secret.encode(), canonical.encode(), hashlib.sha1).digest()
        signature = base64.b64encode(digest).decode()
        headers["Authorization"] = f"OSS {self.access_key_id}:{signature}"
        return headers

    def _url(self, object_key: str, query: Mapping[str, str | None]) -> str:
        url = f"{self._base_url}/{quote(object_key, safe='/~')}"
        if query:
            url += "?" + self._query_string(query)
        return url

    @staticmethod
    def _detail(response: httpx.Response) -> str:
        return " ".join(response.text.split())[:300]

    async def _request(
        self,
        method: str,
        object_key: str,
        query: Mapping[str, str | None],
        *,
        content: bytes | AsyncIterator[bytes] = b"",
        content_length: int = 0,
        content_type: str = "",
        content_md5: str = "",
        extra_headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        headers = self._signed_headers(
            method,
            object_key,
            query,
            content_type=content_type,
            content_md5=content_md5,
            extra_headers=extra_headers,
        )
        headers["Content-Length"] = str(content_length)
        # Build the request independently instead of using AsyncClient.request().
        # Provider clients carry API-specific default headers (including 115
        # cookies and GuangYa's JSON content type); merging those into an OSS
        # request would both leak credentials across hosts and invalidate the
        # signature when an unsigned default Content-Type reaches the wire.
        request = httpx.Request(
            method,
            self._url(object_key, query),
            headers=headers,
            content=content,
        )
        try:
            return await self._client.send(request)
        except httpx.HTTPError as exc:
            raise CloudUploadError(f"OSS 请求失败：{exc}") from exc

    @staticmethod
    def _xml_value(payload: bytes, name: str) -> str:
        try:
            root = ElementTree.fromstring(payload)
        except ElementTree.ParseError as exc:
            raise CloudUploadError("OSS 返回了无效 XML") from exc
        for element in root.iter():
            if element.tag.rsplit("}", 1)[-1] == name and element.text:
                return element.text
        raise CloudUploadError(f"OSS 响应缺少 {name}")

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
                    raise CloudUploadError(f"读取 OSS 上传分片时提前到达文件末尾：{path}")
                remaining -= len(chunk)
                if on_streamed is not None:
                    on_streamed(size - remaining)
                yield chunk
        finally:
            stream.close()

    async def _initiate(self, object_key: str) -> str:
        response = await self._request("POST", object_key, {"uploads": None})
        if response.status_code != 200:
            raise CloudUploadError(f"OSS 初始化分片上传失败（HTTP {response.status_code}）：{self._detail(response)}")
        return self._xml_value(response.content, "UploadId")

    async def _upload_part(
        self,
        local_path: Path,
        object_key: str,
        upload_id: str,
        *,
        number: int,
        offset: int,
        size: int,
        progress: UploadProgress | None,
    ) -> str:
        query = {"partNumber": str(number), "uploadId": upload_id}
        last_error: Exception | None = None
        for attempt in range(3):
            on_streamed = None
            if progress is not None:

                def on_streamed(streamed: int) -> None:
                    progress("uploading", offset + streamed)

            try:
                response = await self._request(
                    "PUT",
                    object_key,
                    query,
                    content=self._file_range(local_path, offset, size, on_streamed),
                    content_length=size,
                    content_type="application/octet-stream",
                )
                if response.status_code != 200:
                    raise CloudUploadError(
                        f"OSS 第 {number} 片上传失败（HTTP {response.status_code}）：{self._detail(response)}"
                    )
                etag = response.headers.get("ETag", "").strip()
                if not etag:
                    raise CloudUploadError(f"OSS 第 {number} 片响应缺少 ETag")
                return etag
            except CloudUploadError as exc:
                last_error = exc
                if attempt < 2:
                    await self._sleep(2**attempt)
        assert last_error is not None
        raise last_error

    async def _complete(
        self,
        object_key: str,
        upload_id: str,
        etags: list[str],
        callback: tuple[str, str] | None,
    ) -> None:
        root = ElementTree.Element("CompleteMultipartUpload")
        for number, etag in enumerate(etags, start=1):
            part = ElementTree.SubElement(root, "Part")
            ElementTree.SubElement(part, "PartNumber").text = str(number)
            ElementTree.SubElement(part, "ETag").text = etag
        body = ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)
        content_md5 = base64.b64encode(hashlib.md5(body).digest()).decode()
        extra_headers: dict[str, str] = {}
        if callback is not None:
            callback_body, callback_vars = callback
            extra_headers["x-oss-callback"] = base64.b64encode(callback_body.encode()).decode()
            extra_headers["x-oss-callback-var"] = base64.b64encode(callback_vars.encode()).decode()
        response = await self._request(
            "POST",
            object_key,
            {"uploadId": upload_id},
            content=body,
            content_length=len(body),
            content_type="application/xml",
            content_md5=content_md5,
            extra_headers=extra_headers,
        )
        if response.status_code != 200:
            raise CloudUploadError(f"OSS 合并分片失败（HTTP {response.status_code}）：{self._detail(response)}")

    async def _abort(self, object_key: str, upload_id: str) -> None:
        try:
            response = await self._request("DELETE", object_key, {"uploadId": upload_id})
        except CloudUploadError as exc:
            logger.warning("中止 OSS 分片上传失败：%s", exc)
            return
        if response.status_code not in {200, 204, 404}:
            logger.warning("中止 OSS 分片上传返回 HTTP %s", response.status_code)

    async def upload(
        self,
        local_path: Path,
        object_key: str,
        *,
        part_size: int = 20 * 1024 * 1024,
        callback: tuple[str, str] | None = None,
        progress: UploadProgress | None = None,
    ) -> None:
        size = local_path.stat().st_size
        if size <= 0:
            raise CloudUploadError("OSS 不接受空录像文件")
        if part_size < 100 * 1024 or part_size > 5 * 1024 * 1024 * 1024:
            raise CloudUploadError("OSS 分片大小超出允许范围")
        object_key = object_key.lstrip("/")
        if not object_key or any(ord(char) < 32 for char in object_key):
            raise CloudUploadError("OSS Object 路径无效")

        upload_id = await self._initiate(object_key)
        try:
            etags: list[str] = []
            for number, offset in enumerate(range(0, size, part_size), start=1):
                etags.append(
                    await self._upload_part(
                        local_path,
                        object_key,
                        upload_id,
                        number=number,
                        offset=offset,
                        size=min(part_size, size - offset),
                        progress=progress,
                    )
                )
            await self._complete(object_key, upload_id, etags, callback)
        except BaseException:
            await self._abort(object_key, upload_id)
            raise
        if progress is not None:
            progress("uploading", size)
