import asyncio
import base64
import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

import httpx

from stream_keeper.cloud import CloudProviderConfig, CloudUploadError, Yun139Client, create_cloud_client
from stream_keeper.cloud.yun139_protocol import yun139_signature
from stream_keeper.settings import CLOUD_ARCHIVE_ROOT, Settings
from stream_keeper.web.store import TaskStore
from stream_keeper.web.uploader import RecordingUploadService


def authorization(days: int = 30, *, token: str = "token") -> str:
    expiration = int((time.time() + days * 86400) * 1000)
    return base64.b64encode(f"prefix:13800000000:{token}|a|b|{expiration}".encode()).decode()


async def no_sleep(_seconds: float) -> None:
    pass


class PersonalCloud:
    """Small protocol fixture that keeps uploaded bytes separate from visible files."""

    def __init__(self) -> None:
        self.entries = {"/": []}
        self.requests: list[httpx.Request] = []
        self.transferred: list[bytes] = []
        self.parts: dict[int, bytes] = {}
        self.file = {}
        self.folder_count = 0
        self.url_batches: list[list[int]] = []
        self.fail_first_part = False
        self.publish = True
        self.rapid = False
        self.exist = False
        self.omit_url = False
        self.complete_count = 0

    @staticmethod
    def ok(data: object = None) -> httpx.Response:
        payload = {"success": True}
        if data is not None:
            payload["data"] = data
        return httpx.Response(200, json=payload)

    def publish_file(self) -> None:
        if self.publish:
            self.entries[self.file["parentFileId"]].append({
                "fileId": "recording", "name": self.file["name"], "size": self.file["size"], "type": "file",
            })

    def urls(self, body: dict) -> dict:
        numbers = [part["partNumber"] for part in body["partInfos"]]
        self.url_batches.append(numbers)
        return {"partInfos": [
            {"partNumber": number, "uploadUrl": f"https://upload.test/{number}"}
            for number in reversed(numbers) if not (self.omit_url and number == 1)
        ]}

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        content = await request.aread()
        if request.url.host == "upload.test":
            if "authorization" in request.headers or "mcloud-sign" in request.headers:
                raise AssertionError("Account credentials leaked to the upload host")
            if int(request.headers["content-length"]) != len(content):
                raise AssertionError("Incorrect upload length")
            self.transferred.append(content)
            if self.fail_first_part and len(self.transferred) == 1:
                return httpx.Response(503)
            self.parts[int(request.url.path[1:])] = content
            return httpx.Response(200)
        if not request.headers["authorization"].startswith("Basic "):
            raise AssertionError("Missing account credentials")
        if len(request.headers["mcloud-sign"].split(",")) != 3:
            raise AssertionError("Missing signature")
        body = json.loads(content)
        path = request.url.path
        if path == "/user/route/qryRoutePolicy":
            if body["userInfo"]["accountName"] != "13800000000":
                raise AssertionError("Incorrect routed account")
            return self.ok({"routePolicyList": [{"modName": "personal", "httpsUrl": "https://personal.test/api"}]})
        if path == "/api/file/list":
            return self.ok({"items": self.entries[body["parentFileId"]], "nextPageCursor": ""})
        if path == "/api/file/create":
            if body["type"] == "folder":
                self.folder_count += 1
                folder_id = f"folder-{self.folder_count}"
                self.entries[body["parentFileId"]].append({
                    "fileId": folder_id, "name": body["name"], "type": "folder",
                })
                self.entries[folder_id] = []
                return self.ok({"fileId": folder_id})
            self.file = body
            if self.rapid or self.exist:
                self.publish_file()
                return self.ok({"fileId": "recording", "rapidUpload": self.rapid, "exist": self.exist})
            return self.ok({"fileId": "recording", "uploadId": "upload-1", "fileName": body["name"], **self.urls(body)})
        if path == "/api/file/getUploadUrl":
            return self.ok(self.urls(body))
        if path == "/api/file/complete":
            self.complete_count += 1
            uploaded = b"".join(self.parts[number] for number in sorted(self.parts))
            if body["contentHash"] != hashlib.sha256(uploaded).hexdigest():
                raise AssertionError("Uploaded bytes do not match the declared SHA256")
            self.publish_file()
            # Complete is allowed to return success without a data object.
            return self.ok()
        raise AssertionError(f"Unexpected API request: {request.url}")


class Yun139Tests(IsolatedAsyncioTestCase):
    def test_signature_matches_web_encode_uri_component_with_unicode(self) -> None:
        body = json.dumps({"name": "录像 !*'()~", "parentFileId": "/"}, ensure_ascii=False, separators=(",", ":"))
        self.assertEqual(
            yun139_signature(body, "2026-10-23 12:34:56", "0123456789abcdef"),
            "47C92C6C254AC59C6B30D0EE2CC14D97",
        )

    async def asyncSetUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "录像.ts"
        self.path.write_bytes(b"recorded-video")
        self.cloud = PersonalCloud()
        self.client = Yun139Client(
            "Basic " + authorization(), transport=httpx.MockTransport(self.cloud), sleep=no_sleep,
        )
        self.addAsyncCleanup(self.client.aclose)

    async def test_native_upload_creates_folders_retries_and_verifies(self) -> None:
        self.cloud.fail_first_part = True
        progress = []
        remote_path = f"{CLOUD_ARCHIVE_ROOT}/主播/录像.ts"
        created = await self.client.upload_verified(
            self.path, remote_path, progress=lambda stage, size: progress.append((stage, size)),
        )
        self.assertTrue(created)
        self.assertEqual(self.cloud.folder_count, 2)
        self.assertEqual(self.cloud.transferred, [b"recorded-video", b"recorded-video"])
        self.assertEqual(self.cloud.complete_count, 1)
        self.assertEqual(self.cloud.file["contentHash"], hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertEqual(progress[0], ("preparing", 0))
        self.assertEqual(progress[-1], ("verifying", self.path.stat().st_size))
        self.assertGreaterEqual(progress.count(("uploading", 0)), 2)
        self.assertFalse(await self.client.upload_verified(self.path, remote_path))
        self.assertEqual(self.cloud.complete_count, 1)

    async def test_more_than_100_parts_gets_additional_urls_and_orders_parts(self) -> None:
        self.path.write_bytes(bytes(range(101)))
        with patch.object(self.client, "_part_size", return_value=1):
            self.assertTrue(await self.client.upload_verified(self.path, "/录像.ts"))
        self.assertEqual([len(batch) for batch in self.cloud.url_batches], [100, 1])
        self.assertEqual(self.cloud.url_batches[-1], [101])
        self.assertEqual(b"".join(self.cloud.transferred), self.path.read_bytes())
        self.assertEqual(self.cloud.complete_count, 1)

    async def test_rapid_and_existing_uploads_are_still_verified(self) -> None:
        for mode in ("rapid", "exist"):
            with self.subTest(mode=mode):
                self.cloud.entries["/"] = []
                self.cloud.rapid = mode == "rapid"
                self.cloud.exist = mode == "exist"
                self.assertTrue(await self.client.upload_verified(self.path, "/录像.ts"))
        self.assertEqual(self.cloud.transferred, [])
        self.cloud.entries["/"] = []
        self.cloud.publish = False
        with self.assertRaisesRegex(CloudUploadError, "大小校验失败"):
            await self.client.upload_verified(self.path, "/录像.ts")

    async def test_missing_upload_url_does_not_complete(self) -> None:
        self.cloud.omit_url = True
        with self.assertRaisesRegex(CloudUploadError, "地址数量不完整"):
            await self.client.upload_verified(self.path, "/录像.ts")
        self.assertEqual(self.cloud.complete_count, 0)

    async def test_existing_conflicts_are_rejected_before_upload(self) -> None:
        for item in (
            {"fileId": "old", "name": "录像.ts", "type": "file", "size": 2},
            {"fileId": "old", "name": "录像.ts", "type": "folder"},
        ):
            with self.subTest(item=item):
                self.cloud.entries["/"] = [item]
                with self.assertRaises(CloudUploadError):
                    await self.client.upload_verified(self.path, "/录像.ts")
        self.assertEqual(self.cloud.transferred, [])

    async def test_directory_pagination_and_repeated_cursors(self) -> None:
        pages = []

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/file/list"):
                cursor = json.loads(request.content)["pageInfo"]["pageCursor"]
                pages.append(cursor)
                if not cursor:
                    return PersonalCloud.ok({"items": [], "nextPageCursor": "page-2"})
                return PersonalCloud.ok({"items": [{
                    "fileId": "recording", "name": "录像.ts", "type": "file", "size": 14,
                }], "nextPageCursor": "page-2" if repeat else ""})
            return await self.cloud(request)

        repeat = False
        client = Yun139Client(authorization(), transport=httpx.MockTransport(handler))
        try:
            self.assertEqual(await client.remote_size("/录像.ts"), 14)
            self.assertEqual(pages, ["", "page-2"])
            repeat = True
            with self.assertRaisesRegex(CloudUploadError, "分页游标重复"):
                await client.remote_size("/录像.ts")
        finally:
            await client.aclose()

    async def test_refresh_is_serialized_and_new_authorization_is_persisted(self) -> None:
        old, new = authorization(1), authorization(30, token="renewed")
        refresh_count = 0
        updates = []

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal refresh_count
            if request.url.path.endswith("authTokenRefresh.do"):
                refresh_count += 1
                await asyncio.sleep(0)
                token = base64.b64decode(new).decode().split(":", 2)[2]
                return httpx.Response(200, text=f"<root><return>0</return><token>{token}</token></root>")
            self.assertEqual(request.headers["authorization"], "Basic " + new)
            return await self.cloud(request)

        async def save(values: dict[str, str]) -> None:
            updates.append(values)

        client = Yun139Client(old, transport=httpx.MockTransport(handler), on_credential_update=save)
        try:
            await asyncio.gather(*(client.remote_size("/missing.ts") for _ in range(3)))
        finally:
            await client.aclose()
        self.assertEqual(refresh_count, 1)
        self.assertEqual(updates, [{"authorization": new}])

    async def test_invalid_or_expired_authorization_fails_without_network(self) -> None:
        for value in ("bad-token", base64.b64encode(b"a:b:c").decode()):
            with self.assertRaisesRegex(CloudUploadError, "格式无效"):
                Yun139Client(value)
        client = Yun139Client(authorization(-1), transport=httpx.MockTransport(self.cloud))
        try:
            with self.assertRaisesRegex(CloudUploadError, "已过期"):
                await client.remote_size("/missing.ts")
        finally:
            await client.aclose()
        self.assertEqual(self.cloud.requests, [])

    async def test_missing_route_and_invalid_file_metadata_fail_closed(self) -> None:
        for scenario in ("no-route", "missing-size", "no-list"):
            with self.subTest(scenario=scenario):
                async def handler(request: httpx.Request, scenario=scenario) -> httpx.Response:
                    if request.url.path.endswith("/file/list"):
                        if scenario == "no-list":
                            return PersonalCloud.ok({})
                        return PersonalCloud.ok({"items": [{"fileId": "x", "name": "录像.ts", "type": "file"}]})
                    if scenario == "no-route":
                        return PersonalCloud.ok({"routePolicyList": []})
                    return await self.cloud(request)

                client = Yun139Client(authorization(), transport=httpx.MockTransport(handler))
                try:
                    with self.assertRaises(CloudUploadError):
                        await client.upload_verified(self.path, "/录像.ts")
                finally:
                    await client.aclose()
        self.assertEqual(self.cloud.complete_count, 0)

    async def test_factory_uses_personal_cloud_options(self) -> None:
        provider = CloudProviderConfig(
            name="yun139", enabled=True, credentials={"authorization": authorization()},
            options={"root_id": "custom-root", "client_type": "pc"},
        )
        provider.validate()
        client = create_cloud_client(provider, provider.credentials, timeout_seconds=300)
        try:
            self.assertIsInstance(client, Yun139Client)
            self.assertEqual(client.root_id, "custom-root")
            self.assertEqual(client.client_type, "pc")
        finally:
            await client.aclose()

    async def test_archive_service_retains_local_file_until_remote_verification_succeeds(self) -> None:
        root = Path(self.temp.name)
        settings = Settings(
            data_dir=root, recordings_dir=root / "recordings", database_path=root / "tasks.db",
            yun139_authorization=authorization(), upload_min_age_minutes=0, validate_binaries=False,
        )
        settings.prepare()
        recording = settings.recordings_dir / "主播" / "录像.ts"
        recording.parent.mkdir()
        recording.write_bytes(self.path.read_bytes())
        store = TaskStore(settings.database_path)
        await store.initialize()

        def factory(target):
            self.assertEqual(target.name, "yun139")
            return Yun139Client(
                settings.yun139_authorization, transport=httpx.MockTransport(self.cloud), sleep=no_sleep,
            )

        self.cloud.publish = False
        service = RecordingUploadService(
            settings, store, client_factory=factory, clock=lambda: datetime.now(timezone.utc) + timedelta(minutes=1),
        )
        failed = await service.run_once()
        self.assertEqual(failed.failed_files, 1)
        self.assertEqual(failed.deleted_files, 0)
        self.assertTrue(recording.exists())
        self.cloud.publish = True
        succeeded = await service.run_once()
        self.assertEqual(succeeded.failed_files, 0)
        self.assertEqual(succeeded.deleted_files, 1)
        self.assertFalse(recording.exists())
