from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from fastapi.testclient import TestClient

from douyin_recorder.models import LiveInfo
from douyin_recorder.settings import Settings
from douyin_recorder.web.app import create_app
from douyin_recorder.web.auth import SESSION_COOKIE_NAME
from douyin_recorder.web.schemas import TaskStatus
from douyin_recorder.web.store import TaskStore


class FakeScheduler:
    def __init__(self, store: TaskStore) -> None:
        self.store = store
        self.started: list[str] = []
        self.stopped: list[str] = []
        self.restarted: list[str] = []
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
        self.restarted.append(task_id)
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
        self.client_context = TestClient(app)
        self.client = self.client_context.__enter__()
        self.csrf_headers: dict[str, str] = {}

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        self.temp_dir.cleanup()

    def login(self) -> dict:
        response = self.client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "secret-password"},
        )
        self.assertEqual(response.status_code, 200)
        self.login_response = response
        payload = response.json()
        self.csrf_headers = {"X-CSRF-Token": payload["csrf_token"]}
        return payload

    def test_public_login_and_session_lifecycle(self) -> None:
        self.assertEqual(self.client.get("/health").status_code, 200)
        login_page = self.client.get("/login")
        self.assertEqual(login_page.status_code, 200)
        self.assertIn('<h1 id="login-title">登录</h1>', login_page.text)
        self.assertEqual(self.client.get("/static/login.js").status_code, 200)

        unauthorized_page = self.client.get("/", follow_redirects=False)
        self.assertEqual(unauthorized_page.status_code, 303)
        self.assertTrue(unauthorized_page.headers["location"].startswith("/login?next="))
        unauthorized_api = self.client.get("/api/tasks")
        self.assertEqual(unauthorized_api.status_code, 401)
        self.assertNotIn("www-authenticate", unauthorized_api.headers)

        rejected = self.client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "wrong-password"},
        )
        self.assertEqual(rejected.status_code, 401)
        self.assertNotIn("set-cookie", rejected.headers)

        session = self.login()
        self.assertEqual(session["username"], "admin")
        self.assertTrue(session["csrf_token"])
        cookie = self.client.cookies.get(SESSION_COOKIE_NAME)
        self.assertTrue(cookie)
        set_cookie = self.login_response.headers["set-cookie"].lower()
        self.assertIn("httponly", set_cookie)
        self.assertIn("samesite=strict", set_cookie)

        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("DouYinStreamKeeper", page.text)
        self.assertEqual(page.headers["x-frame-options"], "DENY")
        current = self.client.get("/api/auth/session")
        self.assertEqual(current.status_code, 200)
        self.assertEqual(current.json()["username"], "admin")

        docs = self.client.get("/api/docs")
        self.assertEqual(docs.status_code, 200)
        self.assertIn("cdn.jsdelivr.net", docs.headers["content-security-policy"])

        logout = self.client.post("/api/auth/logout", headers=self.csrf_headers)
        self.assertEqual(logout.status_code, 204)
        self.assertIsNone(self.client.cookies.get(SESSION_COOKIE_NAME))
        self.assertEqual(self.client.get("/api/auth/session").status_code, 401)

    def test_csrf_rejects_cross_origin_mutation(self) -> None:
        self.login()
        payload = {
            "url": "https://live.douyin.com/123456789",
            "auto_start": False,
        }
        missing_token = self.client.post("/api/tasks", json=payload)
        self.assertEqual(missing_token.status_code, 403)

        cross_origin = self.client.post(
            "/api/tasks",
            headers={"Origin": "http://attacker.example"},
            json=payload,
        )
        self.assertEqual(cross_origin.status_code, 403)

        accepted = self.client.post("/api/tasks", headers=self.csrf_headers, json=payload)
        self.assertEqual(accepted.status_code, 201)

    def test_create_list_stop_and_delete_task(self) -> None:
        self.login()
        response = self.client.post(
            "/api/tasks",
            headers=self.csrf_headers,
            json={
                "url": "https://live.douyin.com/123456789",
                "label": "API 测试",
                "quality": "HD",
                "output_format": "ts",
                "source": "auto",
                "segment_seconds": 600,
                "segment_count": 4,
                "monitor": True,
                "interval_seconds": 60,
                "auto_start": True,
            },
        )
        self.assertEqual(response.status_code, 201)
        task = response.json()
        self.assertTrue(task["enabled"])
        self.assertEqual(task["status"], "waiting")
        self.assertEqual(task["segment_count"], 4)
        self.assertEqual(self.scheduler.started, [task["id"]])

        listed = self.client.get("/api/tasks").json()
        self.assertEqual([item["id"] for item in listed], [task["id"]])

        stopped = self.client.post(f"/api/tasks/{task['id']}/stop", headers=self.csrf_headers)
        self.assertEqual(stopped.status_code, 200)
        self.assertFalse(stopped.json()["enabled"])

        deleted = self.client.delete(f"/api/tasks/{task['id']}", headers=self.csrf_headers)
        self.assertEqual(deleted.status_code, 204)
        self.assertEqual(self.client.get("/api/tasks").json(), [])

    def test_create_task_accepts_share_text_and_stores_clean_url(self) -> None:
        self.login()
        response = self.client.post(
            "/api/tasks",
            headers=self.csrf_headers,
            json={
                "url": (
                    "7- #在抖音，记录美好生活#【爱雄泽】正在直播，来和我一起支持Ta吧。"
                    "复制下方链接，打开【抖音】，直接观看直播！ "
                    "https://v.douyin.com/eAb3MZKYD48/ 9@7.com :4pm"
                ),
                "auto_start": False,
            },
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["url"], "https://v.douyin.com/eAb3MZKYD48/")
        self.assertEqual(response.json()["segment_count"], 4)

    def test_update_restarts_only_for_effective_recording_config_changes(self) -> None:
        self.login()
        created = self.client.post(
            "/api/tasks",
            headers=self.csrf_headers,
            json={"url": "https://live.douyin.com/123456789", "segment_count": 0, "auto_start": True},
        ).json()
        task_id = created["id"]

        renamed = self.client.patch(
            f"/api/tasks/{task_id}",
            headers=self.csrf_headers,
            json={"label": "新备注"},
        )
        self.assertEqual(renamed.status_code, 200)
        self.assertEqual(renamed.json()["label"], "新备注")
        self.assertEqual(self.scheduler.restarted, [])

        unchanged = self.client.patch(
            f"/api/tasks/{task_id}",
            headers=self.csrf_headers,
            json={"quality": "OD"},
        )
        self.assertEqual(unchanged.status_code, 200)
        self.assertEqual(self.scheduler.restarted, [])

        changed = self.client.patch(
            f"/api/tasks/{task_id}",
            headers=self.csrf_headers,
            json={"quality": "HD"},
        )
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(changed.json()["quality"], "HD")
        self.assertEqual(self.scheduler.restarted, [task_id])

        limited = self.client.patch(
            f"/api/tasks/{task_id}",
            headers=self.csrf_headers,
            json={"segment_count": 4},
        )
        self.assertEqual(limited.status_code, 200)
        self.assertEqual(limited.json()["segment_count"], 4)
        self.assertEqual(self.scheduler.restarted, [task_id, task_id])

        invalid = self.client.patch(
            f"/api/tasks/{task_id}",
            headers=self.csrf_headers,
            json={"segment_seconds": 0},
        )
        self.assertEqual(invalid.status_code, 422)

    def test_segment_count_requires_nonzero_segment_duration(self) -> None:
        self.login()
        response = self.client.post(
            "/api/tasks",
            headers=self.csrf_headers,
            json={
                "url": "https://live.douyin.com/123456789",
                "segment_seconds": 0,
                "segment_count": 4,
                "auto_start": False,
            },
        )

        self.assertEqual(response.status_code, 422)
        self.assertIn("分段时长", response.text)

    def test_inspect_does_not_expose_signed_stream_urls(self) -> None:
        self.login()
        response = self.client.post(
            "/api/inspect",
            headers=self.csrf_headers,
            json={"url": "https://live.douyin.com/123456789", "quality": "OD"},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["has_flv"])
        self.assertTrue(payload["has_hls"])
        self.assertNotIn("flv_url", payload)
        self.assertNotIn("m3u8_url", payload)
        self.assertNotIn("secret", response.text)

    def test_login_rate_limit(self) -> None:
        for _ in range(self.settings.login_max_attempts):
            response = self.client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "wrong-password"},
            )
            self.assertEqual(response.status_code, 401)

        limited = self.client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "wrong-password"},
        )
        self.assertEqual(limited.status_code, 429)
        self.assertGreater(int(limited.headers["retry-after"]), 0)

    def test_rejects_unknown_fields_and_invalid_url(self) -> None:
        self.login()
        response = self.client.post(
            "/api/tasks",
            headers=self.csrf_headers,
            json={"url": "https://example.com/live", "unknown": True},
        )
        self.assertEqual(response.status_code, 422)
