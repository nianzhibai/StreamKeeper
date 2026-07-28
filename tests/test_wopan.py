import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase

import httpx

from douyin_recorder.cloud import WoPanClient


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
                        "RSP": {"RSP_CODE": "9999", "RSP_DESC": "expired", "DATA": ""},
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
