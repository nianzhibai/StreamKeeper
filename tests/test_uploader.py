import asyncio
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase, TestCase

from stream_keeper.cloud import CloudUploadError, UploadProgress
from stream_keeper.settings import CLOUD_ARCHIVE_ROOT, Settings
from stream_keeper.web.recordings import RecordingPreviewCache
from stream_keeper.web.schemas import TaskConfig, TaskStatus
from stream_keeper.web.store import TaskStore
from stream_keeper.web.uploader import RecordingUploadService, UploadTarget


def make_settings(root: Path, **overrides) -> Settings:
    values = {
        "data_dir": root,
        "recordings_dir": root / "recordings",
        "database_path": root / "tasks.db",
        "web_password": "test-password",
        "quark_cookie": "cookie=value",
        "quark_upload_path": "/QuarkArchive",
        "wopan_access_token": "1234567890abcdef-access",
        "wopan_refresh_token": "refresh-token",
        "wopan_upload_path": "/WoPanArchive",
        "upload_min_age_minutes": 10,
        "validate_binaries": False,
    }
    values.update(overrides)
    return Settings(**values)


def make_old_file(path: Path, content: bytes, now: datetime) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    timestamp = (now - timedelta(minutes=20)).timestamp()
    os.utime(path, (timestamp, timestamp))
    return path


class FakeUploadClient:
    def __init__(self, *, fail_fragment: str | None = None) -> None:
        self.fail_fragment = fail_fragment
        self.calls: list[tuple[Path, str]] = []
        self.remote_paths: set[str] = set()
        self.close_count = 0

    async def upload_verified(
        self,
        local_path: Path,
        remote_path: str,
        *,
        progress: UploadProgress | None = None,
    ) -> bool:
        self.calls.append((local_path, remote_path))
        if progress is not None:
            progress("preparing", 0)
        if self.fail_fragment and self.fail_fragment in remote_path:
            raise CloudUploadError("模拟上传失败")
        size = local_path.stat().st_size
        if progress is not None:
            progress("uploading", size)
            progress("verifying", size)
        created = remote_path not in self.remote_paths
        self.remote_paths.add(remote_path)
        return created

    async def aclose(self) -> None:
        self.close_count += 1


class RecordingUploadServiceTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.now = datetime(2026, 7, 12, 1, 0, tzinfo=timezone(timedelta(hours=8)))
        self.settings = make_settings(self.root)
        self.settings.prepare()
        self.store = TaskStore(self.settings.database_path)
        await self.store.initialize()

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_uploads_stable_files_to_all_targets_then_deletes_local(self) -> None:
        stable = make_old_file(
            self.settings.recordings_dir / "测试主播" / "2026-07-11" / "stable.ts",
            b"video",
            self.now,
        )
        young = self.settings.recordings_dir / "测试主播" / "2026-07-11" / "young.mp4"
        young.write_bytes(b"young")
        empty = make_old_file(self.settings.recordings_dir / "测试主播" / "empty.mkv", b"", self.now)
        (self.settings.recordings_dir / "测试主播" / "notes.txt").write_text("ignore", encoding="utf-8")

        active = make_old_file(self.settings.recordings_dir / "正在直播" / "current.flv", b"active", self.now)
        provider_active = make_old_file(
            self.settings.recordings_dir / "刚开始直播" / "segment_000.ts",
            b"active-segment",
            self.now,
        )
        task = await self.store.create(TaskConfig(url="https://live.douyin.com/123456789"))
        await self.store.update_runtime(
            task.id,
            status=TaskStatus.RECORDING,
            output_path=str(active),
        )

        clients = {"quark": FakeUploadClient(), "wopan": FakeUploadClient()}
        service = RecordingUploadService(
            self.settings,
            self.store,
            client_factory=lambda target: clients[target.name],
            active_directories_provider=lambda: {provider_active.parent.resolve()},
            clock=lambda: self.now,
        )
        summary = await service.run_once()

        self.assertEqual(summary.scanned_files, 5)
        self.assertEqual(summary.skipped_files, 4)
        self.assertEqual(summary.uploaded_copies, 2)
        self.assertEqual(summary.deleted_files, 1)
        self.assertEqual(summary.failed_files, 0)
        self.assertFalse(stable.exists())
        self.assertTrue(young.exists())
        self.assertTrue(empty.exists())
        self.assertTrue(active.exists())
        self.assertTrue(provider_active.exists())
        self.assertEqual(clients["quark"].close_count, 1)
        self.assertEqual(clients["wopan"].close_count, 1)
        self.assertEqual(
            [clients[name].calls[0][1] for name in ("quark", "wopan")],
            [
                f"{CLOUD_ARCHIVE_ROOT}/测试主播/2026-07-11/stable.ts",
                f"{CLOUD_ARCHIVE_ROOT}/测试主播/2026-07-11/stable.ts",
            ],
        )

    async def test_partial_failure_keeps_file_and_next_run_resumes(self) -> None:
        recording = make_old_file(self.settings.recordings_dir / "主播" / "retry.ts", b"video", self.now)
        clients = {
            "quark": FakeUploadClient(),
            "wopan": FakeUploadClient(fail_fragment=CLOUD_ARCHIVE_ROOT),
        }

        def client_factory(target: UploadTarget) -> FakeUploadClient:
            return clients[target.name]

        service = RecordingUploadService(
            self.settings,
            self.store,
            client_factory=client_factory,
            clock=lambda: self.now,
        )

        first = await service.run_once()
        self.assertEqual(first.uploaded_copies, 1)
        self.assertEqual(first.failed_files, 1)
        self.assertTrue(recording.exists())

        clients["wopan"].fail_fragment = None
        second = await service.run_once()
        self.assertEqual(second.uploaded_copies, 1)
        self.assertEqual(second.deleted_files, 1)
        self.assertFalse(recording.exists())

    async def test_uploading_last_recording_prunes_empty_parent_directories(self) -> None:
        recording = make_old_file(
            self.settings.recordings_dir / "主播" / "2026-07-11" / "archived.ts",
            b"video",
            self.now,
        )
        clients = {"quark": FakeUploadClient(), "wopan": FakeUploadClient()}
        service = RecordingUploadService(
            self.settings,
            self.store,
            client_factory=lambda target: clients[target.name],
            clock=lambda: self.now,
        )

        summary = await service.run_once()

        self.assertEqual(summary.deleted_files, 1)
        self.assertFalse(recording.exists())
        self.assertFalse(recording.parent.exists())
        self.assertFalse(recording.parent.parent.exists())
        self.assertTrue(self.settings.recordings_dir.exists())

    async def test_upload_does_not_prune_a_directory_that_became_active(self) -> None:
        recording = make_old_file(
            self.settings.recordings_dir / "主播" / "2026-07-11" / "archived.ts",
            b"video",
            self.now,
        )
        calls = 0

        def active_directories() -> set[Path]:
            nonlocal calls
            calls += 1
            return set() if calls == 1 else {recording.parent.resolve()}

        clients = {"quark": FakeUploadClient(), "wopan": FakeUploadClient()}
        service = RecordingUploadService(
            self.settings,
            self.store,
            client_factory=lambda target: clients[target.name],
            active_directories_provider=active_directories,
            clock=lambda: self.now,
        )

        summary = await service.run_once()

        self.assertEqual(summary.deleted_files, 1)
        self.assertFalse(recording.exists())
        self.assertTrue(recording.parent.exists())
        self.assertTrue(recording.parent.parent.exists())

    async def test_manual_trigger_uses_archive_policy_and_rejects_overlap(self) -> None:
        recording = make_old_file(self.settings.recordings_dir / "主播" / "manual.ts", b"video", self.now)
        young = self.settings.recordings_dir / "主播" / "still-recording.ts"
        young.write_bytes(b"young")
        started = asyncio.Event()
        release = asyncio.Event()

        class BlockingUploadClient(FakeUploadClient):
            async def upload_verified(
                self,
                local_path: Path,
                remote_path: str,
                *,
                progress: UploadProgress | None = None,
            ) -> bool:
                started.set()
                await release.wait()
                return await super().upload_verified(local_path, remote_path, progress=progress)

        client = BlockingUploadClient()
        service = RecordingUploadService(
            self.settings,
            self.store,
            client_factory=lambda _target: client,
            clock=lambda: self.now,
        )

        self.assertTrue(await service.trigger("manual"))
        await started.wait()
        self.assertTrue(service.running)
        self.assertFalse(await service.trigger("manual"))
        release.set()
        await service._active_run

        self.assertFalse(service.running)
        self.assertFalse(recording.exists())
        self.assertTrue(young.exists())
        self.assertEqual(service.last_execution.status, "success")
        self.assertEqual(service.last_execution.summary.deleted_files, 1)
        self.assertEqual(service.last_execution.summary.skipped_files, 1)

    async def test_successful_upload_also_drops_the_browser_playback_cache(self) -> None:
        recording = make_old_file(self.settings.recordings_dir / "主播" / "played.ts", b"video", self.now)
        kept = make_old_file(self.settings.recordings_dir / "主播" / "young.ts", b"video", self.now)
        cache_dir = self.root / "preview-cache"
        cache_dir.mkdir()
        cache = RecordingPreviewCache(cache_dir, "ffmpeg")
        stat = recording.stat()
        preview = cache_dir / f"{cache._cache_key('主播/played.ts', (stat.st_size, stat.st_mtime_ns))}.mp4"
        preview.write_bytes(b"remuxed-for-the-browser")
        kept_stat = kept.stat()
        other = cache_dir / f"{cache._cache_key('主播/young.ts', (kept_stat.st_size, kept_stat.st_mtime_ns))}.mp4"
        other.write_bytes(b"still-needed")

        clients = {"quark": FakeUploadClient(), "wopan": FakeUploadClient(fail_fragment="young.ts")}
        service = RecordingUploadService(
            self.settings,
            self.store,
            client_factory=lambda target: clients[target.name],
            clock=lambda: self.now,
            preview_cache=cache,
        )
        summary = await service.run_once()

        self.assertEqual(summary.deleted_files, 1)
        self.assertEqual(summary.failed_files, 1)
        self.assertFalse(recording.exists())
        self.assertFalse(preview.exists())
        # The failed upload kept its recording, so its preview stays usable.
        self.assertTrue(kept.exists())
        self.assertTrue(other.exists())

    async def test_archive_run_reports_start_and_outcome_to_the_activity_log(self) -> None:
        make_old_file(self.settings.recordings_dir / "主播" / "ok.ts", b"video", self.now)
        make_old_file(self.settings.recordings_dir / "主播" / "broken.ts", b"video", self.now)
        clients = {
            "quark": FakeUploadClient(),
            "wopan": FakeUploadClient(fail_fragment="broken.ts"),
        }
        service = RecordingUploadService(
            self.settings,
            self.store,
            client_factory=lambda target: clients[target.name],
            clock=lambda: self.now,
        )

        self.assertTrue(await service.trigger("scheduled"))
        await service._active_run
        events = [(event.level, event.message) for event in await self.store.list_events(limit=20)]

        self.assertEqual(
            events,
            [
                ("warning", "定时归档完成，但有 1 个文件失败"),
                ("error", "broken.ts 归档失败，已保留本地文件"),
                ("info", "开始定时归档到网盘"),
            ],
        )
        # broken.ts reached quark before wopan rejected it, so three copies landed.
        self.assertIn("上传 3 个副本", (await self.store.list_events(limit=1))[0].detail)


class UploadScheduleTests(TestCase):
    def test_next_run_is_one_am_local_time(self) -> None:
        tz = timezone(timedelta(hours=8))
        before = datetime(2026, 7, 12, 0, 30, tzinfo=tz)
        exact = datetime(2026, 7, 12, 1, 0, tzinfo=tz)
        after = datetime(2026, 7, 12, 1, 30, tzinfo=tz)

        self.assertEqual(RecordingUploadService.seconds_until_next_run(before, 1), 30 * 60)
        self.assertEqual(RecordingUploadService.seconds_until_next_run(exact, 1), 0)
        self.assertEqual(RecordingUploadService.seconds_until_next_run(after, 1), 23.5 * 3600)
