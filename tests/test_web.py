import asyncio
import hashlib
import json
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from stream_keeper.errors import InsufficientDiskSpaceError
from stream_keeper.models import LiveInfo
from stream_keeper.settings import CLOUD_ARCHIVE_ROOT, WEB_SETUP_PASSWORD, Settings
from stream_keeper.web.app import create_app
from stream_keeper.web.auth import SESSION_COOKIE_NAME
from stream_keeper.web.cloud_login import CloudLoginPoll
from stream_keeper.web.recordings import RecordingPreviewCache, build_remux_command
from stream_keeper.web.schemas import TaskStatus
from stream_keeper.web.store import TaskStore


class FakeScheduler:
    def __init__(self, store: TaskStore) -> None:
        self.store = store
        self.started: list[str] = []
        self.stopped: list[str] = []
        self.restarted: list[str] = []
        self.initial_infos: list[LiveInfo | None] = []
        self.active_task_count = 0
        self.recording_task_count = 0
        self.max_concurrent_recordings = 3
        self.capacity_updates: list[int] = []

    def set_max_concurrent_recordings(self, limit: int) -> None:
        self.max_concurrent_recordings = limit
        self.capacity_updates.append(limit)

    async def startup(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    async def start(self, task_id: str, *, initial_info: LiveInfo | None = None):
        self.started.append(task_id)
        self.initial_infos.append(initial_info)
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

    def enrich_record(self, record):
        return record

    def enrich_records(self, records):
        return list(records)


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
        if self.provider in {"quark", "baidu"}:
            return CloudLoginPoll("success", {"cookie": f"qr-{self.provider}-secret-cookie"})
        if self.provider == "pan115":
            return CloudLoginPoll("success", {"cookie": "UID=qr-uid; CID=qr-cid; SEID=qr-seid"})
        if self.provider == "guangya":
            return CloudLoginPoll(
                "success",
                {
                    "access_token": "qr-guangya-access-token-123456",
                    "refresh_token": "qr-guangya-refresh-token-123456",
                    "client_id": "aMe-8VSlkrbQXpUR",
                },
            )
        return CloudLoginPoll(
            "success",
            {
                "access_token": "qr-wopan-access-token-123456",
                "refresh_token": "qr-wopan-refresh-token-123456",
            },
        )

    async def aclose(self) -> None:
        pass


class FakeBaiduOpenListAuth:
    def __init__(self) -> None:
        self.codes: list[str] = []

    async def authorization_url(self) -> str:
        return "https://openapi.baidu.com/oauth/2.0/authorize?redirect_uri=oob"

    async def exchange(self, authorization_code: str) -> dict[str, str]:
        self.codes.append(authorization_code)
        return {
            "access_token": "openlist-baidu-access-token",
            "refresh_token": "openlist-baidu-refresh-token",
            "client_id": "openlist-baidu-client-id",
            "client_secret": "openlist-baidu-client-secret",
        }

    async def aclose(self) -> None:
        pass


class WebSetupTests(TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.settings = Settings(
            data_dir=root,
            recordings_dir=root / "recordings",
            database_path=root / "tasks.db",
            web_username="admin",
            web_password=WEB_SETUP_PASSWORD,
            validate_binaries=False,
        )
        self.store = TaskStore(self.settings.database_path)
        # Simulate an upgrade from the release that persisted the documented
        # placeholder as a real account. Startup must turn it back into setup.
        asyncio.run(self.store.initialize())
        asyncio.run(self.store.sync_web_credentials("admin", WEB_SETUP_PASSWORD))
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

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        self.temp_dir.cleanup()

    def test_default_environment_credentials_trigger_one_time_web_setup(self) -> None:
        status_response = self.client.get("/api/auth/status")
        self.assertEqual(status_response.status_code, 200)
        self.assertEqual(
            status_response.json(),
            {
                "authentication_enabled": True,
                "setup_required": True,
            },
        )
        self.assertEqual(self.client.get("/", follow_redirects=False).status_code, 303)
        initial_login_page = self.client.get("/login")
        self.assertEqual(initial_login_page.status_code, 200)
        self.assertIn('id="setup-confirm-field"', initial_login_page.text)
        self.assertIn("/static/login.js?v=20260851", initial_login_page.text)

        login_before_setup = self.client.post(
            "/api/auth/login",
            json={"username": "admin", "password": WEB_SETUP_PASSWORD},
        )
        self.assertEqual(login_before_setup.status_code, 409)

        mismatch = self.client.post(
            "/api/auth/setup",
            json={
                "username": "chosen-admin",
                "password": "chosen-secure-password",
                "password_confirmation": "different-secure-password",
            },
        )
        self.assertEqual(mismatch.status_code, 422)
        placeholder = self.client.post(
            "/api/auth/setup",
            json={
                "username": "chosen-admin",
                "password": WEB_SETUP_PASSWORD,
                "password_confirmation": WEB_SETUP_PASSWORD,
            },
        )
        self.assertEqual(placeholder.status_code, 422)

        setup = self.client.post(
            "/api/auth/setup",
            json={
                "username": "  chosen-admin  ",
                "password": "chosen-secure-password",
                "password_confirmation": "chosen-secure-password",
            },
        )
        self.assertEqual(setup.status_code, 201)
        self.assertEqual(setup.json()["username"], "chosen-admin")
        self.assertTrue(self.client.cookies.get(SESSION_COOKIE_NAME))
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(
            self.client.get("/api/auth/status").json(),
            {
                "authentication_enabled": True,
                "setup_required": False,
            },
        )
        self.assertEqual(
            self.client.post(
                "/api/auth/setup",
                json={
                    "username": "other-admin",
                    "password": "other-secure-password",
                    "password_confirmation": "other-secure-password",
                },
            ).status_code,
            409,
        )
        self.assertNotIn(b"chosen-secure-password", self.store.database_path.read_bytes())

        csrf_headers = {"X-CSRF-Token": setup.json()["csrf_token"]}
        self.assertEqual(self.client.post("/api/auth/logout", headers=csrf_headers).status_code, 204)
        rejected_placeholder = self.client.post(
            "/api/auth/login",
            json={"username": "admin", "password": WEB_SETUP_PASSWORD},
        )
        self.assertEqual(rejected_placeholder.status_code, 401)
        accepted = self.client.post(
            "/api/auth/login",
            json={"username": "chosen-admin", "password": "chosen-secure-password"},
        )
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(self.client.get("/api/auth/blocked-clients").json(), [])

        login_page = self.client.get("/login", follow_redirects=False)
        self.assertEqual(login_page.status_code, 303)
        login_script = self.client.get("/static/login.js").text
        self.assertIn('fetch("/api/auth/status"', login_script)
        self.assertIn('setupRequired ? "/api/auth/setup" : "/api/auth/login"', login_script)
        self.assertIn("password_confirmation", login_script)

        restarted_store = TaskStore(self.settings.database_path)
        restarted_scheduler = FakeScheduler(restarted_store)
        restarted_app = create_app(
            self.settings,
            store=restarted_store,
            scheduler=restarted_scheduler,
            inspect_client_factory=FakeInspectClient,
            cloud_login_flow_factory=FakeCloudLoginFlow,
            cloud_login_poll_interval=0.01,
        )
        with TestClient(restarted_app) as restarted_client:
            self.assertFalse(restarted_client.get("/api/auth/status").json()["setup_required"])
            restarted_login = restarted_client.post(
                "/api/auth/login",
                json={"username": "chosen-admin", "password": "chosen-secure-password"},
            )
            self.assertEqual(restarted_login.status_code, 200)


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
        self.baidu_openlist_auth = FakeBaiduOpenListAuth()
        app = create_app(
            self.settings,
            store=self.store,
            scheduler=self.scheduler,
            inspect_client_factory=FakeInspectClient,
            cloud_login_flow_factory=FakeCloudLoginFlow,
            cloud_login_poll_interval=0.01,
            baidu_openlist_auth=self.baidu_openlist_auth,
        )
        self.app = app
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
        self.assertIn('id="login-form"', login_page.text)
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
        self.assertIn("fullscreen=(self)", page.headers["permissions-policy"])
        self.assertIn("media-src 'self' blob:", page.headers["content-security-policy"])
        self.assertIn("worker-src 'self' blob:", page.headers["content-security-policy"])
        self.assertIn("录制任务", self.client.get("/tasks").text)
        self.assertIn("本地录像", self.client.get("/recordings").text)
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

    def test_account_credentials_can_be_updated_and_all_sessions_are_revoked(self) -> None:
        self.login()
        current_token = self.client.cookies.get(SESSION_COOKIE_NAME)
        second_token, _ = asyncio.run(self.store.create_session("admin", 3600))

        missing_csrf = self.client.put(
            "/api/settings/account",
            json={
                "username": "operator",
                "current_password": "secret-password",
                "new_password": None,
                "new_password_confirmation": None,
            },
        )
        self.assertEqual(missing_csrf.status_code, 403)

        wrong_password = self.client.put(
            "/api/settings/account",
            headers=self.csrf_headers,
            json={
                "username": "operator",
                "current_password": "incorrect-password",
                "new_password": None,
                "new_password_confirmation": None,
            },
        )
        self.assertEqual(wrong_password.status_code, 400)
        self.assertIn("当前密码不正确", wrong_password.text)
        self.assertIsNotNone(asyncio.run(self.store.get_session(current_token)))
        self.assertIsNotNone(asyncio.run(self.store.get_session(second_token)))

        mismatch = self.client.put(
            "/api/settings/account",
            headers=self.csrf_headers,
            json={
                "username": "operator",
                "current_password": "secret-password",
                "new_password": "replacement-password",
                "new_password_confirmation": "different-password",
            },
        )
        self.assertEqual(mismatch.status_code, 422)

        unchanged = self.client.put(
            "/api/settings/account",
            headers=self.csrf_headers,
            json={
                "username": "admin",
                "current_password": "secret-password",
                "new_password": None,
                "new_password_confirmation": None,
            },
        )
        self.assertEqual(unchanged.status_code, 400)
        self.assertIn("均未更改", unchanged.text)
        self.assertIsNotNone(asyncio.run(self.store.get_session(current_token)))

        updated = self.client.put(
            "/api/settings/account",
            headers=self.csrf_headers,
            json={
                "username": "  operator  ",
                "current_password": "secret-password",
                "new_password": "replacement-password",
                "new_password_confirmation": "replacement-password",
            },
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json(), {"username": "operator", "sessions_revoked": True})
        self.assertIsNone(self.client.cookies.get(SESSION_COOKIE_NAME))
        self.assertIsNone(asyncio.run(self.store.get_session(current_token)))
        self.assertIsNone(asyncio.run(self.store.get_session(second_token)))
        self.assertEqual(self.client.get("/api/auth/session").status_code, 401)

        old_login = self.client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "secret-password"},
        )
        self.assertEqual(old_login.status_code, 401)
        new_login = self.client.post(
            "/api/auth/login",
            json={"username": "operator", "password": "replacement-password"},
        )
        self.assertEqual(new_login.status_code, 200)
        events = self.client.get("/api/events").json()["events"]
        self.assertTrue(any(event["message"] == "管理员登录账号已更新" for event in events))

    def test_phone_navigation_groups_secondary_pages_under_more_sheet(self) -> None:
        self.login()
        shell = self.client.get("/static/shell.js").text
        style = self.client.get("/static/style.css").text
        sprite = self.client.get("/static/sprite.js").text

        self.assertEqual(shell.count('mobile: "primary"'), 3)
        self.assertEqual(shell.count('mobile: "more"'), 3)
        self.assertIn('data-mobile-more-toggle', shell)
        self.assertIn('id="mobile-more-dialog"', shell)
        self.assertIn('dialog.showModal()', shell)
        self.assertIn('dialog.addEventListener("touchend"', shell)
        self.assertIn('window.matchMedia("(max-width: 640px)")', shell)
        self.assertIn('mobileLabel: "任务"', shell)
        self.assertIn('mobileLabel: "录像"', shell)
        self.assertIn('aria-current="page"', shell)

        page_header_style = style.split('.page-head {', 1)[1].split('}', 1)[0]
        page_title_style = style.split('.page-heading h1 {', 1)[1].split('}', 1)[0]
        self.assertIn('align-items: center;', page_header_style)
        self.assertIn('min-height: 38px;', page_title_style)
        self.assertIn('align-items: center;', page_title_style)
        self.assertIn('@media (max-width: 640px)', style)
        self.assertIn('grid-template-columns: repeat(4, minmax(0, 1fr))', style)
        self.assertIn('.nav-link[data-nav="archive"]', style)
        self.assertIn('.mobile-more[open]', style)
        self.assertIn('env(safe-area-inset-bottom)', style)
        self.assertIn('more:', sprite)

        for path in ("/", "/tasks", "/recordings", "/archive", "/logs", "/settings"):
            with self.subTest(path=path):
                page = self.client.get(path)
                self.assertIn('/static/style.css?v=20260850', page.text)
                self.assertIn('/static/sprite.js?v=20260847', page.text)
                self.assertIn('/static/shell.js?v=20260844', page.text)
                self.assertNotIn('class="page-eyebrow"', page.text)
                self.assertNotIn('id="refresh-button"', page.text)
                self.assertRegex(
                    page.text,
                    r'<div class="page-heading">\s*<h1>[^<]+</h1>\s*</div>',
                )

    def test_page_scripts_do_not_bind_removed_refresh_controls(self) -> None:
        self.login()
        for script in ("dashboard.js", "tasks.js", "recordings.js", "archive.js", "logs.js"):
            with self.subTest(script=script):
                self.assertNotIn("#refresh-button", self.client.get(f"/static/{script}").text)
        self.assertIn('id="cloud-login-refresh"', self.client.get("/archive").text)

    def test_task_url_input_keeps_standard_field_size_on_phones(self) -> None:
        self.login()
        task_page = self.client.get("/tasks").text
        style = self.client.get("/static/style.css").text

        self.assertIn('<span class="input-row">', task_page)
        self.assertIn('grid-template-columns: minmax(0, 1fr) auto', style)
        self.assertIn('.input-row .input {\n  min-width: 0;', style)
        self.assertIn('.input-row { grid-template-columns: minmax(0, 1fr); }', style)
        self.assertIn('.input-row .btn { width: 100%; }', style)
        self.assertNotIn('.input-row .input {\n  flex: 1;', style)

    def test_recording_defaults_are_available_in_settings_and_new_task_dialog(self) -> None:
        self.login()
        settings_page = self.client.get("/settings").text
        task_page = self.client.get("/tasks").text
        settings_script = self.client.get("/static/settings.js").text
        tasks_script = self.client.get("/static/tasks.js").text

        self.assertIn("管理员账号", settings_page)
        self.assertIn('id="account-settings-form"', settings_page)
        self.assertIn('name="current_password"', settings_page)
        self.assertIn('name="new_password_confirmation"', settings_page)
        self.assertIn("所有登录会话都会立即失效", settings_page)
        self.assertIn("录制并发", settings_page)
        self.assertIn('name="max_concurrent_recordings"', settings_page)
        self.assertIn("调低不会中断正在录制的直播", settings_page)
        self.assertIn("录制默认值", settings_page)
        self.assertIn('name="recording_output_format"', settings_page)
        self.assertIn('name="recording_segment_seconds"', settings_page)
        self.assertIn('name="recording_segment_count"', settings_page)
        self.assertIn("/static/settings.js?v=20260831", settings_page)
        self.assertIn("/api/settings/account", settings_script)
        self.assertIn('window.location.replace("/login")', settings_script)
        self.assertIn("new_password_confirmation", settings_script)
        self.assertIn("/api/settings/recording-defaults", settings_script)
        self.assertIn("/api/settings/recording-runtime", settings_script)
        self.assertIn("form.elements.max_concurrent_recordings.value", settings_script)
        self.assertIn("form.elements.recording_output_format.value", settings_script)

        self.assertIn("仅覆盖当前任务", task_page)
        self.assertIn("/static/tasks.js?v=20260835", task_page)
        self.assertIn("form.output_format.value = state.recordingDefaults.output_format", tasks_script)
        self.assertIn("form.segment_seconds.value = String(state.recordingDefaults.segment_seconds)", tasks_script)
        self.assertIn("form.segment_count.value = String(state.recordingDefaults.segment_count)", tasks_script)

    def test_recording_library_browses_and_streams_only_safe_video_files(self) -> None:
        self.login()
        recording_page = self.client.get("/recordings")
        self.assertIn('id="recording-player"', recording_page.text)
        self.assertIn('id="recording-download"', recording_page.text)
        self.assertIn("/static/artplayer.css?v=5.4.1", recording_page.text)
        self.assertIn("/static/artplayer.js?v=5.4.1", recording_page.text)
        self.assertNotIn('id="recording-play-toggle"', recording_page.text)
        self.assertNotIn('id="recording-player-seek"', recording_page.text)
        player_script = self.client.get("/static/recordings.js").text
        self.assertIn("playback_mode", player_script)
        self.assertIn('preview ? "preview" : "file"', player_script)
        self.assertIn('"remux"', player_script)
        self.assertIn("new Artplayer", player_script)
        self.assertIn("playbackRate: true", player_script)
        self.assertIn("fullscreen: true", player_script)
        self.assertIn("instance.destroy()", player_script)
        self.assertNotIn("requestFullscreen", player_script)
        self.assertNotIn("mpegts", player_script)
        self.assertNotIn("is-portrait", player_script)
        artplayer_script = self.client.get("/static/artplayer.js")
        artplayer_style = self.client.get("/static/artplayer.css")
        self.assertEqual(artplayer_script.status_code, 200)
        self.assertEqual(artplayer_style.status_code, 200)
        self.assertIn("artplayer.js v5.4.1", artplayer_script.text)
        self.assertNotIn('M("artplayer-style"', artplayer_script.text)
        self.assertIn(".art-video-player", artplayer_style.text)

        recording_dir = self.settings.recordings_dir / "测试 主播" / "2026-07-12"
        recording_dir.mkdir(parents=True)
        video = recording_dir / "测试 主播_2026-07-12_12-00-00.mp4"
        video.write_bytes(b"0123456789")
        (recording_dir / "空录像.ts").touch()
        (recording_dir / "内部信息.txt").write_text("not public", encoding="utf-8")
        outside = Path(self.temp_dir.name) / "outside.mp4"
        outside.write_bytes(b"secret")
        try:
            (self.settings.recordings_dir / "越界链接.mp4").symlink_to(outside)
            has_symlink = True
        except OSError:
            has_symlink = False

        root = self.client.get("/api/recordings")
        self.assertEqual(root.status_code, 200)
        self.assertEqual(root.json()["path"], "")
        self.assertEqual([entry["name"] for entry in root.json()["entries"]], ["测试 主播"])

        anchor = self.client.get("/api/recordings", params={"path": "测试 主播"})
        self.assertEqual(anchor.status_code, 200)
        self.assertEqual(anchor.json()["entries"][0]["kind"], "directory")

        listing = self.client.get("/api/recordings", params={"path": "测试 主播/2026-07-12"})
        self.assertEqual(listing.status_code, 200)
        entries = listing.json()["entries"]
        self.assertEqual(
            [entry["name"] for entry in entries],
            ["测试 主播_2026-07-12_12-00-00.mp4", "空录像.ts"],
        )
        self.assertEqual(entries[0]["size"], 10)
        self.assertEqual(entries[0]["extension"], "mp4")
        self.assertEqual(entries[0]["playback_mode"], "direct")
        self.assertTrue(entries[0]["playable"])
        self.assertEqual(entries[1]["playback_mode"], "remux")
        self.assertFalse(entries[1]["playable"])

        remux_output = Path(self.temp_dir.name) / "preview.mp4"
        remux_command = build_remux_command("/usr/bin/ffmpeg", video, remux_output)
        self.assertIn("copy", remux_command)
        self.assertNotIn("libx264", remux_command)
        self.assertNotIn("-c:a", remux_command)
        self.assertIn("aac_adtstoasc", remux_command)
        self.assertIn("+faststart", remux_command)
        self.assertNotIn("empty_moov", remux_command)
        self.assertEqual(remux_command[-1], str(remux_output))

        file_path = "测试 主播/2026-07-12/测试 主播_2026-07-12_12-00-00.mp4"
        ranged = self.client.get(f"/api/recordings/file/{file_path}", headers={"Range": "bytes=2-5"})
        self.assertEqual(ranged.status_code, 206)
        self.assertEqual(ranged.content, b"2345")
        self.assertEqual(ranged.headers["content-range"], "bytes 2-5/10")
        self.assertEqual(ranged.headers["accept-ranges"], "bytes")
        self.assertTrue(ranged.headers["content-type"].startswith("video/mp4"))

        download = self.client.get(f"/api/recordings/file/{file_path}", params={"download": "true"})
        self.assertEqual(download.status_code, 200)
        self.assertIn("attachment", download.headers["content-disposition"])

        cached_preview = Path(self.temp_dir.name) / "cached-preview.mp4"
        cached_preview.write_bytes(b"browser-compatible-preview")
        with patch.object(
            self.app.state.recording_preview_cache,
            "get",
            new=AsyncMock(return_value=cached_preview),
        ):
            preview = self.client.get(f"/api/recordings/preview/{file_path}")
            preview_range = self.client.get(
                f"/api/recordings/preview/{file_path}",
                headers={"Range": "bytes=8-17"},
            )
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.content, b"browser-compatible-preview")
        self.assertTrue(preview.headers["content-type"].startswith("video/mp4"))
        self.assertEqual(preview.headers["accept-ranges"], "bytes")
        self.assertEqual(preview.headers["x-recording-preview"], "remux-cache")
        self.assertEqual(preview_range.status_code, 206)
        self.assertEqual(preview_range.content, b"compatible")
        self.assertEqual(preview_range.headers["content-range"], "bytes 8-17/26")

        self.assertEqual(self.client.get("/api/recordings", params={"path": "../"}).status_code, 400)
        if has_symlink:
            self.assertEqual(self.client.get("/api/recordings/file/越界链接.mp4").status_code, 400)
        self.assertEqual(
            self.client.get("/api/recordings/file/测试 主播/2026-07-12/内部信息.txt").status_code,
            404,
        )
        self.assertEqual(
            self.client.delete(
                "/api/recordings/file/测试 主播/2026-07-12/内部信息.txt",
                headers=self.csrf_headers,
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.delete("/api/recordings/file/../outside.mp4", headers=self.csrf_headers).status_code,
            404,
        )

        with patch.object(
            self.app.state.recording_preview_cache,
            "discard",
            new=AsyncMock(return_value=1),
        ) as discard:
            deleted = self.client.delete(f"/api/recordings/file/{file_path}", headers=self.csrf_headers)
        self.assertEqual(deleted.status_code, 204)
        self.assertFalse(video.exists())
        discard.assert_awaited_once_with(file_path)
        self.assertEqual(
            self.client.delete(f"/api/recordings/file/{file_path}", headers=self.csrf_headers).status_code,
            404,
        )

        recordings_script = self.client.get("/static/recordings.js").text
        self.assertIn('data-action="delete"', recordings_script)
        self.assertIn("对应的转码文件", recordings_script)
        self.assertIn('method: "DELETE"', recordings_script)

    def test_preview_cache_entries_can_be_found_by_recording_path(self) -> None:
        directory = Path(self.temp_dir.name) / "preview-cache"
        directory.mkdir()
        cache = RecordingPreviewCache(directory, "ffmpeg")

        first = cache._cache_key("主播/2026-07-12/live.ts", (100, 111))
        # A re-recorded file reuses the path but must not reuse the remux.
        second = cache._cache_key("主播/2026-07-12/live.ts", (200, 222))
        other = cache._cache_key("主播/2026-07-12/另一场.ts", (100, 111))
        self.assertNotEqual(first, second)
        self.assertEqual(first.split("-")[0], second.split("-")[0])
        self.assertNotEqual(first.split("-")[0], other.split("-")[0])

        for key in (first, second, other):
            (directory / f"{key}.mp4").write_bytes(b"remux")
        keep_temporary = directory / f".{first}.abc123.mp4"
        keep_temporary.write_bytes(b"in-flight")

        self.assertEqual(asyncio.run(cache.discard("主播/2026-07-12/live.ts")), 2)
        self.assertFalse((directory / f"{first}.mp4").exists())
        self.assertFalse((directory / f"{second}.mp4").exists())
        self.assertTrue((directory / f"{other}.mp4").exists())
        # An in-flight remux owns its temporary file and cleans it up itself.
        self.assertTrue(keep_temporary.exists())

        self.assertEqual(asyncio.run(cache.discard("主播/2026-07-12/live.ts")), 0)
        self.assertEqual(asyncio.run(RecordingPreviewCache(directory / "missing", "ffmpeg").discard("a.ts")), 0)

    def test_preview_cache_preserves_one_gibibyte_of_free_space(self) -> None:
        directory = Path(self.temp_dir.name) / "preview-cache"
        source = Path(self.temp_dir.name) / "recording.ts"
        source.write_bytes(b"video")
        cache = RecordingPreviewCache(directory, "ffmpeg")

        with (
            patch("stream_keeper.web.recordings.resolve_ffmpeg", return_value="/fake/ffmpeg"),
            patch(
                "stream_keeper.web.recordings.ensure_disk_reserve",
                side_effect=InsufficientDiskSpaceError("磁盘可用空间不足，必须至少保留 1 GB"),
            ),
        ):
            with self.assertRaisesRegex(HTTPException, "至少保留 1 GB") as raised:
                asyncio.run(cache.get(source, "recording.ts"))

        self.assertEqual(raised.exception.status_code, 507)
        self.assertEqual(tuple(directory.iterdir()), ())

    def test_recordings_delegate_cloud_uploads_to_the_archive_page(self) -> None:
        self.login()
        recordings = self.client.get("/recordings")
        self.assertNotIn('id="upload-all-button"', recordings.text)
        self.assertNotIn('id="upload-queue"', recordings.text)

        recordings_script = self.client.get("/static/recordings.js").text
        self.assertNotIn("/api/recordings/uploads", recordings_script)
        self.assertNotIn("EventSource", recordings_script)

        archive = self.client.get("/archive")
        self.assertIn('id="cloud-run-button"', archive.text)
        archive_script = self.client.get("/static/archive.js").text
        self.assertIn("/api/cloud/archive/run", archive_script)

        self.assertFalse(any("/api/recordings/uploads" in route.path for route in self.app.routes))
        self.assertEqual(self.client.get("/api/recordings/uploads").status_code, 404)

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
        self.assertFalse(task["monitor"])
        self.assertEqual(self.scheduler.started, [task["id"]])

        listed = self.client.get("/api/tasks").json()
        self.assertEqual([item["id"] for item in listed], [task["id"]])

        stopped = self.client.post(f"/api/tasks/{task['id']}/stop", headers=self.csrf_headers)
        self.assertEqual(stopped.status_code, 200)
        self.assertFalse(stopped.json()["enabled"])

        deleted = self.client.delete(f"/api/tasks/{task['id']}", headers=self.csrf_headers)
        self.assertEqual(deleted.status_code, 204)
        self.assertEqual(self.client.get("/api/tasks").json(), [])

    def test_recording_defaults_apply_to_new_tasks_and_explicit_task_values_win(self) -> None:
        self.login()
        initial = self.client.get("/api/settings/recording-defaults")
        self.assertEqual(
            initial.json(),
            {"output_format": "ts", "segment_seconds": 1800, "segment_count": 0},
        )

        invalid = self.client.put(
            "/api/settings/recording-defaults",
            headers=self.csrf_headers,
            json={"output_format": "mp4", "segment_seconds": 0, "segment_count": 4},
        )
        self.assertEqual(invalid.status_code, 422)

        saved = self.client.put(
            "/api/settings/recording-defaults",
            headers=self.csrf_headers,
            json={"output_format": "mp4", "segment_seconds": 600, "segment_count": 4},
        )
        self.assertEqual(saved.status_code, 200)

        inherited = self.client.post(
            "/api/tasks",
            headers=self.csrf_headers,
            json={"url": "https://live.douyin.com/123456789", "auto_start": False},
        )
        self.assertEqual(inherited.status_code, 201)
        self.assertEqual(inherited.json()["output_format"], "mp4")
        self.assertEqual(inherited.json()["segment_seconds"], 600)
        self.assertEqual(inherited.json()["segment_count"], 4)
        self.assertFalse(inherited.json()["monitor"])

        explicit = self.client.post(
            "/api/tasks",
            headers=self.csrf_headers,
            json={
                "url": "https://live.bilibili.com/123456",
                "output_format": "mkv",
                "segment_seconds": 0,
                "segment_count": 0,
                "auto_start": False,
            },
        )
        self.assertEqual(explicit.status_code, 201)
        self.assertEqual(explicit.json()["output_format"], "mkv")
        self.assertEqual(explicit.json()["segment_seconds"], 0)
        self.assertEqual(explicit.json()["segment_count"], 0)
        self.assertTrue(explicit.json()["monitor"])

        self.client.put(
            "/api/settings/recording-defaults",
            headers=self.csrf_headers,
            json={"output_format": "flv", "segment_seconds": 0, "segment_count": 0},
        )
        unchanged = self.client.get(f"/api/tasks/{inherited.json()['id']}").json()
        self.assertEqual(unchanged["output_format"], "mp4")
        self.assertEqual(unchanged["segment_seconds"], 600)
        self.assertEqual(unchanged["segment_count"], 4)

    def test_recording_concurrency_can_be_updated_at_runtime(self) -> None:
        self.login()
        initial = self.client.get("/api/settings/recording-runtime")
        self.assertEqual(initial.json(), {"max_concurrent_recordings": 3})
        self.assertEqual(self.scheduler.capacity_updates, [3])

        rejected = self.client.put(
            "/api/settings/recording-runtime",
            json={"max_concurrent_recordings": 2},
        )
        self.assertEqual(rejected.status_code, 403)
        for invalid in (0, 101):
            with self.subTest(invalid=invalid):
                response = self.client.put(
                    "/api/settings/recording-runtime",
                    headers=self.csrf_headers,
                    json={"max_concurrent_recordings": invalid},
                )
                self.assertEqual(response.status_code, 422)

        saved = self.client.put(
            "/api/settings/recording-runtime",
            headers=self.csrf_headers,
            json={"max_concurrent_recordings": 2},
        )
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.json(), {"max_concurrent_recordings": 2})
        self.assertEqual(self.scheduler.max_concurrent_recordings, 2)
        self.assertEqual(self.scheduler.capacity_updates, [3, 2])
        self.assertEqual(self.client.get("/api/system").json()["max_concurrent_recordings"], 2)
        self.assertEqual(
            asyncio.run(self.store.get_recording_runtime_settings()).max_concurrent_recordings,
            2,
        )

    def test_system_reports_the_size_of_recordings_still_waiting_to_be_archived(self) -> None:
        self.login()
        self.assertEqual(self.client.get("/api/system").json()["pending_upload_bytes"], 0)

        recording_dir = self.settings.recordings_dir / "主播" / "2026-08-22"
        recording_dir.mkdir(parents=True)
        (recording_dir / "a.mp4").write_bytes(b"0" * 1024)
        (recording_dir / "b.ts").write_bytes(b"0" * 512)
        # Neither a non-video sidecar nor an empty file adds to the pending total.
        (recording_dir / "notes.txt").write_text("ignored", encoding="utf-8")
        (recording_dir / "empty.flv").touch()

        self.assertEqual(self.client.get("/api/system").json()["pending_upload_bytes"], 1536)

        # An archived recording is deleted locally, so the pending total drops with it.
        (recording_dir / "a.mp4").unlink()
        self.assertEqual(self.client.get("/api/system").json()["pending_upload_bytes"], 512)

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
        self.assertEqual(response.json()["segment_count"], 0)
        self.assertTrue(response.json()["monitor"])

    def test_create_task_accepts_bilibili_and_kuaishou_rooms(self) -> None:
        self.login()
        for url in (
            "https://live.bilibili.com/123456?from=share",
            "https://live.kuaishou.com/u/example?share=1",
        ):
            with self.subTest(url=url):
                response = self.client.post(
                    "/api/tasks",
                    headers=self.csrf_headers,
                    json={"url": url, "auto_start": False},
                )
                self.assertEqual(response.status_code, 201)
                self.assertNotIn("?", response.json()["url"])

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
        self.assertFalse(limited.json()["monitor"])
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
        self.assertEqual(payload["platform"], "抖音")
        self.assertTrue(payload["has_flv"])
        self.assertTrue(payload["has_hls"])
        self.assertGreaterEqual(len(payload["inspection_token"]), 16)
        self.assertNotIn("flv_url", payload)
        self.assertNotIn("m3u8_url", payload)
        self.assertNotIn("secret", response.text)

    def test_fresh_inspection_is_handed_to_immediate_task_start_once(self) -> None:
        self.login()
        url = "https://live.douyin.com/123456789"
        inspection = self.client.post(
            "/api/inspect",
            headers=self.csrf_headers,
            json={"url": url, "quality": "OD"},
        )
        token = inspection.json()["inspection_token"]

        created = self.client.post(
            "/api/tasks",
            headers=self.csrf_headers,
            json={"url": url, "quality": "OD", "auto_start": True, "inspection_token": token},
        )

        self.assertEqual(created.status_code, 201)
        self.assertEqual(self.scheduler.started, [created.json()["id"]])
        initial_info = self.scheduler.initial_infos[0]
        self.assertIsNotNone(initial_info)
        self.assertEqual(initial_info.anchor_name, "测试主播")
        self.assertIn("token=secret", initial_info.flv_url or "")
        self.assertNotIn("secret", created.text)

        reused = self.client.post(
            "/api/tasks",
            headers=self.csrf_headers,
            json={"url": url, "quality": "OD", "auto_start": True, "inspection_token": token},
        )
        self.assertEqual(reused.status_code, 201)
        self.assertIsNone(self.scheduler.initial_infos[-1])

        tasks_script = self.client.get("/static/tasks.js").text
        self.assertIn("payload.inspection_token = inspection.token", tasks_script)
        self.assertIn('elements.form.elements.url.addEventListener("input", invalidateInspection)', tasks_script)
        self.assertIn('elements.form.elements.quality.addEventListener("change", invalidateInspection)', tasks_script)
        self.assertIn("/static/tasks.js?v=20260835", self.client.get("/tasks").text)

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

    def test_activity_log_page_lists_curated_events_with_filters(self) -> None:
        self.login()
        page = self.client.get("/logs")
        self.assertEqual(page.status_code, 200)
        self.assertIn("运行日志", page.text)
        self.assertIn('id="log-list"', page.text)
        self.assertIn("运行日志", self.client.get("/static/shell.js").text)

        self.client.post("/api/auth/login", json={"username": "admin", "password": "wrong-password"})

        payload = self.client.get("/api/events").json()
        events = payload["events"]
        self.assertEqual(events[0]["message"], "登录失败：用户名或密码错误")
        self.assertEqual(events[0]["level"], "warning")
        self.assertEqual(events[0]["detail"], "来源 IP testclient")
        self.assertEqual(events[0]["category"], "auth")
        self.assertEqual([event["message"] for event in events[1:]][:1], ["admin 登录成功"])
        self.assertTrue(events[-1]["message"].startswith("服务已启动"))
        self.assertEqual(events[-1]["category"], "system")
        self.assertEqual(payload["summary"]["warnings"], 1)
        self.assertEqual(payload["summary"]["errors"], 0)
        self.assertEqual(payload["summary"]["total"], len(events))
        self.assertEqual(payload["summary"]["latest_at"], events[0]["created_at"])

        auth_only = self.client.get("/api/events?category=auth").json()["events"]
        self.assertEqual({event["category"] for event in auth_only}, {"auth"})
        alerts = self.client.get("/api/events?alerts_only=true").json()["events"]
        self.assertEqual([event["message"] for event in alerts], ["登录失败：用户名或密码错误"])
        self.assertEqual(len(self.client.get("/api/events?limit=1").json()["events"]), 1)
        self.assertEqual(self.client.get("/api/events?category=nope").status_code, 422)
        self.assertEqual(self.client.get("/api/events?limit=501").status_code, 422)

    def test_activity_log_keeps_only_the_newest_entries(self) -> None:
        store = TaskStore(self.settings.database_path)
        for index in range(5):
            asyncio.run(store.append_event("system", "info", f"事件 {index}", retention=3))

        events = asyncio.run(store.list_events(limit=10))
        self.assertEqual([event.message for event in events], ["事件 4", "事件 3", "事件 2"])

    def _seed_events(self) -> TaskStore:
        store = TaskStore(self.settings.database_path)
        rows = [
            ("task", "success", "开始录制「钢琴电台」", "画质 原画", "task-a"),
            ("task", "error", "「陪玩夜场」拉流失败", "连接被远端重置", "task-b"),
            ("upload", "warning", "跳过 3 个文件", "不足 10 分钟", None),
            ("upload", "success", "归档完成", "上传 12 份副本", None),
            ("system", "info", "含通配符的 100%_文本", None, None),
        ]
        for category, level, message, detail, task_id in rows:
            asyncio.run(store.append_event(category, level, message, detail, retention=5000, task_id=task_id))
        return store

    def test_activity_log_supports_multi_select_search_and_task_scope(self) -> None:
        self.login()
        self._seed_events()

        multi_level = self.client.get("/api/events?levels=warning&levels=error").json()["events"]
        self.assertEqual({event["level"] for event in multi_level}, {"warning", "error"})

        multi_category = self.client.get("/api/events?categories=task&categories=upload").json()
        self.assertEqual({event["category"] for event in multi_category["events"]}, {"task", "upload"})

        searched = self.client.get("/api/events?search=归档").json()["events"]
        self.assertEqual([event["message"] for event in searched], ["归档完成"])

        scoped = self.client.get("/api/events?task_id=task-a").json()["events"]
        self.assertEqual([event["message"] for event in scoped], ["开始录制「钢琴电台」"])
        self.assertEqual(scoped[0]["task_id"], "task-a")

    def test_activity_log_escapes_like_wildcards_in_search(self) -> None:
        self.login()
        self._seed_events()

        # A bare % must search for a literal percent sign, not match every row.
        literal = self.client.get("/api/events?search=%25").json()["events"]
        self.assertEqual([event["message"] for event in literal], ["含通配符的 100%_文本"])
        underscore = self.client.get("/api/events?search=%25_").json()["events"]
        self.assertEqual([event["message"] for event in underscore], ["含通配符的 100%_文本"])

    def test_activity_log_pages_through_history_with_cursors(self) -> None:
        self.login()
        self._seed_events()

        first = self.client.get("/api/events?limit=2").json()
        self.assertEqual(len(first["events"]), 2)
        self.assertTrue(first["has_more"])

        oldest_id = first["events"][-1]["id"]
        older = self.client.get(f"/api/events?limit=2&before_id={oldest_id}").json()
        self.assertEqual(len(older["events"]), 2)
        self.assertTrue(all(event["id"] < oldest_id for event in older["events"]))

        newest_id = first["events"][0]["id"]
        tail = self.client.get(f"/api/events?after_id={newest_id}").json()
        self.assertEqual(tail["events"], [])

        asyncio.run(TaskStore(self.settings.database_path).append_event("system", "info", "新事件", retention=5000))
        appended = self.client.get(f"/api/events?after_id={newest_id}").json()["events"]
        self.assertEqual([event["message"] for event in appended], ["新事件"])

    def test_activity_log_facets_count_independently_of_selection(self) -> None:
        self.login()
        self._seed_events()

        facets = self.client.get("/api/events?levels=error").json()["facets"]
        # Counts must ignore the level selection, otherwise every other chip would
        # read zero as soon as one is picked.
        self.assertGreaterEqual(facets["levels"]["warning"], 1)
        self.assertGreaterEqual(facets["levels"]["success"], 2)
        self.assertGreaterEqual(facets["categories"]["upload"], 2)

        narrowed = self.client.get("/api/events?search=归档").json()["facets"]
        self.assertEqual(narrowed["levels"], {"success": 1})
        self.assertEqual(narrowed["categories"], {"upload": 1})
        self.assertEqual(narrowed["matched"], 1)

    def test_activity_log_export_streams_filtered_rows(self) -> None:
        self.login()
        self._seed_events()

        text = self.client.get("/api/events/export?format=txt&categories=upload")
        self.assertEqual(text.status_code, 200)
        self.assertIn("attachment;", text.headers["content-disposition"])
        lines = [line for line in text.text.splitlines() if line]
        self.assertEqual(len(lines), 2)
        self.assertIn("[upload]", lines[0])
        self.assertIn("跳过 3 个文件", text.text)
        self.assertNotIn("拉流失败", text.text)

        jsonl = self.client.get("/api/events/export?format=jsonl&levels=error")
        payloads = [json.loads(line) for line in jsonl.text.splitlines() if line]
        self.assertEqual([item["message"] for item in payloads], ["「陪玩夜场」拉流失败"])
        self.assertEqual(payloads[0]["task_id"], "task-b")

        self.assertEqual(self.client.get("/api/events/export?format=csv").status_code, 422)

    def test_activity_log_clear_requires_csrf_and_records_the_wipe(self) -> None:
        self.login()
        self._seed_events()

        self.assertEqual(self.client.delete("/api/events").status_code, 403)

        payload = self.client.delete("/api/events", headers=self.csrf_headers).json()
        self.assertEqual(payload["events"][0]["category"], "system")
        self.assertIn("运行日志已清空", payload["events"][0]["message"])
        # The wipe itself is the only surviving entry, so the page never goes blank.
        self.assertEqual(len(payload["events"]), 1)
        self.assertEqual(payload["summary"]["total"], 1)

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
        # The overview card reports the last run and the configured drives; the drive
        # rows themselves are rendered from /api/cloud/archive rather than hard-coded.
        self.assertIn('id="overview-archive-status"', overview_page.text)
        self.assertIn('id="overview-last-run"', overview_page.text)
        self.assertIn('id="overview-archive-providers"', overview_page.text)
        self.assertIn('id="cloud-run-button"', archive_page.text)
        self.assertIn('data-provider-configure="quark"', archive_page.text)
        self.assertIn('data-provider-configure="wopan"', archive_page.text)
        self.assertIn('data-provider-configure="baidu"', archive_page.text)
        self.assertIn('data-provider-configure="pan115"', archive_page.text)
        self.assertIn('data-provider-configure="guangya"', archive_page.text)
        self.assertIn('id="provider-config-dialog"', archive_page.text)
        self.assertIn('id="run-targets"', archive_page.text)
        for icon_path in (
            "/static/provider-quark.png",
            "/static/provider-wopan.png",
            "/static/provider-baidu.png",
            "/static/provider-pan115.png",
            "/static/provider-guangya.png",
        ):
            self.assertIn(icon_path, archive_page.text)
            self.assertEqual(self.client.get(icon_path).status_code, 200)
        self.assertIn('id="archive-schedule-form"', settings_page.text)
        self.assertIn('name="recording_output_format"', settings_page.text)
        self.assertIn('name="recording_segment_seconds"', settings_page.text)
        self.assertIn('name="recording_segment_count"', settings_page.text)
        self.assertIn('name="upload_mode"', settings_page.text)
        self.assertIn('value="recording_completed"', settings_page.text)
        self.assertNotIn('name="quark_', settings_page.text)
        self.assertNotIn('name="wopan_', settings_page.text)

        initial = self.client.get("/api/cloud/archive")
        self.assertEqual(initial.status_code, 200)
        self.assertFalse(initial.json()["enabled"])
        self.assertEqual(initial.json()["schedule"]["mode"], "scheduled")
        self.assertEqual(
            [provider["name"] for provider in initial.json()["providers"]],
            ["quark", "wopan", "baidu", "pan115", "guangya"],
        )
        quark_payload = {
            "enabled": True,
            "cookie": "session=quark-secret-cookie",
            "clear_cookie": False,
            "root_id": "0",
            "upload_path": "/ignored-quark-path",
        }
        rejected = self.client.put("/api/cloud/archive/providers/quark", json=quark_payload)
        self.assertEqual(rejected.status_code, 403)

        saved = self.client.put(
            "/api/cloud/archive/providers/quark",
            headers=self.csrf_headers,
            json=quark_payload,
        )
        self.assertEqual(saved.status_code, 200)
        saved_payload = saved.json()
        self.assertTrue(saved_payload["enabled"])
        self.assertTrue(saved_payload["quark"]["credential_configured"])
        self.assertFalse(saved_payload["wopan"]["access_token_configured"])
        self.assertEqual(saved_payload["schedule"]["hour"], 1)
        self.assertEqual(saved_payload["quark"]["upload_path"], CLOUD_ARCHIVE_ROOT)
        generic_quark = next(provider for provider in saved_payload["providers"] if provider["name"] == "quark")
        self.assertTrue(generic_quark["credential_configured"])
        self.assertEqual(generic_quark["options"], {"root_id": "0"})
        self.assertNotIn("quark-secret-cookie", saved.text)

        schedule = self.client.put(
            "/api/cloud/archive/schedule",
            headers=self.csrf_headers,
            json={
                "mode": "recording_completed",
                "hour": 2,
                "min_age_minutes": 10,
                "timeout_seconds": 300,
            },
        )
        self.assertEqual(schedule.status_code, 200)
        self.assertEqual(schedule.json()["schedule"]["mode"], "recording_completed")
        self.assertEqual(schedule.json()["schedule"]["hour"], 2)
        self.assertIsNone(schedule.json()["schedule"]["next_run_at"])

        wopan_payload = {
            "enabled": True,
            "access_token": "wopan-access-token-123456",
            "refresh_token": "wopan-refresh-secret",
            "clear_tokens": False,
            "root_id": "0",
            "family_id": "",
            "upload_path": "/ignored-wopan-path",
        }
        saved_wopan = self.client.put(
            "/api/cloud/archive/providers/wopan",
            headers=self.csrf_headers,
            json=wopan_payload,
        )
        self.assertEqual(saved_wopan.status_code, 200)
        saved_payload = saved_wopan.json()
        self.assertTrue(saved_payload["quark"]["credential_configured"])
        self.assertTrue(saved_payload["wopan"]["access_token_configured"])
        self.assertTrue(saved_payload["wopan"]["refresh_token_configured"])
        self.assertEqual(saved_payload["wopan"]["upload_path"], CLOUD_ARCHIVE_ROOT)
        self.assertEqual(saved_payload["schedule"]["mode"], "recording_completed")
        self.assertEqual(saved_payload["schedule"]["hour"], 2)
        self.assertNotIn("wopan-access-token", saved_wopan.text)
        self.assertNotIn("wopan-refresh-secret", saved_wopan.text)

        quark_payload["cookie"] = None
        preserved = self.client.put(
            "/api/cloud/archive/providers/quark",
            headers=self.csrf_headers,
            json=quark_payload,
        )
        self.assertEqual(preserved.status_code, 200)
        self.assertTrue(preserved.json()["quark"]["credential_configured"])
        self.assertTrue(preserved.json()["wopan"]["refresh_token_configured"])
        self.assertEqual(preserved.json()["schedule"]["mode"], "recording_completed")
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
        self.assertEqual(
            [(target["name"], target["status"]) for target in last_status["targets"]],
            [("quark", "success"), ("wopan", "success")],
        )

        quark_payload["cookie"] = "new-cookie"
        quark_payload["clear_cookie"] = True
        invalid_clear = self.client.put(
            "/api/cloud/archive/providers/quark",
            headers=self.csrf_headers,
            json=quark_payload,
        )
        self.assertEqual(invalid_clear.status_code, 422)

        quark_payload["cookie"] = None
        quark_payload["clear_cookie"] = True
        quark_payload["enabled"] = False
        cleared_quark = self.client.put(
            "/api/cloud/archive/providers/quark",
            headers=self.csrf_headers,
            json=quark_payload,
        )
        self.assertEqual(cleared_quark.status_code, 200)
        self.assertFalse(cleared_quark.json()["quark"]["credential_configured"])

        wopan_payload["enabled"] = False
        wopan_payload["access_token"] = None
        wopan_payload["refresh_token"] = None
        wopan_payload["clear_tokens"] = True
        cleared = self.client.put(
            "/api/cloud/archive/providers/wopan",
            headers=self.csrf_headers,
            json=wopan_payload,
        )
        self.assertEqual(cleared.status_code, 200)
        self.assertFalse(cleared.json()["enabled"])
        self.assertFalse(cleared.json()["wopan"]["access_token_configured"])
        disabled_run = self.client.post("/api/cloud/archive/run", headers=self.csrf_headers)
        self.assertEqual(disabled_run.status_code, 409)

    def test_generic_cloud_provider_config_is_persisted_without_exposing_secrets(self) -> None:
        self.login()
        providers = {
            "quark": {
                "credentials": {"cookie": "generic-quark-secret-cookie"},
                "options": {"root_id": "0"},
            },
            "wopan": {
                "credentials": {
                    "access_token": "generic-wopan-access-token-123456",
                    "refresh_token": "generic-wopan-refresh-token",
                },
                "options": {"root_id": "0", "family_id": ""},
            },
            "baidu": {
                "credentials": {"access_token": "baidu-secret-access-token"},
                "options": {},
            },
            "pan115": {
                "credentials": {"cookie": "UID=manual-uid; CID=manual-cid; SEID=manual-seid"},
                "options": {"root_id": "0"},
            },
            "guangya": {
                "credentials": {
                    "client_id": "guangya-client-id",
                    "access_token": "guangya-secret-access-token",
                },
                "options": {"root_id": ""},
            },
        }
        secrets: list[str] = []
        for provider, values in providers.items():
            with self.subTest(provider=provider):
                secrets.extend(values["credentials"].values())
                response = self.client.put(
                    f"/api/cloud/archive/providers/{provider}/config",
                    headers=self.csrf_headers,
                    json={"enabled": True, "clear_credentials": False, **values},
                )
                self.assertEqual(response.status_code, 200)
                provider_view = next(item for item in response.json()["providers"] if item["name"] == provider)
                self.assertTrue(provider_view["credential_configured"])
                for secret in values["credentials"].values():
                    self.assertNotIn(secret, response.text)

        overview = self.client.get("/api/cloud/archive")
        self.assertEqual(overview.status_code, 200)
        for secret in secrets:
            self.assertNotIn(secret, overview.text)

        with closing(sqlite3.connect(self.settings.database_path)) as connection:
            stored = connection.execute("SELECT config_json FROM cloud_upload_config WHERE id = 1").fetchone()[0]
        for secret in secrets:
            self.assertIn(secret, stored)

        conflict = self.client.put(
            "/api/cloud/archive/providers/pan115/config",
            headers=self.csrf_headers,
            json={
                "enabled": True,
                "credentials": {"cookie": "UID=new; CID=new; SEID=new"},
                "clear_credentials": True,
                "options": {"root_id": "0"},
            },
        )
        self.assertEqual(conflict.status_code, 422)

        cleared = self.client.put(
            "/api/cloud/archive/providers/baidu/config",
            headers=self.csrf_headers,
            json={"enabled": False, "credentials": {}, "clear_credentials": True, "options": {}},
        )
        self.assertEqual(cleared.status_code, 200)
        self.assertFalse(cleared.json()["baidu"]["credential_configured"])

    def test_partial_cloud_credential_edit_uses_latest_runtime_tokens_as_baseline(self) -> None:
        self.login()
        configured = self.client.put(
            "/api/cloud/archive/providers/wopan",
            headers=self.csrf_headers,
            json={
                "enabled": True,
                "access_token": "configured-access-token-123456",
                "refresh_token": "configured-refresh-token",
                "clear_tokens": False,
                "root_id": "0",
                "family_id": "",
            },
        )
        self.assertEqual(configured.status_code, 200)

        with closing(sqlite3.connect(self.settings.database_path)) as connection:
            raw = json.loads(
                connection.execute("SELECT config_json FROM cloud_upload_config WHERE id = 1").fetchone()[0]
            )
        defaults = raw["providers"]["wopan"]["credentials"]
        fingerprint = hashlib.sha256(
            json.dumps(defaults, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        asyncio.run(self.store.resolve_cloud_credentials("wopan", fingerprint, defaults))
        asyncio.run(
            self.store.patch_cloud_credentials(
                "wopan",
                fingerprint,
                {
                    "access_token": "runtime-access-token-123456",
                    "refresh_token": "runtime-refresh-token-rotated",
                },
            )
        )

        updated = self.client.put(
            "/api/cloud/archive/providers/wopan",
            headers=self.csrf_headers,
            json={
                "enabled": True,
                "access_token": "manually-replaced-access-123456",
                "refresh_token": None,
                "clear_tokens": False,
                "root_id": "0",
                "family_id": "",
            },
        )
        self.assertEqual(updated.status_code, 200)
        with closing(sqlite3.connect(self.settings.database_path)) as connection:
            raw = json.loads(
                connection.execute("SELECT config_json FROM cloud_upload_config WHERE id = 1").fetchone()[0]
            )
        self.assertEqual(
            raw["providers"]["wopan"]["credentials"],
            {
                "access_token": "manually-replaced-access-123456",
                "refresh_token": "runtime-refresh-token-rotated",
            },
        )
        self.assertIsNone(
            asyncio.run(
                self.store.patch_cloud_credentials(
                    "wopan",
                    fingerprint,
                    {"refresh_token": "late-stale-refresh-token"},
                )
            )
        )

    def test_cloud_qr_login_saves_credentials_without_returning_secrets(self) -> None:
        self.login()
        archive_page = self.client.get("/archive")
        settings_page = self.client.get("/settings")
        self.assertIn("data-cloud-login", archive_page.text)
        self.assertIn('id="cloud-login-dialog"', archive_page.text)
        self.assertNotIn('id="cloud-login-dialog"', settings_page.text)

        rejected = self.client.post("/api/cloud/login/quark")
        self.assertEqual(rejected.status_code, 403)

        for provider in ("quark", "wopan", "pan115", "baidu", "guangya"):
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
        self.assertTrue(archive["pan115"]["credential_configured"])
        self.assertEqual(archive["pan115"]["configured_credentials"], ["cookie"])
        self.assertTrue(archive["baidu"]["credential_configured"])
        self.assertEqual(archive["baidu"]["configured_credentials"], ["cookie"])
        self.assertTrue(archive["guangya"]["credential_configured"])
        self.assertNotIn("qr-seid", json.dumps(archive))

    def test_baidu_openlist_online_api_switches_cookie_auth_to_oauth(self) -> None:
        self.login()
        archive_page = self.client.get("/archive")
        self.assertIn('id="baidu-openlist-login"', archive_page.text)
        self.assertIn('id="baidu-openlist-dialog"', archive_page.text)

        configured = self.client.put(
            "/api/cloud/archive/providers/baidu/config",
            headers=self.csrf_headers,
            json={
                "enabled": True,
                "credentials": {"cookie": "BDUSS=old-cookie; STOKEN=old-stoken"},
                "clear_credentials": False,
                "options": {},
            },
        )
        self.assertEqual(configured.status_code, 200)

        started = self.client.post("/api/cloud/login/baidu/openlist", headers=self.csrf_headers)
        self.assertEqual(started.status_code, 200)
        self.assertTrue(started.json()["authorization_url"].startswith("https://openapi.baidu.com/"))

        exchanged = self.client.post(
            "/api/cloud/login/baidu/openlist/exchange",
            headers=self.csrf_headers,
            json={"authorization_code": "one-time-baidu-code"},
        )
        self.assertEqual(exchanged.status_code, 200)
        self.assertNotIn("openlist-baidu-access-token", exchanged.text)
        self.assertEqual(self.baidu_openlist_auth.codes, ["one-time-baidu-code"])
        baidu = exchanged.json()["baidu"]
        self.assertEqual(
            baidu["configured_credentials"],
            ["access_token", "refresh_token", "client_id", "client_secret"],
        )

        with closing(sqlite3.connect(self.settings.database_path)) as connection:
            stored = connection.execute("SELECT config_json FROM cloud_upload_config WHERE id = 1").fetchone()[0]
            raw = json.loads(stored)
        credentials = raw["providers"]["baidu"]["credentials"]
        self.assertNotIn("cookie", credentials)
        self.assertEqual(credentials["access_token"], "openlist-baidu-access-token")
