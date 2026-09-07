import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase

import httpx

from stream_keeper.cloud import CloudUploadError, WoPanClient


async def no_sleep(_seconds: float) -> None:
    return None


class WoPanClientTests(IsolatedAsyncioTestCase):
    async def test_native_encrypted_upload_refreshes_tokens_and_verifies(self) -> None:
        directories: dict[str, list[dict[str, object]]] = {"0": []}
        credential_updates: list[dict[str, str]] = []
        progress: list[tuple[str, int]] = []
        first_query_expired = True
        created_counter = 0
        upload_requests = 0
        client: WoPanClient

        def dispatcher_response(data: object, channel: str = "wohome") -> httpx.Response:
            encrypted = client._encrypt(data, channel)  # Protocol-level fixture for the mocked server.
            return httpx.Response(
                200,
                json={
                    "STATUS": "200",
                    "MSG": "ok",
                    "RSP": {"RSP_CODE": "0000", "RSP_DESC": "ok", "DATA": encrypted},
                },
            )

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal first_query_expired, created_counter, upload_requests
            if request.url.host == "upload.example":
                upload_requests += 1
                body = await request.aread()
                self.assertIn(b'name="totalPart"\r\n\r\n1', body)
                self.assertIn(b'name="partSize"\r\n\r\n6', body)
                self.assertIn(b'name="partIndex"\r\n\r\n1', body)
                self.assertIn(b'filename="remote.ts"', body)
                self.assertIn(b"abcdef", body)
                archive = next(entry for entry in directories["0"] if entry["name"] == "DouYinStreamKeeper")
                directories[str(archive["id"])].append(
                    {"id": "file-1", "fid": "fid-1", "name": "remote.ts", "size": 6, "type": 1}
                )
                return httpx.Response(200, json={"code": "0000", "data": {"fid": "fid-1"}, "msg": "ok"})

            self.assertEqual(request.url.host, "panservice.mail.wo.cn")
            request_body = json.loads(await request.aread())
            header = request_body["header"]
            channel = header["channel"]
            key = header["key"]
            sign_value = f"{key}{header['resTime']}{header['reqSeq']}{channel}{header['version']}".encode()
            self.assertEqual(header["sign"], hashlib.md5(sign_value).hexdigest())
            param = client._decrypt(request_body["body"]["param"], channel)

            if key == "QueryAllFiles" and first_query_expired:
                first_query_expired = False
                return httpx.Response(
                    200,
                    json={
                        "STATUS": "200",
                        "MSG": "expired",
                        "RSP": {"RSP_CODE": "1001", "RSP_DESC": "无效登录信息", "DATA": ""},
                    },
                )
            if key == "AppRefreshToken":
                self.assertEqual(param["refreshToken"], "old-refresh-token")
                return httpx.Response(
                    200,
                    json={
                        "STATUS": "200",
                        "MSG": "ok",
                        "RSP": {
                            "RSP_CODE": "0000",
                            "RSP_DESC": "ok",
                            "DATA": {
                                "access_token": "new-access-token-123456",
                                "refresh_token": "new-refresh-token",
                            },
                        },
                    },
                )
            if key == "QueryAllFiles":
                parent = str(param["parentDirectoryId"])
                return dispatcher_response({"files": directories.get(parent, [])})
            if key == "CreateDirectory":
                if "familyId" in param and not param["familyId"]:
                    # Matches the live API: personal space rejects the key rather than ignoring it.
                    return httpx.Response(
                        200,
                        json={
                            "STATUS": "200",
                            "MSG": "ok",
                            "RSP": {"RSP_CODE": "9999", "RSP_DESC": "系统异常", "DATA": ""},
                        },
                    )
                created_counter += 1
                directory_id = f"directory-{created_counter}"
                parent = str(param["parentDirectoryId"])
                directories.setdefault(parent, []).append(
                    {"id": directory_id, "fid": "0", "name": param["directoryName"], "size": 0, "type": 0}
                )
                directories[directory_id] = []
                return dispatcher_response({"id": directory_id})
            if key == "GetZoneInfo":
                return dispatcher_response({"url": "https://upload.example"})
            return httpx.Response(500, text=f"unexpected key {key}")

        async def save_credentials(state: dict[str, str]) -> None:
            credential_updates.append(state)

        transport = httpx.MockTransport(handler)
        client = WoPanClient(
            "old-access-token-123456",
            "old-refresh-token",
            on_credential_update=save_credentials,
            transport=transport,
            sleep=no_sleep,
        )
        with TemporaryDirectory() as tmp:
            local_path = Path(tmp) / "local.ts"
            local_path.write_bytes(b"abcdef")
            try:
                self.assertTrue(
                    await client.upload_verified(
                        local_path,
                        "/DouYinStreamKeeper/remote.ts",
                        progress=lambda stage, uploaded: progress.append((stage, uploaded)),
                    )
                )
                self.assertFalse(await client.upload_verified(local_path, "/DouYinStreamKeeper/remote.ts"))
            finally:
                await client.aclose()

        # Consecutive repeats only mark phase boundaries, so collapse them.
        self.assertEqual(
            [event for index, event in enumerate(progress) if index == 0 or progress[index - 1] != event],
            [("preparing", 0), ("uploading", 6), ("verifying", 6)],
        )

        self.assertEqual(upload_requests, 1)
        self.assertEqual(created_counter, 1)
        self.assertEqual([entry["name"] for entry in directories["0"]], ["DouYinStreamKeeper"])
        self.assertEqual(
            credential_updates,
            [{"access_token": "new-access-token-123456", "refresh_token": "new-refresh-token"}],
        )

    async def test_dispatcher_refresh_is_bounded_and_only_handles_auth_errors(self) -> None:
        for code, refresh_rejected, expected_calls in (
            (1001, False, ["QueryAllFiles", "AppRefreshToken", "QueryAllFiles"]),
            ("9999", False, ["QueryAllFiles", "AppRefreshToken", "QueryAllFiles"]),
            ("1001", True, ["QueryAllFiles", "AppRefreshToken"]),
            ("1009", False, ["QueryAllFiles"]),
        ):
            with self.subTest(code=code, refresh_rejected=refresh_rejected):
                calls: list[str] = []

                async def handler(
                    request: httpx.Request,
                    calls: list[str] = calls,
                    code: str | int = code,
                    refresh_rejected: bool = refresh_rejected,
                ) -> httpx.Response:
                    key = json.loads(await request.aread())["header"]["key"]
                    calls.append(key)
                    result: dict[str, object] = {"RSP_CODE": code, "RSP_DESC": "rejected"}
                    if key == "AppRefreshToken" and not refresh_rejected:
                        result = {
                            "RSP_CODE": "0000",
                            "DATA": {"access_token": "new-access-token-123456", "refresh_token": "new-refresh"},
                        }
                    return httpx.Response(200, json={"STATUS": "200", "RSP": result})

                client = WoPanClient(
                    "old-access-token-123456", "old-refresh", transport=httpx.MockTransport(handler)
                )
                try:
                    with self.assertRaisesRegex(CloudUploadError, f"code={code}"):
                        await client.remote_size("/remote.ts")
                finally:
                    await client.aclose()
                self.assertEqual(calls, expected_calls)

    async def test_invalid_login_without_refresh_token_reports_missing_credential(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"STATUS": "200", "RSP": {"RSP_CODE": "1001", "RSP_DESC": "无效登录信息"}}
            )

        client = WoPanClient("old-access-token-123456", transport=httpx.MockTransport(handler))
        try:
            with self.assertRaisesRegex(CloudUploadError, "未配置 refresh token"):
                await client.remote_size("/remote.ts")
        finally:
            await client.aclose()

    async def test_upload_part_refreshes_and_reencrypts_before_retry(self) -> None:
        for code in ("1001", "9999"):
            with self.subTest(code=code):
                await self._assert_upload_part_refresh(code)

    async def _assert_upload_part_refresh(self, code: str) -> None:
        calls: list[str] = []
        updates: list[dict[str, str]] = []
        old_token = "old-access-token-123456"
        new_token = "new-access-token-123456"
        file_info: dict[str, object] = {"fileName": "remote.ts", "fileSize": 6}
        client: WoPanClient

        async def save_credentials(state: dict[str, str]) -> None:
            updates.append(state)

        async def handler(request: httpx.Request) -> httpx.Response:
            body = await request.aread()
            if request.url.host == "upload.example":
                calls.append("upload")
                token = old_token if len(calls) == 1 else new_token
                self.assertIn(b'name="accessToken"\r\n\r\n' + token.encode(), body)
                encrypted = client._encrypt(file_info, "wohome", access_token=token).encode()
                self.assertIn(b'name="fileInfo"\r\n\r\n' + encrypted, body)
                self.assertIn(b"abcdef", body)
                if len(calls) == 1:
                    return httpx.Response(200, json={"code": code, "msg": "无效登录信息"})
                self.assertEqual(len(updates), 1)
                return httpx.Response(200, json={"code": "0000"})
            self.assertEqual(json.loads(body)["header"]["key"], "AppRefreshToken")
            calls.append("refresh")
            return httpx.Response(
                200,
                json={
                    "STATUS": "200",
                    "RSP": {
                        "RSP_CODE": "0000",
                        "DATA": {"access_token": new_token, "refresh_token": "new-refresh"},
                    },
                },
            )

        client = WoPanClient(
            old_token,
            "old-refresh",
            on_credential_update=save_credentials,
            transport=httpx.MockTransport(handler),
            sleep=no_sleep,
        )
        try:
            with TemporaryDirectory() as tmp:
                path = Path(tmp) / "local.ts"
                path.write_bytes(b"abcdef")
                await client._upload_part(
                    "https://upload.example/openapi/client/upload2C",
                    path,
                    fields={},
                    file_info=file_info,
                    filename="remote.ts",
                    content_type="video/mp2t",
                    offset=0,
                    size=6,
                    part_index=1,
                )
        finally:
            await client.aclose()
        self.assertEqual(calls, ["upload", "refresh", "upload"])
        self.assertEqual(updates, [{"access_token": new_token, "refresh_token": "new-refresh"}])

    async def _capture_create_directory_param(self, family_id: str) -> dict[str, object]:
        created_params: list[dict[str, object]] = []
        client: WoPanClient

        def dispatcher_response(data: object) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "STATUS": "200",
                    "MSG": "ok",
                    "RSP": {"RSP_CODE": "0000", "RSP_DESC": "ok", "DATA": client._encrypt(data, "wohome")},
                },
            )

        async def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(await request.aread())
            param = client._decrypt(body["body"]["param"], "wohome")
            if body["header"]["key"] == "QueryAllFiles":
                return dispatcher_response({"files": []})
            self.assertEqual(body["header"]["key"], "CreateDirectory")
            created_params.append(param)
            return dispatcher_response({"id": "directory-1"})

        client = WoPanClient(
            "access-token-1234567890",
            "refresh-token",
            family_id=family_id,
            transport=httpx.MockTransport(handler),
            sleep=no_sleep,
        )
        try:
            self.assertEqual(await client._walk_directories(["归档"], create=True), "directory-1")
        finally:
            await client.aclose()

        self.assertEqual(len(created_params), 1)
        return created_params[0]

    async def test_create_directory_only_sends_family_id_inside_family_space(self) -> None:
        """Personal space answers 9999 系统异常 when the request carries an empty familyId."""

        personal = await self._capture_create_directory_param("")
        self.assertEqual(personal["spaceType"], "0")
        self.assertNotIn("familyId", personal)

        family = await self._capture_create_directory_param("family-1")
        self.assertEqual(family["spaceType"], "1")
        self.assertEqual(family["familyId"], "family-1")

    async def test_upload_sanitizes_emoji_remote_path(self) -> None:
        """Anchor names with emoji must be folded before CreateDirectory and upload2C."""

        directories: dict[str, list[dict[str, object]]] = {"0": []}
        created_names: list[str] = []
        leaf_id = "0"
        upload_body: bytes | None = None
        client: WoPanClient

        def dispatcher_response(data: object) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "STATUS": "200",
                    "MSG": "ok",
                    "RSP": {"RSP_CODE": "0000", "RSP_DESC": "ok", "DATA": client._encrypt(data, "wohome")},
                },
            )

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal upload_body, leaf_id
            if request.url.host == "upload.example":
                upload_body = await request.aread()
                directories[leaf_id].append(
                    {
                        "id": "file-1",
                        "fid": "fid-1",
                        "name": "兔兔兔奶糖_2026-08-01_00-00-00_000.ts",
                        "size": 6,
                        "type": 1,
                    }
                )
                return httpx.Response(200, json={"code": "0000", "data": {"fid": "fid-1"}, "msg": "ok"})

            self.assertEqual(request.url.host, "panservice.mail.wo.cn")
            body = json.loads(await request.aread())
            param = client._decrypt(body["body"]["param"], "wohome")
            key = body["header"]["key"]
            if key == "QueryAllFiles":
                return dispatcher_response({"files": directories.get(str(param["parentDirectoryId"]), [])})
            if key == "CreateDirectory":
                created_names.append(str(param["directoryName"]))
                leaf_id = f"directory-{len(created_names)}"
                parent = str(param["parentDirectoryId"])
                directories.setdefault(parent, []).append(
                    {"id": leaf_id, "fid": "0", "name": param["directoryName"], "size": 0, "type": 0}
                )
                directories[leaf_id] = []
                return dispatcher_response({"id": leaf_id})
            if key == "GetZoneInfo":
                return dispatcher_response({"url": "https://upload.example"})
            return httpx.Response(500, text=f"unexpected key {key}")

        client = WoPanClient(
            "access-token-1234567890",
            "refresh-token",
            transport=httpx.MockTransport(handler),
            sleep=no_sleep,
        )
        try:
            with TemporaryDirectory() as tmp:
                local_path = Path(tmp) / "local.ts"
                local_path.write_bytes(b"abcdef")
                remote_path = "/DouYinStreamKeeper/兔兔兔奶糖🍬/2026-08-01/兔兔兔奶糖🍬_2026-08-01_00-00-00_000.ts"
                self.assertTrue(await client.upload_verified(local_path, remote_path))
                # The dedup check walks the sanitized path and finds the same size.
                self.assertFalse(await client.upload_verified(local_path, remote_path))
        finally:
            await client.aclose()

        self.assertEqual(created_names, ["DouYinStreamKeeper", "兔兔兔奶糖", "2026-08-01"])
        assert upload_body is not None
        self.assertIn('filename="兔兔兔奶糖_2026-08-01_00-00-00_000.ts"'.encode(), upload_body)

    def test_sanitize_name_folds_characters_wopan_rejects(self) -> None:
        sanitize = WoPanClient._sanitize_name

        self.assertEqual(sanitize("兔兔兔奶糖🍬"), "兔兔兔奶糖")
        self.assertEqual(sanitize("猫猫k-"), "猫猫k-")
        self.assertEqual(sanitize('a/b\\c:d*e?f"g<h>i|j'), "a_b_c_d_e_f_g_h_i_j")
        self.assertEqual(sanitize("a\x00b\x1fc"), "a_b_c")
        self.assertEqual(sanitize("   🍬   "), "未命名")
        self.assertEqual(sanitize(""), "未命名")
        self.assertEqual(sanitize("🍬", fallback="recording"), "recording")
        # CJK and everyday punctuation survive, and the result is idempotent.
        self.assertEqual(sanitize("兔兔兔奶糖 直播（高清）"), "兔兔兔奶糖 直播（高清）")
        self.assertEqual(sanitize(sanitize("a🍬b/ c")), sanitize("a🍬b/ c"))
