import asyncio
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase, TestCase

from douyin_recorder.cloud import CloudUploadError, UploadProgress
from douyin_recorder.settings import CLOUD_ARCHIVE_ROOT, Settings
from douyin_recorder.web.schemas import TaskConfig, TaskStatus
from douyin_recorder.web.store import TaskStore
from douyin_recorder.web.uploader import RecordingUploadService, UploadJob, UploadTarget


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

    async def test_manual_trigger_runs_in_background_and_rejects_overlap(self) -> None:
        recording = make_old_file(self.settings.recordings_dir / "主播" / "manual.ts", b"video", self.now)
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
        self.assertEqual(service.last_execution.status, "success")
        self.assertEqual(service.last_execution.summary.deleted_files, 1)

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


class ManualRecordingUploadTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.now = datetime(2026, 7, 12, 1, 0, tzinfo=timezone(timedelta(hours=8)))
        self.settings = make_settings(self.root)
        self.settings.prepare()
        self.store = TaskStore(self.settings.database_path)
        await self.store.initialize()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    def make_service(self, settings: Settings | None = None, **overrides) -> RecordingUploadService:
        started = self.started
        release = self.release

        class BlockingUploadClient(FakeUploadClient):
            async def upload_verified(
                self,
                local_path: Path,
                remote_path: str,
                *,
                progress: UploadProgress | None = None,
            ) -> bool:
                if progress is not None:
                    progress("uploading", 4)
                started.set()
                await release.wait()
                return await super().upload_verified(local_path, remote_path, progress=progress)

        clients = {"quark": BlockingUploadClient(), "wopan": BlockingUploadClient()}
        return RecordingUploadService(
            settings or self.settings,
            self.store,
            client_factory=lambda target: clients[target.name],
            clock=lambda: self.now,
            **overrides,
        )

    async def test_manual_upload_streams_progress_then_deletes_local(self) -> None:
        recording = make_old_file(self.settings.recordings_dir / "主播" / "manual.ts", b"video-bytes", self.now)
        service = self.make_service()

        job = await service.enqueue_file("主播/manual.ts")
        self.assertEqual(job.status, "queued")
        self.assertEqual(job.size, len(b"video-bytes"))

        await self.started.wait()
        self.assertEqual(job.status, "running")
        self.assertEqual(job.stage, "uploading")
        self.assertEqual(job.target, "quark")
        self.assertEqual(job.target_index, 0)
        self.assertEqual(job.target_count, 2)
        self.assertEqual(job.uploaded_bytes, 4)

        self.release.set()
        await service._job_worker

        self.assertEqual(job.status, "success")
        self.assertEqual(job.stage, "")
        self.assertEqual(job.uploaded_copies, 2)
        self.assertEqual(job.uploaded_bytes, job.size)
        self.assertTrue(job.deleted)
        self.assertIsNone(job.error)
        self.assertFalse(recording.exists())
        self.assertEqual([item.path for item in service.jobs()], ["主播/manual.ts"])

    async def test_manual_uploads_run_one_at_a_time_and_cancel_keeps_files(self) -> None:
        first = make_old_file(self.settings.recordings_dir / "主播" / "a.ts", b"first", self.now)
        second = make_old_file(self.settings.recordings_dir / "主播" / "b.ts", b"second", self.now)
        service = self.make_service()

        job_a = await service.enqueue_file("主播/a.ts")
        job_b = await service.enqueue_file("主播/b.ts")
        await self.started.wait()
        self.assertEqual(job_a.status, "running")
        self.assertEqual(job_b.status, "queued")

        self.assertTrue(service.cancel_file("主播/b.ts"))
        self.assertEqual(job_b.status, "cancelled")
        self.assertTrue(service.cancel_file("主播/a.ts"))
        await service._job_worker

        self.assertEqual(job_a.status, "cancelled")
        self.assertIn("本地文件保持不变", job_a.error)
        self.assertTrue(first.exists())
        self.assertTrue(second.exists())
        self.assertFalse(service.cancel_file("主播/a.ts"))
        self.assertFalse(service.cancel_file("主播/未知.ts"))

    async def test_manual_upload_rejects_unusable_recordings(self) -> None:
        recording = make_old_file(self.settings.recordings_dir / "正在直播" / "current.flv", b"active", self.now)
        empty = make_old_file(self.settings.recordings_dir / "主播" / "empty.ts", b"", self.now)
        (self.settings.recordings_dir / "主播" / "notes.txt").write_text("ignore", encoding="utf-8")
        service = self.make_service(active_directories_provider=lambda: {recording.parent.resolve()})

        for relative_path, fragment in (
            ("主播/missing.ts", "不存在"),
            ("主播/empty.ts", "为空"),
            ("主播/notes.txt", "视频"),
            ("../outside.ts", "录像目录"),
            ("正在直播/current.flv", "正在录制"),
        ):
            with self.subTest(path=relative_path):
                with self.assertRaises(CloudUploadError) as error:
                    await service.enqueue_file(relative_path)
                self.assertIn(fragment, str(error.exception))
        self.assertTrue(empty.exists())
        self.assertEqual(service.jobs(), [])

    async def test_batch_upload_queues_every_recording_below_the_given_directory(self) -> None:
        anchor = self.settings.recordings_dir / "主播"
        first = make_old_file(anchor / "2026-07-11" / "a.ts", b"first", self.now)
        second = make_old_file(anchor / "2026-07-12" / "b.mp4", b"second", self.now)
        other = make_old_file(self.settings.recordings_dir / "别的主播" / "c.flv", b"other", self.now)
        make_old_file(anchor / "2026-07-12" / "empty.ts", b"", self.now)
        (anchor / "2026-07-12" / "notes.txt").write_text("ignore", encoding="utf-8")
        live = make_old_file(self.settings.recordings_dir / "正在直播" / "live.flv", b"live", self.now)
        service = self.make_service(active_directories_provider=lambda: {live.parent.resolve()})

        # A subdirectory scan stays inside that subtree and ignores the stability window.
        scoped = await service.collect_manual_candidates("主播")
        self.assertEqual(
            sorted(candidate.relative_path.as_posix() for candidate in scoped),
            ["主播/2026-07-11/a.ts", "主播/2026-07-12/b.mp4"],
        )

        everything = await service.collect_manual_candidates("")
        self.assertEqual(
            sorted(candidate.relative_path.as_posix() for candidate in everything),
            ["主播/2026-07-11/a.ts", "主播/2026-07-12/b.mp4", "别的主播/c.flv"],
        )

        jobs = await service.enqueue_directory("")
        self.assertEqual(len(jobs), 3)
        self.assertEqual([job.status for job in jobs], ["queued", "queued", "queued"])
        await self.started.wait()
        self.assertEqual([job.status for job in jobs].count("running"), 1)

        # Re-running skips what is already queued instead of duplicating it.
        with self.assertRaises(CloudUploadError) as duplicate:
            await service.enqueue_directory("")
        self.assertIn("已在上传队列中", str(duplicate.exception))

        self.assertEqual(service.cancel_all(), 3)
        await service._job_worker
        self.assertEqual({job.status for job in jobs}, {"cancelled"})
        for path in (first, second, other, live):
            self.assertTrue(path.exists())

    async def test_batch_upload_rejects_bad_directories_and_empty_results(self) -> None:
        service = self.make_service()
        for relative_path, fragment in (("../outside", "录像目录内"), ("不存在的主播", "目录不存在")):
            with self.subTest(path=relative_path):
                with self.assertRaises(CloudUploadError) as error:
                    await service.collect_manual_candidates(relative_path)
                self.assertIn(fragment, str(error.exception))

        with self.assertRaises(CloudUploadError) as empty:
            await service.enqueue_directory("")
        self.assertIn("没有可上传的录像", str(empty.exception))
        self.assertEqual(service.cancel_all(), 0)

    async def test_manual_upload_requires_an_enabled_target_and_rejects_duplicates(self) -> None:
        make_old_file(self.settings.recordings_dir / "主播" / "manual.ts", b"video", self.now)
        disabled = make_settings(self.root, quark_cookie="", wopan_access_token="", wopan_refresh_token="")
        with self.assertRaises(CloudUploadError) as error:
            await self.make_service(disabled).enqueue_file("主播/manual.ts")
        self.assertIn("网盘上传目标", str(error.exception))

        service = self.make_service()
        await service.enqueue_file("主播/manual.ts")
        with self.assertRaises(CloudUploadError) as duplicate:
            await service.enqueue_file("主播/manual.ts")
        self.assertIn("已在上传队列中", str(duplicate.exception))
        await service.shutdown()


class UploadSpeedTests(TestCase):
    def make_job(self, size: int = 1000) -> tuple[UploadJob, list[float]]:
        now = [0.0]
        job = UploadJob(
            path="主播/a.ts",
            name="a.ts",
            size=size,
            created_at=datetime(2026, 7, 12, tzinfo=timezone.utc),
            status="running",
            monotonic=lambda: now[0],
        )
        return job, now

    def test_speed_uses_the_transferred_bytes_over_the_sample_window(self) -> None:
        job, now = self.make_job()
        for step in range(1, 5):
            now[0] = float(step)
            job.report("uploading", step * 100)
        # 400 bytes across the 4 seconds since the first sample.
        self.assertEqual(job.speed_bytes_per_second, 100)
        self.assertEqual(job.uploaded_bytes, 400)

    def test_speed_stays_zero_until_the_window_is_wide_enough(self) -> None:
        job, now = self.make_job()
        job.report("uploading", 100)
        self.assertEqual(job.speed_bytes_per_second, 0)
        now[0] = 0.2
        job.report("uploading", 900)
        self.assertEqual(job.speed_bytes_per_second, 0)

    def test_dedup_and_verify_reports_never_count_as_transferred_bytes(self) -> None:
        job, now = self.make_job()
        job.report("preparing", 0)
        now[0] = 3.0
        # A remote hit jumps straight to the full size without moving any bytes.
        job.report("verifying", 1000)
        self.assertEqual(job.uploaded_bytes, 1000)
        self.assertEqual(job.transferred_bytes, 0)
        self.assertEqual(job.speed_bytes_per_second, 0)

    def test_retry_rewind_and_target_switch_do_not_produce_negative_speed(self) -> None:
        job, now = self.make_job()
        for step, value in ((1.0, 400), (2.0, 800)):
            now[0] = step
            job.report("uploading", value)
        now[0] = 3.0
        job.report("uploading", 400)  # a retried part restarts at its own offset
        self.assertEqual(job.transferred_bytes, 800)
        now[0] = 4.0
        job.uploaded_bytes = 0  # the next target starts over
        job.report("uploading", 200)
        self.assertEqual(job.transferred_bytes, 1000)
        self.assertGreater(job.speed_bytes_per_second, 0)

    def test_speed_decays_to_zero_while_a_transfer_is_stalled(self) -> None:
        job, now = self.make_job()
        for step in range(1, 4):
            now[0] = float(step)
            job.report("uploading", step * 100)
        now[0] = 5.0
        self.assertLess(job.speed_bytes_per_second, 100)
        now[0] = 20.0
        self.assertEqual(job.speed_bytes_per_second, 0)

    def test_finished_jobs_report_no_speed(self) -> None:
        job, now = self.make_job()
        for step in range(1, 5):
            now[0] = float(step)
            job.report("uploading", step * 100)
        job.status = "success"
        self.assertEqual(job.speed_bytes_per_second, 0)


class UploadScheduleTests(TestCase):
    def test_next_run_is_one_am_local_time(self) -> None:
        tz = timezone(timedelta(hours=8))
        before = datetime(2026, 7, 12, 0, 30, tzinfo=tz)
        exact = datetime(2026, 7, 12, 1, 0, tzinfo=tz)
        after = datetime(2026, 7, 12, 1, 30, tzinfo=tz)

        self.assertEqual(RecordingUploadService.seconds_until_next_run(before, 1), 30 * 60)
        self.assertEqual(RecordingUploadService.seconds_until_next_run(exact, 1), 0)
        self.assertEqual(RecordingUploadService.seconds_until_next_run(after, 1), 23.5 * 3600)
