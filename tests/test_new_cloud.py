import asyncio
import base64
import hashlib
import json
import struct
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import patch
from urllib.parse import parse_qs

import httpx
from Crypto.Cipher import AES

import stream_keeper.cloud.pan115_cookie as pan115_cookie_module
from stream_keeper.cloud import (
    BaiduNetdiskClient,
    CloudArchiveConfig,
    CloudProviderConfig,
    CloudUploadError,
    GuangYaPanClient,
    Pan115Client,
    Pan115CookieClient,
    create_cloud_client,
)
from stream_keeper.cloud.oss import AliyunOssUploader, OssCredentials, parse_oss_expiration
from stream_keeper.settings import CLOUD_ARCHIVE_ROOT


async def no_sleep(_seconds: float) -> None:
    return None


class AliyunOssUploaderTests(IsolatedAsyncioTestCase):
    def test_sts_expiration_parser_accepts_provider_timestamp_shapes(self) -> None:
        expected = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(parse_oss_expiration("2026-08-01T00:00:00Z"), expected)
        self.assertEqual(parse_oss_expiration(expected.timestamp()), expected)
        self.assertEqual(parse_oss_expiration(expected.timestamp() * 1000), expected)
        self.assertIsNone(parse_oss_expiration("not-a-date"))

    async def test_provider_default_headers_are_not_sent_to_oss(self) -> None:
        requests: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            await request.aread()
            if "uploads" in request.url.params:
                return httpx.Response(
                    200,
                    content=b"<InitiateMultipartUploadResult><UploadId>upload-1</UploadId></InitiateMultipartUploadResult>",
                )
            if "partNumber" in request.url.params:
                return httpx.Response(200, headers={"ETag": '"etag-1"'})
            return httpx.Response(200, content=b"<CompleteMultipartUploadResult/>")

        client = httpx.AsyncClient(
            headers={
                "Cookie": "UID=secret; CID=secret",
                "Content-Type": "application/json",
                "X-Device-Id": "private-device",
            },
            transport=httpx.MockTransport(handler),
        )
        try:
            uploader = AliyunOssUploader(
                client,
                endpoint="https://oss.test",
                bucket="bucket",
                access_key_id="access-key",
                access_key_secret="access-secret",
                security_token="security-token",
                sleep=no_sleep,
            )
            with TemporaryDirectory() as tmp:
                path = Path(tmp) / "recording.ts"
                path.write_bytes(b"recording")
                await uploader.upload(path, "archive/recording.ts", part_size=100 * 1024)
        finally:
            await client.aclose()

        self.assertEqual(len(requests), 3)
        for request in requests:
            self.assertNotIn("cookie", request.headers)
            self.assertNotIn("x-device-id", request.headers)
            self.assertIn("x-oss-security-token", request.headers)
        self.assertNotIn("content-type", requests[0].headers)
        self.assertEqual(requests[1].headers["content-type"], "application/octet-stream")
        self.assertEqual(requests[2].headers["content-type"], "application/xml")

    async def test_expired_sts_is_refreshed_and_the_same_part_is_replayed(self) -> None:
        refreshes = 0
        part_bodies: list[bytes] = []
        access_keys: list[str] = []

        async def credentials() -> OssCredentials:
            nonlocal refreshes
            refreshes += 1
            return OssCredentials(
                access_key_id="new-key",
                access_key_secret="new-secret",
                security_token="new-security-token",
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            )

        async def handler(request: httpx.Request) -> httpx.Response:
            access_key = request.headers["authorization"].split(" ", 1)[1].split(":", 1)[0]
            access_keys.append(access_key)
            if "uploads" in request.url.params:
                return httpx.Response(
                    200,
                    content=b"<InitiateMultipartUploadResult><UploadId>upload-1</UploadId></InitiateMultipartUploadResult>",
                )
            if "partNumber" in request.url.params:
                part_bodies.append(await request.aread())
                if access_key == "old-key":
                    return httpx.Response(
                        403,
                        content=b"<Error><Code>SecurityTokenExpired</Code></Error>",
                    )
                return httpx.Response(200, headers={"ETag": '"etag-1"'})
            return httpx.Response(200, content=b"<CompleteMultipartUploadResult/>")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            uploader = AliyunOssUploader(
                client,
                endpoint="https://oss.test",
                bucket="bucket",
                access_key_id="old-key",
                access_key_secret="old-secret",
                security_token="old-security-token",
                credential_provider=credentials,
                sleep=no_sleep,
            )
            with TemporaryDirectory() as tmp:
                path = Path(tmp) / "recording.ts"
                path.write_bytes(b"recording")
                await uploader.upload(path, "archive/recording.ts", part_size=100 * 1024)
        finally:
            await client.aclose()

        self.assertEqual(refreshes, 1)
        self.assertEqual(part_bodies, [b"recording", b"recording"])
        self.assertEqual(access_keys, ["old-key", "old-key", "new-key", "new-key"])

    async def test_sts_is_refreshed_before_the_expiration_window(self) -> None:
        refreshes = 0
        access_keys: list[str] = []

        async def credentials() -> OssCredentials:
            nonlocal refreshes
            refreshes += 1
            return OssCredentials(
                access_key_id="fresh-key",
                access_key_secret="fresh-secret",
                security_token="fresh-security-token",
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            )

        async def handler(request: httpx.Request) -> httpx.Response:
            access_keys.append(request.headers["authorization"].split(" ", 1)[1].split(":", 1)[0])
            if "uploads" in request.url.params:
                return httpx.Response(
                    200,
                    content=b"<InitiateMultipartUploadResult><UploadId>upload-1</UploadId></InitiateMultipartUploadResult>",
                )
            if "partNumber" in request.url.params:
                await request.aread()
                return httpx.Response(200, headers={"ETag": '"etag-1"'})
            return httpx.Response(200, content=b"<CompleteMultipartUploadResult/>")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            uploader = AliyunOssUploader(
                client,
                endpoint="https://oss.test",
                bucket="bucket",
                access_key_id="expiring-key",
                access_key_secret="expiring-secret",
                security_token="expiring-token",
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=1),
                credential_provider=credentials,
                sleep=no_sleep,
            )
            with TemporaryDirectory() as tmp:
                path = Path(tmp) / "recording.ts"
                path.write_bytes(b"recording")
                await uploader.upload(path, "archive/recording.ts", part_size=100 * 1024)
        finally:
            await client.aclose()

        self.assertEqual(refreshes, 1)
        self.assertEqual(access_keys, ["fresh-key", "fresh-key", "fresh-key"])


class BaiduNetdiskClientTests(IsolatedAsyncioTestCase):
    async def test_native_precreate_multipart_upload_and_verify(self) -> None:
        directories: dict[str, list[dict[str, object]]] = {"/": []}
        uploaded_parts: list[int] = []
        upload_meta: dict[str, object] = {}
        created = False

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal created
            if request.url.host == "upload.baidu.test":
                if "partseq" in request.url.params:
                    uploaded_parts.append(int(request.url.params["partseq"]))
                    await request.aread()
                    return httpx.Response(200, json={"errno": 0})
                return httpx.Response(404, json={"errno": 1})

            self.assertEqual(request.url.host, "pan.baidu.com")
            path = request.url.path.removeprefix("/rest/2.0")
            if path == "/xpan/nas":
                return httpx.Response(200, json={"errno": 0, "vip_type": 0})
            if path == "/xpan/file" and request.url.params.get("method") == "list":
                directory = request.url.params["dir"]
                return httpx.Response(200, json={"errno": 0, "list": directories.get(directory, [])})
            if path == "/rest/2.0/pcs/file":
                return httpx.Response(200, json={"errno": 0, "servers": [{"server": "https://upload.baidu.test"}]})
            if path != "/xpan/file":
                return httpx.Response(404, json={"errno": 1, "errmsg": "not found"})

            form = parse_qs((await request.aread()).decode())
            method = request.url.params.get("method")
            if method == "create":
                target = form["path"][0]
                if form.get("isdir") == ["1"]:
                    parent = str(Path(target).parent).replace("\\", "/")
                    directories.setdefault(target, [])
                    directories.setdefault(parent, []).append(
                        {
                            "fs_id": target,
                            "server_filename": Path(target).name,
                            "size": 0,
                            "isdir": 1,
                            "path": target,
                        }
                    )
                else:
                    parent = str(Path(target).parent).replace("\\", "/")
                    directories.setdefault(parent, []).append(
                        {
                            "fs_id": 123,
                            "server_filename": Path(target).name,
                            "size": int(form["size"][0]),
                            "isdir": 0,
                            "path": target,
                        }
                    )
                    created = True
                return httpx.Response(200, json={"errno": 0, "path": target})
            if method == "precreate":
                upload_meta.update({"path": form["path"][0], "size": int(form["size"][0])})
                return httpx.Response(
                    200,
                    json={"errno": 0, "return_type": 1, "uploadid": "upload-1", "block_list": [0, 1]},
                )
            return httpx.Response(404, json={"errno": 1, "errmsg": "unknown"})

        # The locate-upload request is sent to the default upload host, so use a
        # transport wrapper that handles it before the normal API handler.
        async def wrapped(request: httpx.Request) -> httpx.Response:
            if request.url.host == "d.pcs.baidu.com" and request.url.params.get("method") == "locateupload":
                return httpx.Response(200, json={"errno": 0, "servers": [{"server": "https://upload.baidu.test"}]})
            return await handler(request)

        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "recording.ts"
            path.write_bytes(b"x" * (4 * 1024 * 1024 + 123))
            client = BaiduNetdiskClient(
                access_token="access-token",
                transport=httpx.MockTransport(wrapped),
                sleep=no_sleep,
            )
            try:
                remote = f"{CLOUD_ARCHIVE_ROOT}/主播/recording.ts"
                self.assertTrue(await client.upload_verified(path, remote))
                self.assertFalse(await client.upload_verified(path, remote))
            finally:
                await client.aclose()

        self.assertEqual(uploaded_parts, [0, 1])
        self.assertTrue(created)
        self.assertEqual(upload_meta["path"], remote)

    async def test_concurrent_rejections_share_one_refresh_even_when_access_token_is_unchanged(self) -> None:
        expired_requests = 0
        refresh_requests = 0
        both_expired = asyncio.Event()
        updates: list[dict[str, str]] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal expired_requests, refresh_requests
            if request.url.host == "openapi.baidu.com":
                refresh_requests += 1
                return httpx.Response(
                    200,
                    json={
                        # Some providers renew validity without changing the token string.
                        "access_token": "same-access-token",
                        "refresh_token": "rotated-refresh-token",
                    },
                )
            self.assertEqual(request.url.host, "pan.baidu.com")
            if refresh_requests == 0:
                expired_requests += 1
                if expired_requests == 2:
                    both_expired.set()
                await both_expired.wait()
                return httpx.Response(200, json={"errno": 111, "errmsg": "expired"})
            return httpx.Response(200, json={"errno": 0, "vip_type": 0})

        async def save_credentials(state: dict[str, str]) -> None:
            updates.append(state)

        client = BaiduNetdiskClient(
            access_token="same-access-token",
            refresh_token="initial-refresh-token",
            client_id="client-id",
            client_secret="client-secret",
            on_credential_update=save_credentials,
            transport=httpx.MockTransport(handler),
        )
        try:
            first, second = await asyncio.gather(
                client._api_request("GET", "/xpan/nas", params={"method": "uinfo"}),
                client._api_request("GET", "/xpan/nas", params={"method": "uinfo"}),
            )
        finally:
            await client.aclose()

        self.assertEqual(first["errno"], 0)
        self.assertEqual(second["errno"], 0)
        self.assertEqual(expired_requests, 2)
        self.assertEqual(refresh_requests, 1)
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]["refresh_token"], "rotated-refresh-token")

    async def test_cookie_mode_uses_web_api_and_superfile2_md5s(self) -> None:
        directories: dict[str, list[dict[str, object]]] = {"/": []}
        uploaded_parts: list[int] = []
        created = False
        bdstoken_requests = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal created, bdstoken_requests
            if request.url.host == "upload.baidu.test":
                if "partseq" in request.url.params:
                    self.assertEqual(request.url.params["app_id"], "250528")
                    self.assertNotIn("access_token", request.url.params)
                    self.assertEqual(request.headers["cookie"].split(";")[0], "BDUSS=cookie-session")
                    await request.aread()
                    uploaded_parts.append(int(request.url.params["partseq"]))
                    return httpx.Response(200, json={"md5": f"server-md5-{request.url.params['partseq']}"})
                return httpx.Response(404, json={"errno": 1})

            self.assertEqual(request.url.host, "pan.baidu.com")
            self.assertEqual(request.headers["cookie"].split(";")[0], "BDUSS=cookie-session")
            if request.url.path == "/api/gettemplatevariable":
                bdstoken_requests += 1
                return httpx.Response(200, json={"errno": 0, "result": {"bdstoken": "web-bdstoken"}})
            if request.url.path == "/rest/2.0/xpan/nas":
                self.assertEqual(request.url.params["method"], "uinfo")
                self.assertEqual(request.url.params["app_id"], "250528")
                return httpx.Response(200, json={"errno": 0, "vip_type": 0})
            if request.url.path == "/rest/2.0/xpan/file" and request.url.params.get("method") == "list":
                directory = request.url.params["dir"]
                self.assertEqual(request.url.params["web"], "web")
                return httpx.Response(200, json={"errno": 0, "list": directories.get(directory, [])})
            if request.url.path == "/api/precreate":
                self.assertEqual(request.url.params["bdstoken"], "web-bdstoken")
                self.assertEqual(request.url.params["app_id"], "250528")
                form = parse_qs((await request.aread()).decode())
                self.assertEqual(form["path"][0], f"{CLOUD_ARCHIVE_ROOT}/主播/recording.ts")
                return httpx.Response(
                    200,
                    json={"errno": 0, "return_type": 1, "uploadid": "upload-1", "block_list": [0, 1]},
                )
            if request.url.path == "/api/create":
                self.assertEqual(request.url.params["bdstoken"], "web-bdstoken")
                form = parse_qs((await request.aread()).decode())
                if form.get("isdir") == ["1"]:
                    target = form["path"][0]
                    directories.setdefault(target, [])
                    directories.setdefault(str(Path(target).parent).replace("\\", "/"), []).append(
                        {"fs_id": target, "server_filename": Path(target).name, "size": 0, "isdir": 1, "path": target}
                    )
                    return httpx.Response(200, json={"errno": 0, "path": target})
                self.assertEqual(form["block_list"][0], '["server-md5-0","server-md5-1"]')
                self.assertEqual(form["uploadid"][0], "upload-1")
                parent = str(Path(form["path"][0]).parent).replace("\\", "/")
                directories.setdefault(parent, []).append(
                    {
                        "fs_id": 123,
                        "server_filename": "recording.ts",
                        "size": int(form["size"][0]),
                        "isdir": 0,
                        "path": form["path"][0],
                    }
                )
                created = True
                return httpx.Response(200, json={"errno": 0, "path": form["path"][0]})
            return httpx.Response(404, json={"errno": 1, "errmsg": "unknown"})

        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "recording.ts"
            path.write_bytes(b"x" * (4 * 1024 * 1024 + 123))
            client = BaiduNetdiskClient(
                cookie="BDUSS=cookie-session; STOKEN=netdisk-stoken",
                transport=httpx.MockTransport(handler),
                sleep=no_sleep,
                upload_base_url="https://upload.baidu.test",
            )
            try:
                remote = f"{CLOUD_ARCHIVE_ROOT}/主播/recording.ts"
                self.assertTrue(await client.upload_verified(path, remote))
            finally:
                await client.aclose()

        self.assertEqual(uploaded_parts, [0, 1])
        self.assertTrue(created)
        self.assertEqual(bdstoken_requests, 1)

    async def test_cookie_mode_never_touches_openapi_refresh(self) -> None:
        refresh_hits = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal refresh_hits
            if request.url.host == "openapi.baidu.com":
                refresh_hits += 1
                return httpx.Response(200, json={"access_token": "rotated"})
            if request.url.path == "/api/gettemplatevariable":
                return httpx.Response(200, json={"errno": 0, "result": {"bdstoken": "web-bdstoken"}})
            if request.url.path == "/api/create":
                return httpx.Response(200, json={"errno": 1, "errmsg": "boom"})
            raise AssertionError(f"unexpected request: {request.url}")

        client = BaiduNetdiskClient(
            cookie="BDUSS=cookie-session",
            transport=httpx.MockTransport(handler),
        )
        try:
            with self.assertRaisesRegex(CloudUploadError, "boom"):
                await client._api_request(
                    "POST",
                    "/xpan/file",
                    params={"method": "create"},
                    form={"isdir": "1", "path": "/x"},
                )
        finally:
            await client.aclose()
        self.assertEqual(refresh_hits, 0)


class Pan115ClientTests(IsolatedAsyncioTestCase):
    async def test_open_api_upload_uses_oss_callback_and_verifies(self) -> None:
        directories: dict[str, list[dict[str, object]]] = {"0": []}
        upload_parent = ""
        upload_name = ""
        upload_size = 0
        completed = False
        token_requests = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal upload_parent, upload_name, upload_size, completed, token_requests
            if request.url.host == "bucket.oss.test":
                if "uploads" in request.url.params:
                    return httpx.Response(
                        200,
                        content=b"<InitiateMultipartUploadResult><UploadId>u1</UploadId></InitiateMultipartUploadResult>",
                    )
                if "partNumber" in request.url.params:
                    await request.aread()
                    if "OSS ak-1:" in request.headers["authorization"]:
                        return httpx.Response(
                            403,
                            content=b"<Error><Code>SecurityTokenExpired</Code></Error>",
                        )
                    return httpx.Response(200, headers={"ETag": '"etag-1"'})
                if "uploadId" in request.url.params:
                    body = await request.aread()
                    self.assertIn(b"CompleteMultipartUpload", body)
                    self.assertIn("x-oss-callback", request.headers)
                    directories[upload_parent].append(
                        {"fid": "file-1", "fn": upload_name, "fc": "1", "fs": upload_size}
                    )
                    completed = True
                    return httpx.Response(200, content=b"<CompleteMultipartUploadResult/>")
                return httpx.Response(404)

            self.assertEqual(request.url.host, "proapi.115.test")
            path = request.url.path.removeprefix("/open")
            if path == "/ufile/files":
                parent = request.url.params["cid"]
                return httpx.Response(
                    200,
                    json={
                        "state": True,
                        "data": directories.get(parent, []),
                        "count": len(directories.get(parent, [])),
                    },
                )
            form = parse_qs((await request.aread()).decode())
            if path == "/folder/add":
                parent = form["pid"][0]
                name = form["file_name"][0]
                folder_id = f"folder-{len(directories)}"
                directories.setdefault(folder_id, [])
                directories[parent].append({"fid": folder_id, "fn": name, "fc": "0", "fs": 0})
                return httpx.Response(200, json={"state": True, "data": {"file_id": folder_id}})
            if path == "/upload/init":
                upload_parent = form["target"][0].removeprefix("U_1_")
                upload_name = form["file_name"][0]
                upload_size = int(form["file_size"][0])
                return httpx.Response(
                    200,
                    json={
                        "state": True,
                        "data": {
                            "status": 1,
                            "bucket": "bucket",
                            "object": "objects/recording.ts",
                            "callback": {
                                "callback": '{\\"state\\":true}',
                                "callback_var": '{\\"x\\":\\"${x:uid}\\"}',
                            },
                        },
                    },
                )
            if path == "/upload/get_token":
                token_requests += 1
                return httpx.Response(
                    200,
                    json={
                        "state": True,
                        "data": {
                            "endpoint": "https://oss.test",
                            "AccessKeyId": f"ak-{token_requests}",
                            "AccessKeySecret": f"secret-{token_requests}",
                            "SecurityToken": f"sts-{token_requests}",
                        },
                    },
                )
            return httpx.Response(404, json={"state": False, "code": 404, "message": "not found"})

        # Use the production API path shape while pointing the client at the mock host.
        transport = httpx.MockTransport(handler)
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "recording.ts"
            path.write_bytes(b"recording")
            client = Pan115Client(
                access_token="access-token",
                root_id="0",
                transport=transport,
                sleep=no_sleep,
                api_base_url="https://proapi.115.test",
                passport_base_url="https://passport.115.test",
            )
            try:
                remote = f"{CLOUD_ARCHIVE_ROOT}/主播/recording.ts"
                self.assertTrue(await client.upload_verified(path, remote))
            finally:
                await client.aclose()
        self.assertTrue(completed)
        self.assertEqual(token_requests, 2)


class Pan115CookieCryptoTests(TestCase):
    def test_raw_lz4_decoder_handles_literals_and_overlapping_matches(self) -> None:
        self.assertEqual(pan115_cookie_module._lz4_block_decompress(b"\x50hello"), b"hello")
        self.assertEqual(
            pan115_cookie_module._lz4_block_decompress(b"\x40abcd\x04\x00"),
            b"abcdabcd",
        )
        with self.assertRaises(CloudUploadError):
            pan115_cookie_module._lz4_block_decompress(b"\x00\x00\x00")
        with self.assertRaisesRegex(CloudUploadError, "匹配长度"):
            pan115_cookie_module._lz4_block_decompress(b"\x1f" + b"x\x01\x00")
        oversized_match = b"\x1f" + b"x\x01\x00" + b"\xff" * 32 + b"\x0d"
        with self.assertRaisesRegex(CloudUploadError, "解压数据过大"):
            pan115_cookie_module._lz4_block_decompress(oversized_match)

    def test_ec_cipher_matches_cbc_and_encodes_a_valid_crc_token(self) -> None:
        cipher = pan115_cookie_module._Ec115Cipher()
        plain = b"115 upload request"
        padding = AES.block_size - len(plain) % AES.block_size
        padded = plain + bytes([padding]) * padding
        expected = AES.new(cipher.key, AES.MODE_CBC, iv=cipher.iv).encrypt(padded)
        self.assertEqual(cipher.encrypt(plain), expected)

        compressed = b"\x20ok"
        server_plain = len(compressed).to_bytes(2, "little") + compressed
        server_plain += bytes((-len(server_plain)) % AES.block_size)
        server_ciphertext = AES.new(cipher.key, AES.MODE_CBC, iv=cipher.iv).encrypt(server_plain)
        self.assertEqual(cipher.decrypt(server_ciphertext), b"ok")
        invalid_length = (999).to_bytes(2, "little") + bytes(AES.block_size - 2)
        invalid_ciphertext = AES.new(cipher.key, AES.MODE_CBC, iv=cipher.iv).encrypt(invalid_length)
        with self.assertRaisesRegex(CloudUploadError, "无效压缩数据长度"):
            cipher.decrypt(invalid_ciphertext)

        timestamp = 0x12345678
        token = base64.b64decode(cipher.encode_token(timestamp), validate=True)
        self.assertEqual(len(token), 48)
        self.assertEqual(
            int.from_bytes(token[-4:], "little"),
            zlib.crc32(pan115_cookie_module._CRC_SALT + token[:-4]) & 0xFFFFFFFF,
        )
        first_mask = token[15]
        second_mask = token[39]
        self.assertEqual(bytes(value ^ first_mask for value in token[:15]), cipher.public_key[:15])
        self.assertEqual(
            bytes(value ^ first_mask for value in token[20:24]),
            struct.pack(">I", timestamp)[::-1],
        )
        self.assertEqual(bytes(value ^ second_mask for value in token[24:39]), cipher.public_key[15:])


class Pan115CookieClientTests(IsolatedAsyncioTestCase):
    async def test_cookie_upload_handles_range_check_oss_callback_and_verification(self) -> None:
        directories: dict[str, list[dict[str, object]]] = {"0": []}
        init_forms: list[dict[str, list[str]]] = []
        upload_parent = ""
        upload_name = ""
        upload_size = 0
        oss_completed = False

        class IdentityCipher:
            def encrypt(self, value: bytes) -> bytes:
                return value

            def decrypt(self, value: bytes) -> bytes:
                return value

            def encode_token(self, _timestamp: int) -> str:
                return "encoded-token"

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal upload_parent, upload_name, upload_size, oss_completed
            if request.url.host == "bucket.cn-shenzhen.oss.aliyuncs.com":
                self.assertNotIn("cookie", request.headers)
                if "uploads" in request.url.params:
                    return httpx.Response(
                        200,
                        content=b"<InitiateMultipartUploadResult><UploadId>u1</UploadId></InitiateMultipartUploadResult>",
                    )
                if "partNumber" in request.url.params:
                    await request.aread()
                    return httpx.Response(200, headers={"ETag": '"etag-1"'})
                if request.method == "POST" and "uploadId" in request.url.params:
                    await request.aread()
                    self.assertIn("x-oss-callback", request.headers)
                    self.assertIn("x-oss-callback-var", request.headers)
                    directories[upload_parent].append({"fid": "file-1", "n": upload_name, "s": upload_size})
                    oss_completed = True
                    return httpx.Response(200, content=b"<CompleteMultipartUploadResult/>")
                raise AssertionError(f"unexpected OSS request: {request.url}")

            self.assertEqual(request.headers["cookie"], "UID=uid;CID=cid;SEID=seid")
            if request.url.host == "webapi.115.com":
                if request.url.path == "/files":
                    parent = request.url.params["cid"]
                    entries = directories.get(parent, [])
                    return httpx.Response(200, json={"state": True, "data": entries, "count": len(entries)})
                if request.url.path == "/files/add":
                    form = parse_qs((await request.aread()).decode())
                    parent = form["pid"][0]
                    folder_id = f"folder-{len(directories)}"
                    directories[folder_id] = []
                    directories[parent].append({"cid": folder_id, "n": form["cname"][0], "s": 0})
                    return httpx.Response(200, json={"state": True, "data": {"cid": folder_id}})
            if request.url.host == "proapi.115.com" and request.url.path == "/app/uploadinfo":
                return httpx.Response(
                    200,
                    json={
                        "state": True,
                        "data": {"user_id": "115-user", "userkey": "user-key", "size_limit": 1024},
                    },
                )
            if request.url.host == "uplb.115.com" and request.url.path == "/4.0/initupload.php":
                form = parse_qs((await request.aread()).decode())
                init_forms.append(form)
                upload_parent = form["target"][0].removeprefix("U_1_")
                upload_name = form["filename"][0]
                upload_size = int(form["filesize"][0])
                if len(init_forms) == 1:
                    return httpx.Response(
                        200,
                        content=json.dumps({"status": 7, "sign_key": "range-key", "sign_check": "1-4"}).encode(),
                    )
                return httpx.Response(
                    200,
                    content=json.dumps(
                        {
                            "status": 1,
                            "bucket": "bucket",
                            "object": "archive/recording.ts",
                            "callback": {
                                "callback": '{"callbackUrl":"https://115.com/callback"}',
                                "callback_var": '{"x:uid":"115-user"}',
                            },
                        }
                    ).encode(),
                )
            if request.url.host == "uplb.115.com" and request.url.path == "/3.0/gettoken.php":
                return httpx.Response(
                    200,
                    json={
                        "StatusCode": "200",
                        "AccessKeyID": "access-key",
                        "AccessKeySecret": "access-secret",
                        "SecurityToken": "security-token",
                    },
                )
            raise AssertionError(f"unexpected request: {request.url}")

        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "recording.ts"
            content = b"recording"
            path.write_bytes(content)
            with patch.object(pan115_cookie_module, "_Ec115Cipher", IdentityCipher):
                client = Pan115CookieClient(
                    "UID=uid;CID=cid;SEID=seid",
                    transport=httpx.MockTransport(handler),
                    sleep=no_sleep,
                )
                try:
                    remote = f"{CLOUD_ARCHIVE_ROOT}/主播/recording.ts"
                    self.assertTrue(await client.upload_verified(path, remote))
                    self.assertFalse(await client.upload_verified(path, remote))
                finally:
                    await client.aclose()

        self.assertTrue(oss_completed)
        self.assertEqual(len(init_forms), 2)
        self.assertEqual(init_forms[0]["target"], ["U_1_folder-2"])
        self.assertEqual(init_forms[1]["sign_key"], ["range-key"])
        self.assertEqual(init_forms[1]["sign_val"], [hashlib.sha1(content[1:5]).hexdigest().upper()])

    async def test_cookie_upload_accepts_instant_upload_status(self) -> None:
        progress: list[tuple[str, int]] = []
        client = Pan115CookieClient(
            "UID=uid;CID=cid;SEID=seid",
            transport=httpx.MockTransport(lambda _request: httpx.Response(500)),
            sleep=no_sleep,
        )
        client._user_id = "115-user"
        client._user_key = "user-key"

        async def instant_init(*_args: object, **_kwargs: object) -> dict[str, object]:
            return {"status": 2}

        client._upload_init = instant_init  # type: ignore[method-assign]
        try:
            with TemporaryDirectory() as tmp:
                path = Path(tmp) / "recording.ts"
                path.write_bytes(b"recording")
                await client._upload(path, "0", path.name, lambda stage, size: progress.append((stage, size)))
        finally:
            await client.aclose()
        self.assertEqual(progress, [("uploading", len(b"recording"))])


class GuangYaPanClientTests(IsolatedAsyncioTestCase):
    async def test_upload_token_accepts_instant_upload_code_and_message(self) -> None:
        responses = iter(
            (
                {"code": 156, "msg": "instant", "data": {"taskId": "task-code"}},
                {"code": 999, "msg": "already uploaded", "data": {"taskId": "task-message"}},
            )
        )

        async def handler(request: httpx.Request) -> httpx.Response:
            self.assertTrue(request.url.path.endswith("/get_res_center_token"))
            return httpx.Response(200, json=next(responses))

        client = GuangYaPanClient(
            access_token="access-token",
            client_id="client-id",
            transport=httpx.MockTransport(handler),
            sleep=no_sleep,
            api_base_url="https://api.guangya.test",
        )
        try:
            token, already_done = await client._get_upload_token("parent", "recording.ts", 9)
            self.assertTrue(already_done)
            self.assertEqual(token["taskId"], "task-code")
            token, already_done = await client._get_upload_token("parent", "recording.ts", 9)
            self.assertTrue(already_done)
            self.assertEqual(token["taskId"], "task-message")
        finally:
            await client.aclose()

    async def test_token_api_and_oss_upload_verify(self) -> None:
        directories: dict[str, list[dict[str, object]]] = {"": []}
        parent_id = ""
        upload_name = ""
        upload_size = 0
        task_checks = 0
        upload_token_requests = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal parent_id, upload_name, upload_size, task_checks, upload_token_requests
            if request.url.host == "bucket.oss.test":
                if "uploads" in request.url.params:
                    return httpx.Response(
                        200,
                        content=b"<InitiateMultipartUploadResult><UploadId>g1</UploadId></InitiateMultipartUploadResult>",
                    )
                if "partNumber" in request.url.params:
                    await request.aread()
                    if "OSS ak-1:" in request.headers["authorization"]:
                        return httpx.Response(
                            403,
                            content=b"<Error><Code>SecurityTokenExpired</Code></Error>",
                        )
                    return httpx.Response(200, headers={"ETag": '"g-etag"'})
                if "uploadId" in request.url.params:
                    await request.aread()
                    directories[parent_id].append(
                        {"fileId": "g-file", "fileName": upload_name, "fileSize": upload_size, "resType": 1}
                    )
                    return httpx.Response(200, content=b"<CompleteMultipartUploadResult/>")

            self.assertEqual(request.url.host, "api.guangya.test")
            path = request.url.path
            body = request.content
            if path.endswith("/get_file_list"):
                import json

                payload = json.loads(body or b"{}")
                parent = payload.get("parentId", "")
                entries = directories.get(parent, [])
                return httpx.Response(
                    200,
                    json={"code": 0, "msg": "success", "data": {"total": len(entries), "list": entries}},
                )
            import json

            payload = json.loads(body or b"{}")
            if path.endswith("/create_dir"):
                folder_id = f"g-folder-{len(directories)}"
                directories[folder_id] = []
                directories.setdefault(payload.get("parentId", ""), []).append(
                    {"fileId": folder_id, "fileName": payload["dirName"], "fileSize": 0, "resType": 2}
                )
                return httpx.Response(200, json={"code": 0, "msg": "success", "data": {"fileId": folder_id}})
            if path.endswith("/get_res_center_token"):
                upload_token_requests += 1
                parent_id = payload["parentId"]
                upload_name = payload["name"]
                upload_size = int(payload["res"]["fileSize"])
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "msg": "success",
                        "data": {
                            "taskId": "gtask",
                            "objectPath": "objects/recording.ts",
                            "bucketName": "bucket",
                            "endPoint": "https://oss.test",
                            "creds": {
                                "accessKeyID": f"ak-{upload_token_requests}",
                                "secretAccessKey": f"secret-{upload_token_requests}",
                                "sessionToken": f"sts-{upload_token_requests}",
                            },
                        },
                    },
                )
            if path.endswith("/get_info_by_task_id"):
                task_checks += 1
                if task_checks == 1:
                    return httpx.Response(200, json={"code": 145, "msg": "processing", "data": {}})
                return httpx.Response(200, json={"code": 0, "msg": "success", "data": {"fileId": "g-file"}})
            raise AssertionError(path)

        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "recording.ts"
            path.write_bytes(b"recording")
            updates: list[dict[str, str]] = []

            async def save_update(state: dict[str, str]) -> None:
                updates.append(state)

            client = GuangYaPanClient(
                access_token="access-token",
                client_id="client-id",
                transport=httpx.MockTransport(handler),
                sleep=no_sleep,
                api_base_url="https://api.guangya.test",
                account_base_url="https://account.guangya.test",
                on_credential_update=save_update,
            )
            try:
                self.assertTrue(await client.upload_verified(path, f"{CLOUD_ARCHIVE_ROOT}/主播/recording.ts"))
            finally:
                await client.aclose()
        self.assertTrue(updates)
        self.assertEqual(len(client.device_id), 32)
        self.assertEqual(task_checks, 2)
        self.assertEqual(upload_token_requests, 2)


class CloudRegistryTests(IsolatedAsyncioTestCase):
    async def test_pan115_factory_prefers_cookie_and_falls_back_to_open_token(self) -> None:
        provider = CloudProviderConfig(
            name="pan115",
            enabled=True,
            credentials={"cookie": "UID=uid;CID=cid;SEID=seid", "access_token": "open-token"},
        )
        cookie_client = create_cloud_client(
            provider,
            provider.credentials,
            timeout_seconds=300,
            transport=httpx.MockTransport(lambda _request: httpx.Response(500)),
        )
        self.assertIsInstance(cookie_client, Pan115CookieClient)
        await cookie_client.aclose()

        open_provider = CloudProviderConfig(
            name="pan115",
            enabled=True,
            credentials={"access_token": "open-token", "refresh_token": "refresh-token"},
        )
        open_client = create_cloud_client(
            open_provider,
            open_provider.credentials,
            timeout_seconds=300,
            transport=httpx.MockTransport(lambda _request: httpx.Response(500)),
        )
        self.assertIsInstance(open_client, Pan115Client)
        await open_client.aclose()

    async def test_baidu_refresh_token_requires_client_credentials(self) -> None:
        provider = CloudProviderConfig(
            name="baidu",
            enabled=True,
            credentials={"access_token": "access", "refresh_token": "refresh"},
        )
        with self.assertRaisesRegex(ValueError, "Client ID"):
            provider.validate()

    async def test_legacy_config_is_migrated_and_new_providers_are_canonical(self) -> None:
        config = CloudArchiveConfig.from_dict(
            {
                "quark_enabled": True,
                "quark_cookie": "cookie",
                "quark_root_id": "0",
                "wopan_enabled": False,
                "wopan_root_id": "0",
                "wopan_family_id": "",
            }
        )
        config.validate()
        self.assertEqual(config.provider("quark").credentials["cookie"], "cookie")
        self.assertNotIn("baidu", {name for name, _ in config.targets})
        self.assertEqual(config.to_dict()["providers"]["pan115"]["options"]["root_id"], "0")
