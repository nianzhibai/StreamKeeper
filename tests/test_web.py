import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from fastapi.testclient import TestClient

from douyin_recorder.models import LiveInfo
from douyin_recorder.settings import CLOUD_ARCHIVE_ROOT, Settings
from douyin_recorder.web.app import create_app
from douyin_recorder.web.auth import SESSION_COOKIE_NAME
from douyin_recorder.web.cloud_login import CloudLoginPoll
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


class FakeCloudLoginFlow:
    qr_ttl_seconds = 60

    def __init__(self, provider: str) -> None:
        self.provider = provider

    async def start(self) -> str:
        return "data:image/png;base64,dGVzdC1xci1pbWFnZQ=="

    async def poll(self) -> CloudLoginPoll:
        if self.provider == "quark":
            return CloudLoginPoll("success", {"cookie": "qr-quark-secret-cookie"})
        return CloudLoginPoll(
            "success",
            {
                "access_token": "qr-wopan-access-token-123456",
                "refresh_token": "qr-wopan-refresh-token-123456",
            },
        )

    async def aclose(self) -> None:
        pass


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
            cloud_login_flow_factory=FakeCloudLoginFlow,
            cloud_login_poll_interval=0.01,
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
        static_asset = self.client.get("/static/login.js")
        self.assertEqual(static_asset.status_code, 200)
        self.assertEqual(static_asset.headers["cache-control"], "no-cache, must-revalidate")

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
        self.assertIn("max-age=604800", set_cookie)

        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Stream Keeper", page.text)
        self.assertEqual(page.headers["x-frame-options"], "DENY")
        self.assertIn("录制任务", self.client.get("/tasks").text)
        self.assertIn("网盘归档", self.client.get("/archive").text)
        self.assertIn("设置", self.client.get("/settings").text)
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

    def test_active_session_renews_once_inside_half_ttl_window(self) -> None:
        self.login()
        near_expiry = datetime.now(timezone.utc) + timedelta(hours=1)
        with closing(sqlite3.connect(self.settings.database_path)) as connection:
            connection.execute(
                "UPDATE web_sessions SET expires_at = ?",
                (near_expiry.isoformat(),),
            )
            connection.commit()

        renewed = self.client.get("/api/auth/session")
        self.assertEqual(renewed.status_code, 200)
        renewed_expiry = datetime.fromisoformat(renewed.json()["expires_at"])
        self.assertGreater(renewed_expiry, datetime.now(timezone.utc) + timedelta(days=6))
        self.assertIn("max-age=604800", renewed.headers["set-cookie"].lower())

        unchanged = self.client.get("/api/auth/session")
        self.assertEqual(unchanged.status_code, 200)
        self.assertNotIn("set-cookie", unchanged.headers)

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

    def test_failed_logins_permanently_blacklist_ip(self) -> None:
        self.login()
        for _ in range(self.settings.login_max_attempts - 1):
            response = self.client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "wrong-password"},
            )
            self.assertEqual(response.status_code, 401)

        blocked = self.client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "wrong-password"},
        )
        self.assertEqual(blocked.status_code, 403)
        self.assertIn("永久禁止", blocked.text)

        correct_password = self.client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "secret-password"},
        )
        self.assertEqual(correct_password.status_code, 403)

        blocked_clients = self.client.get("/api/auth/blocked-clients")
        self.assertEqual(blocked_clients.status_code, 200)
        self.assertEqual(blocked_clients.json(), ["testclient"])
        unblocked = self.client.delete(
            "/api/auth/blocked-clients/testclient",
            headers=self.csrf_headers,
        )
        self.assertEqual(unblocked.status_code, 204)

        accepted = self.client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "secret-password"},
        )
        self.assertEqual(accepted.status_code, 200)

    def test_rejects_unknown_fields_and_invalid_url(self) -> None:
        self.login()
        response = self.client.post(
            "/api/tasks",
            headers=self.csrf_headers,
            json={"url": "https://example.com/live", "unknown": True},
        )
        self.assertEqual(response.status_code, 422)

    def test_cloud_archive_can_be_configured_and_run_from_web_without_exposing_secrets(self) -> None:
        self.login()
        overview_page = self.client.get("/")
        archive_page = self.client.get("/archive")
        settings_page = self.client.get("/settings")
        self.assertNotIn('id="cloud-run-button"', overview_page.text)
        self.assertIn('id="cloud-run-button"', archive_page.text)
        self.assertIn('id="cloud-form"', settings_page.text)

        initial = self.client.get("/api/cloud/archive")
        self.assertEqual(initial.status_code, 200)
        self.assertFalse(initial.json()["enabled"])
        payload = {
            "quark": {
                "enabled": True,
                "cookie": "session=quark-secret-cookie",
                "clear_cookie": False,
                "root_id": "0",
                "upload_path": "/ignored-quark-path",
            },
            "wopan": {
                "enabled": True,
                "access_token": "wopan-access-token-123456",
                "refresh_token": "wopan-refresh-secret",
                "clear_tokens": False,
                "root_id": "0",
                "family_id": "",
                "upload_path": "/ignored-wopan-path",
            },
            "schedule": {"hour": 1, "min_age_minutes": 10, "timeout_seconds": 300},
        }
        rejected = self.client.put("/api/cloud/archive", json=payload)
        self.assertEqual(rejected.status_code, 403)

        saved = self.client.put("/api/cloud/archive", headers=self.csrf_headers, json=payload)
        self.assertEqual(saved.status_code, 200)
        saved_payload = saved.json()
        self.assertTrue(saved_payload["enabled"])
        self.assertTrue(saved_payload["quark"]["credential_configured"])
        self.assertTrue(saved_payload["wopan"]["access_token_configured"])
        self.assertTrue(saved_payload["wopan"]["refresh_token_configured"])
        self.assertEqual(saved_payload["quark"]["upload_path"], CLOUD_ARCHIVE_ROOT)
        self.assertEqual(saved_payload["wopan"]["upload_path"], CLOUD_ARCHIVE_ROOT)
        self.assertNotIn("quark-secret-cookie", saved.text)
        self.assertNotIn("wopan-access-token", saved.text)
        self.assertNotIn("wopan-refresh-secret", saved.text)

        payload["quark"]["cookie"] = None
        payload["wopan"]["access_token"] = None
        payload["wopan"]["refresh_token"] = None
        payload["schedule"]["hour"] = 2
        preserved = self.client.put("/api/cloud/archive", headers=self.csrf_headers, json=payload)
        self.assertEqual(preserved.status_code, 200)
        self.assertTrue(preserved.json()["quark"]["credential_configured"])
        self.assertTrue(preserved.json()["wopan"]["refresh_token_configured"])
        self.assertEqual(preserved.json()["schedule"]["hour"], 2)

        started = self.client.post("/api/cloud/archive/run", headers=self.csrf_headers)
        self.assertEqual(started.status_code, 202)
        last_status = None
        for _ in range(50):
            current = self.client.get("/api/cloud/archive").json()
            if not current["running"] and current["last_run"]:
                last_status = current["last_run"]
                break
            time.sleep(0.01)
        self.assertIsNotNone(last_status)
        self.assertEqual(last_status["status"], "success")
        self.assertEqual(last_status["summary"]["scanned_files"], 0)

        payload["quark"]["clear_cookie"] = True
        invalid_clear = self.client.put("/api/cloud/archive", headers=self.csrf_headers, json=payload)
        self.assertEqual(invalid_clear.status_code, 422)

        payload["quark"]["enabled"] = False
        payload["wopan"]["enabled"] = False
        payload["wopan"]["clear_tokens"] = True
        cleared = self.client.put("/api/cloud/archive", headers=self.csrf_headers, json=payload)
        self.assertEqual(cleared.status_code, 200)
        self.assertFalse(cleared.json()["enabled"])
        self.assertFalse(cleared.json()["quark"]["credential_configured"])
        self.assertFalse(cleared.json()["wopan"]["access_token_configured"])
        disabled_run = self.client.post("/api/cloud/archive/run", headers=self.csrf_headers)
        self.assertEqual(disabled_run.status_code, 409)

    def test_cloud_qr_login_saves_credentials_without_returning_secrets(self) -> None:
        self.login()
        settings_page = self.client.get("/settings")
        self.assertIn('data-cloud-login="quark"', settings_page.text)
        self.assertIn('data-cloud-login="wopan"', settings_page.text)
        self.assertIn('id="cloud-login-dialog"', settings_page.text)

        rejected = self.client.post("/api/cloud/login/quark")
        self.assertEqual(rejected.status_code, 403)

        for provider in ("quark", "wopan"):
            created = self.client.post(
                f"/api/cloud/login/{provider}",
                headers=self.csrf_headers,
            )
            self.assertEqual(created.status_code, 201)
            self.assertTrue(created.json()["qr_image"].startswith("data:image/png;base64,"))
            self.assertNotIn("secret", created.text)
            session_id = created.json()["session_id"]
            completed = None
            for _ in range(100):
                response = self.client.get(f"/api/cloud/login/{provider}/{session_id}")
                self.assertEqual(response.status_code, 200)
                if response.json()["state"] == "success":
                    completed = response
                    break
                time.sleep(0.01)
            self.assertIsNotNone(completed)
            self.assertIsNone(completed.json()["qr_image"])
            self.assertNotIn("secret", completed.text)
            deleted = self.client.delete(
                f"/api/cloud/login/{provider}/{session_id}",
                headers=self.csrf_headers,
            )
            self.assertEqual(deleted.status_code, 204)

        archive = self.client.get("/api/cloud/archive").json()
        self.assertTrue(archive["quark"]["credential_configured"])
        self.assertTrue(archive["wopan"]["access_token_configured"])
        self.assertTrue(archive["wopan"]["refresh_token_configured"])
