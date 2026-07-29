import asyncio
import json
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from douyin_recorder.models import LiveInfo
from douyin_recorder.settings import CLOUD_ARCHIVE_ROOT, Settings
from douyin_recorder.web.app import create_app
from douyin_recorder.web.auth import SESSION_COOKIE_NAME
from douyin_recorder.web.cloud_login import CloudLoginPoll
from douyin_recorder.web.recordings import RecordingPreviewCache, build_remux_command
from douyin_recorder.web.schemas import TaskStatus
from douyin_recorder.web.store import TaskStore
from douyin_recorder.web.uploader import UploadJob


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

    def test_recording_uploads_expose_queue_progress_and_cancellation(self) -> None:
        self.login()
        player_script = self.client.get("/static/recordings.js").text
        self.assertIn('data-action="upload"', player_script)
        self.assertIn('data-action="cancel-upload"', player_script)
        self.assertIn("/api/recordings/uploads", player_script)
        self.assertIn("uploadRatio", player_script)
        self.assertIn("uploadSpeed", player_script)
        self.assertIn("speed_bytes_per_second", player_script)
        self.assertIn("progress-track", player_script)

        recording_dir = self.settings.recordings_dir / "测试主播" / "2026-07-12"
        recording_dir.mkdir(parents=True)
        video = recording_dir / "片段.mp4"
        video.write_bytes(b"0123456789")
        (recording_dir / "内部信息.txt").write_text("not public", encoding="utf-8")
        path = "测试主播/2026-07-12/片段.mp4"

        empty = self.client.get("/api/recordings/uploads")
        self.assertEqual(empty.status_code, 200)
        self.assertEqual(empty.json(), {"jobs": []})

        for payload_path, expected in (
            ("测试主播/2026-07-12/缺失.mp4", 404),
            ("测试主播/2026-07-12/内部信息.txt", 404),
            ("../outside.mp4", 400),
        ):
            with self.subTest(path=payload_path):
                rejected = self.client.post(
                    "/api/recordings/uploads",
                    json={"path": payload_path},
                    headers=self.csrf_headers,
                )
                self.assertEqual(rejected.status_code, expected)

        # No cloud target is configured in this fixture, so the queue refuses the file.
        disabled = self.client.post("/api/recordings/uploads", json={"path": path}, headers=self.csrf_headers)
        self.assertEqual(disabled.status_code, 409)
        self.assertIn("网盘上传目标", disabled.json()["detail"])

        service = self.app.state.upload_service
        job = UploadJob(
            path=path,
            name=video.name,
            size=10,
            created_at=datetime.now(timezone.utc),
            status="running",
            stage="uploading",
            target="quark",
            target_count=2,
            uploaded_bytes=4,
        )
        service._jobs[path] = job

        running = self.client.get("/api/recordings/uploads").json()["jobs"]
        self.assertEqual(len(running), 1)
        self.assertEqual(running[0]["path"], path)
        self.assertEqual(running[0]["status"], "running")
        self.assertEqual(running[0]["stage"], "uploading")
        self.assertEqual(running[0]["target"], "quark")
        self.assertEqual(running[0]["target_count"], 2)
        self.assertEqual(running[0]["uploaded_bytes"], 4)
        self.assertEqual(running[0]["size"], 10)
        self.assertEqual(running[0]["speed_bytes_per_second"], 0)
        self.assertFalse(running[0]["deleted"])

        # A running job is only cancellable through its own task, a queued one directly.
        def cancel_upload():
            return self.client.delete(f"/api/recordings/uploads/{path}", headers=self.csrf_headers)

        self.assertEqual(cancel_upload().status_code, 404)
        job.status = "queued"
        self.assertEqual(cancel_upload().status_code, 204)
        self.assertEqual(self.client.get("/api/recordings/uploads").json()["jobs"][0]["status"], "cancelled")
        self.assertEqual(cancel_upload().status_code, 404)
        self.assertTrue(video.exists())

    def test_batch_recording_upload_previews_scope_before_queueing(self) -> None:
        self.login()
        page = self.client.get("/recordings")
        self.assertIn('id="upload-all-button"', page.text)
        self.assertIn('id="upload-queue"', page.text)
        player_script = self.client.get("/static/recordings.js").text
        self.assertIn("/api/recordings/uploads/batch", player_script)
        self.assertIn("/api/recordings/uploads/candidates", player_script)
        self.assertIn("renderUploadQueue", player_script)

        anchor = self.settings.recordings_dir / "测试主播"
        (anchor / "2026-07-12").mkdir(parents=True)
        (anchor / "2026-07-12" / "片段.mp4").write_bytes(b"0123456789")
        (anchor / "2026-07-12" / "空.ts").touch()
        (anchor / "2026-07-12" / "内部信息.txt").write_text("not public", encoding="utf-8")
        (self.settings.recordings_dir / "别的主播").mkdir(parents=True)
        (self.settings.recordings_dir / "别的主播" / "片段.flv").write_bytes(b"01234")

        root = self.client.get("/api/recordings/uploads/candidates")
        self.assertEqual(root.status_code, 200)
        self.assertEqual(root.json(), {"path": "", "count": 2, "total_size": 15})

        scoped = self.client.get("/api/recordings/uploads/candidates", params={"path": "测试主播"})
        self.assertEqual(scoped.json(), {"path": "测试主播", "count": 1, "total_size": 10})

        for path, expected in (("缺失的主播", 404), ("../", 404)):
            with self.subTest(path=path):
                missing = self.client.get("/api/recordings/uploads/candidates", params={"path": path})
                self.assertEqual(missing.status_code, expected)

        # No cloud target is configured in this fixture, so the batch is refused.
        batch = self.client.post("/api/recordings/uploads/batch", json={"path": ""}, headers=self.csrf_headers)
        self.assertEqual(batch.status_code, 409)
        self.assertIn("网盘上传目标", batch.json()["detail"])
        self.assertEqual(self.client.get("/api/recordings/uploads").json(), {"jobs": []})

        cancel_all = self.client.delete("/api/recordings/uploads", headers=self.csrf_headers)
        self.assertEqual(cancel_all.status_code, 404)

        service = self.app.state.upload_service
        service._jobs["测试主播/2026-07-12/片段.mp4"] = UploadJob(
            path="测试主播/2026-07-12/片段.mp4",
            name="片段.mp4",
            size=10,
            created_at=datetime.now(timezone.utc),
        )
        self.assertEqual(self.client.delete("/api/recordings/uploads", headers=self.csrf_headers).status_code, 204)
        self.assertEqual(self.client.get("/api/recordings/uploads").json()["jobs"][0]["status"], "cancelled")

    def test_upload_progress_page_subscribes_to_the_event_stream(self) -> None:
        self.login()
        player_script = self.client.get("/static/recordings.js").text
        self.assertIn("EventSource", player_script)
        self.assertIn("/api/recordings/uploads/stream", player_script)
        self.assertIn("fallBackToPolling", player_script)
        self.assertIn("/api/recordings/uploads/stream", [route.path for route in self.app.routes])

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


class UploadStreamTests(IsolatedAsyncioTestCase):
    """Starlette's TestClient buffers whole responses, so drive the ASGI app directly."""

    async def asyncSetUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.settings = Settings(
            data_dir=root,
            recordings_dir=root / "recordings",
            database_path=root / "tasks.db",
            web_username="admin",
            web_password="",  # disables the session middleware for this fixture
            allow_insecure=True,
            validate_binaries=False,
        )
        self.settings.prepare()
        self.store = TaskStore(self.settings.database_path)
        await self.store.initialize()
        self.app = create_app(
            self.settings,
            store=self.store,
            scheduler=FakeScheduler(self.store),
            inspect_client_factory=FakeInspectClient,
            cloud_login_flow_factory=FakeCloudLoginFlow,
        )
        self.service = self.app.state.upload_service

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_stream_pushes_a_snapshot_on_connect_and_on_every_change(self) -> None:
        disconnected = asyncio.Event()
        events: asyncio.Queue[dict] = asyncio.Queue()
        start: dict = {}

        async def receive() -> dict:
            await disconnected.wait()
            return {"type": "http.disconnect"}

        async def send(message: dict) -> None:
            if message["type"] == "http.response.start":
                start.update(message)
            elif message["type"] == "http.response.body" and message.get("body"):
                await events.put(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/recordings/uploads/stream",
            "raw_path": b"/api/recordings/uploads/stream",
            "query_string": b"",
            "root_path": "",
            "headers": [(b"host", b"testserver")],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        }

        async def next_snapshot() -> dict:
            while True:
                message = await asyncio.wait_for(events.get(), timeout=5)
                text = message["body"].decode()
                if text.startswith(": "):  # heartbeat
                    continue
                self.assertTrue(text.endswith("\n\n"))
                return json.loads(text.removeprefix("data: "))

        task = asyncio.create_task(self.app(scope, receive, send))
        try:
            opening = await next_snapshot()
            headers = {name.decode(): value.decode() for name, value in start["headers"]}
            self.assertEqual(start["status"], 200)
            self.assertTrue(headers["content-type"].startswith("text/event-stream"))
            self.assertEqual(headers["cache-control"], "no-store")
            self.assertEqual(headers["x-accel-buffering"], "no")
            self.assertEqual(opening, {"jobs": []})

            # A state change must push without the client asking for anything.
            self.service._jobs["主播/片段.mp4"] = UploadJob(
                path="主播/片段.mp4",
                name="片段.mp4",
                size=10,
                created_at=datetime.now(timezone.utc),
                status="running",
                stage="uploading",
                target="quark",
                target_count=1,
                uploaded_bytes=4,
            )
            self.service._notify()

            pushed = await next_snapshot()
            self.assertEqual(len(pushed["jobs"]), 1)
            self.assertEqual(pushed["jobs"][0]["path"], "主播/片段.mp4")
            self.assertEqual(pushed["jobs"][0]["uploaded_bytes"], 4)
            self.assertEqual(pushed["jobs"][0]["status"], "running")
        finally:
            disconnected.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_subscribers_are_registered_pre_armed_and_released(self) -> None:
        self.assertEqual(self.service._subscribers, set())
        first = self.service.subscribe()
        second = self.service.subscribe()
        # Pre-set so a new listener gets a snapshot without waiting for a change.
        self.assertTrue(first.is_set())
        self.assertTrue(second.is_set())

        first.clear()
        second.clear()
        self.service._notify()
        self.assertTrue(first.is_set())
        self.assertTrue(second.is_set())

        self.service.unsubscribe(first)
        first.clear()
        second.clear()
        self.service._notify()
        self.assertFalse(first.is_set())
        self.assertTrue(second.is_set())

        self.service.unsubscribe(second)
        self.service.unsubscribe(second)  # idempotent
        self.assertEqual(self.service._subscribers, set())
