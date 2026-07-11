import base64
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from fastapi.testclient import TestClient

from douyin_recorder.models import LiveInfo
from douyin_recorder.settings import Settings
from douyin_recorder.web.app import create_app
from douyin_recorder.web.schemas import TaskStatus
from douyin_recorder.web.store import TaskStore


class FakeScheduler:
    def __init__(self, store: TaskStore) -> None:
        self.store = store
        self.started: list[str] = []
        self.stopped: list[str] = []
        self.active_task_count = 0
        self.recording_task_count = 0

    async def startup(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    async def start(self, task_id: str):
        self.started.append(task_id)
        return await self.store.update_runtime(
            task_id,
            enabled=True,
            status=TaskStatus.WAITING,
            status_message="任务已启动",
        )

    async def stop(self, task_id: str, *, disable: bool = True):
        self.stopped.append(task_id)
        if await self.store.get(task_id) is None:
            return None
        return await self.store.update_runtime(
            task_id,
            enabled=not disable,
            status=TaskStatus.STOPPED,
            status_message="任务已停止",
        )

    async def restart(self, task_id: str):
        await self.stop(task_id, disable=False)
        return await self.start(task_id)


class FakeInspectClient:
    async def fetch(self, url: str, quality: str) -> LiveInfo:
        return LiveInfo(
            platform="抖音",
            anchor_name="测试主播",
            is_live=True,
            title="测试直播",
            quality=quality,
            m3u8_url="https://secret.example/live.m3u8?token=secret",
            flv_url="https://secret.example/live.flv?token=secret",
            record_url="https://secret.example/live.m3u8?token=secret",
            live_url=url,
        )


class WebTests(TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.settings = Settings(
            data_dir=root,
            recordings_dir=root / "recordings",
            database_path=root / "tasks.db",
            web_username="admin",
            web_password="secret-password",
            validate_binaries=False,
        )
        self.store = TaskStore(self.settings.database_path)
        self.scheduler = FakeScheduler(self.store)
        app = create_app(
            self.settings,
            store=self.store,
            scheduler=self.scheduler,
            inspect_client_factory=FakeInspectClient,
        )
        token = base64.b64encode(b"admin:secret-password").decode()
        self.auth_headers = {"Authorization": f"Basic {token}"}
        self.client_context = TestClient(app)
        self.client = self.client_context.__enter__()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        self.temp_dir.cleanup()

    def test_health_is_public_but_application_requires_auth(self) -> None:
        self.assertEqual(self.client.get("/health").status_code, 200)
        unauthorized = self.client.get("/")
        self.assertEqual(unauthorized.status_code, 401)
        self.assertIn("Basic", unauthorized.headers["www-authenticate"])

        page = self.client.get("/", headers=self.auth_headers)
        self.assertEqual(page.status_code, 200)
        self.assertIn("抖音直播录制", page.text)
        self.assertEqual(page.headers["x-frame-options"], "DENY")

        docs = self.client.get("/api/docs", headers=self.auth_headers)
        self.assertEqual(docs.status_code, 200)
        self.assertIn("cdn.jsdelivr.net", docs.headers["content-security-policy"])

    def test_create_list_stop_and_delete_task(self) -> None:
        response = self.client.post(
            "/api/tasks",
            headers=self.auth_headers,
            json={
                "url": "https://live.douyin.com/123456789",
                "label": "API 测试",
                "quality": "HD",
                "output_format": "ts",
                "source": "auto",
                "segment_seconds": 600,
                "monitor": True,
                "interval_seconds": 60,
                "auto_start": True,
            },
        )
        self.assertEqual(response.status_code, 201)
        task = response.json()
        self.assertTrue(task["enabled"])
        self.assertEqual(task["status"], "waiting")
        self.assertEqual(self.scheduler.started, [task["id"]])

        listed = self.client.get("/api/tasks", headers=self.auth_headers).json()
        self.assertEqual([item["id"] for item in listed], [task["id"]])

        stopped = self.client.post(f"/api/tasks/{task['id']}/stop", headers=self.auth_headers)
        self.assertEqual(stopped.status_code, 200)
        self.assertFalse(stopped.json()["enabled"])

        deleted = self.client.delete(f"/api/tasks/{task['id']}", headers=self.auth_headers)
        self.assertEqual(deleted.status_code, 204)
        self.assertEqual(self.client.get("/api/tasks", headers=self.auth_headers).json(), [])

    def test_inspect_does_not_expose_signed_stream_urls(self) -> None:
        response = self.client.post(
            "/api/inspect",
            headers=self.auth_headers,
            json={"url": "https://live.douyin.com/123456789", "quality": "OD"},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["has_flv"])
        self.assertTrue(payload["has_hls"])
        self.assertNotIn("flv_url", payload)
        self.assertNotIn("m3u8_url", payload)
        self.assertNotIn("secret", response.text)

    def test_rejects_unknown_fields_and_invalid_url(self) -> None:
        response = self.client.post(
            "/api/tasks",
            headers=self.auth_headers,
            json={"url": "https://example.com/live", "unknown": True},
        )
        self.assertEqual(response.status_code, 422)
