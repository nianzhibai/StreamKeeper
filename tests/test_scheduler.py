import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from stream_keeper.models import LiveInfo, RecordingResult, SelectedSource
from stream_keeper.settings import Settings
from stream_keeper.web import scheduler as scheduler_module
from stream_keeper.web.scheduler import ResizableRecordingLimiter, TaskScheduler
from stream_keeper.web.schemas import TaskConfig, TaskStatus
from stream_keeper.web.store import TaskStore


def make_settings(root: Path) -> Settings:
    return Settings(
        data_dir=root,
        recordings_dir=root / "recordings",
        database_path=root / "tasks.db",
        validate_binaries=False,
    )


def make_config(*, monitor: bool = False, segment_seconds: int = 0, segment_count: int = 0) -> TaskConfig:
    return TaskConfig(
        url="https://live.douyin.com/123456789",
        quality="OD",
        output_format="ts",
        source="auto",
        segment_seconds=segment_seconds,
        segment_count=segment_count,
        monitor=monitor,
        interval_seconds=10,
    )


def make_info(is_live: bool) -> LiveInfo:
    return LiveInfo(
        platform="抖音",
        anchor_name="测试主播",
        is_live=is_live,
        title="测试直播",
        quality="OD" if is_live else None,
        m3u8_url="https://example.com/live.m3u8" if is_live else None,
        flv_url="https://example.com/live.flv?codec=h264" if is_live else None,
        record_url="https://example.com/live.m3u8" if is_live else None,
        live_url="https://live.douyin.com/123456789",
    )


class FakeClient:
    def __init__(self, info: LiveInfo) -> None:
        self.info = info
        self.calls = 0

    async def fetch(self, _url: str, _quality: str) -> LiveInfo:
        self.calls += 1
        return self.info


class FakeRecorder:
    def __init__(self, _options) -> None:
        pass

    async def record(self, info: LiveInfo) -> RecordingResult:
        return RecordingResult(
            "/data/recordings/test.ts",
            SelectedSource("flv", info.flv_url or ""),
            0,
        )


class LimitReachedRecorder(FakeRecorder):
    async def record(self, info: LiveInfo) -> RecordingResult:
        return RecordingResult(
            "/data/recordings/test_%03d.ts",
            SelectedSource("flv", info.flv_url or ""),
            0,
            limit_reached=True,
        )


async def wait_for_status(store: TaskStore, task_id: str, expected: TaskStatus, timeout: float = 1.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        record = await store.get(task_id)
        if record and record.status == expected:
            return record
        await asyncio.sleep(0.01)
    raise TimeoutError(f"Task {task_id} did not reach {expected.value}")


class SchedulerTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.settings = make_settings(self.root)
        self.settings.prepare()
        self.store = TaskStore(self.settings.database_path)
        await self.store.initialize()

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_increasing_recording_limit_releases_queued_work(self) -> None:
        limiter = ResizableRecordingLimiter(1)
        first_started = asyncio.Event()
        second_started = asyncio.Event()
        release = asyncio.Event()

        async def hold(started: asyncio.Event) -> None:
            async with limiter:
                started.set()
                await release.wait()

        first = asyncio.create_task(hold(first_started))
        second = asyncio.create_task(hold(second_started))
        await first_started.wait()
        await asyncio.sleep(0)
        self.assertFalse(second_started.is_set())
        self.assertEqual(limiter.active, 1)

        limiter.resize(2)
        await asyncio.wait_for(second_started.wait(), timeout=1)
        self.assertEqual(limiter.active, 2)

        release.set()
        await asyncio.gather(first, second)
        self.assertEqual(limiter.active, 0)

    async def test_lowering_recording_limit_drains_without_interrupting_active_work(self) -> None:
        limiter = ResizableRecordingLimiter(2)
        releases = {name: asyncio.Event() for name in ("first", "second", "third")}
        started: asyncio.Queue[str] = asyncio.Queue()

        async def hold(name: str) -> None:
            async with limiter:
                await started.put(name)
                await releases[name].wait()

        tasks = [asyncio.create_task(hold(name)) for name in releases]
        self.assertEqual({await started.get(), await started.get()}, {"first", "second"})
        self.assertEqual(limiter.active, 2)

        limiter.resize(1)
        releases["first"].set()
        await asyncio.sleep(0.02)
        self.assertTrue(started.empty())
        self.assertEqual(limiter.active, 1)

        releases["second"].set()
        self.assertEqual(await asyncio.wait_for(started.get(), timeout=1), "third")
        self.assertEqual(limiter.active, 1)
        releases["third"].set()
        await asyncio.gather(*tasks)
        self.assertEqual(limiter.active, 0)

    async def test_cancelled_recording_waiter_does_not_leak_capacity(self) -> None:
        limiter = ResizableRecordingLimiter(1)
        await limiter.acquire()
        waiter = asyncio.create_task(limiter.acquire())
        await asyncio.sleep(0)

        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        limiter.release()
        self.assertEqual(limiter.active, 0)

        async with limiter:
            self.assertEqual(limiter.active, 1)
        self.assertEqual(limiter.active, 0)

    async def test_one_shot_offline_task_stops(self) -> None:
        task = await self.store.create(make_config(monitor=False))
        client = FakeClient(make_info(False))
        scheduler = TaskScheduler(self.store, self.settings, client_factory=lambda: client)

        await scheduler.start(task.id)
        result = await wait_for_status(self.store, task.id, TaskStatus.STOPPED)

        self.assertFalse(result.enabled)
        self.assertFalse(result.is_live)
        self.assertEqual(client.calls, 1)
        await scheduler.shutdown()

    async def test_one_shot_live_task_records_and_stores_path(self) -> None:
        task = await self.store.create(make_config(monitor=False))
        scheduler = TaskScheduler(
            self.store,
            self.settings,
            client_factory=lambda: FakeClient(make_info(True)),
            recorder_factory=FakeRecorder,
        )

        await scheduler.start(task.id)
        result = await wait_for_status(self.store, task.id, TaskStatus.STOPPED)

        self.assertFalse(result.enabled)
        self.assertEqual(result.output_path, "/data/recordings/test.ts")
        await scheduler.shutdown()

    async def test_start_reuses_fresh_inspection_for_first_worker_iteration(self) -> None:
        task = await self.store.create(make_config(monitor=False))
        client = FakeClient(make_info(False))
        scheduler = TaskScheduler(
            self.store,
            self.settings,
            client_factory=lambda: client,
            recorder_factory=FakeRecorder,
        )

        await scheduler.start(task.id, initial_info=make_info(True))
        result = await wait_for_status(self.store, task.id, TaskStatus.STOPPED)

        self.assertEqual(client.calls, 0)
        self.assertEqual(result.anchor_name, "测试主播")
        self.assertEqual(result.output_path, "/data/recordings/test.ts")
        await scheduler.shutdown()

    async def test_successful_recording_notifies_completion_handler_after_status_update(self) -> None:
        task = await self.store.create(make_config(monitor=False))
        notifications: list[tuple[str, TaskStatus, bool]] = []

        async def recording_completed(result: RecordingResult) -> None:
            current = await self.store.get(task.id)
            assert current is not None
            notifications.append((result.output_path, current.status, current.enabled))

        scheduler = TaskScheduler(
            self.store,
            self.settings,
            client_factory=lambda: FakeClient(make_info(True)),
            recorder_factory=FakeRecorder,
            recording_completed_handler=recording_completed,
        )

        await scheduler.start(task.id)
        await wait_for_status(self.store, task.id, TaskStatus.STOPPED)
        for _ in range(100):
            if notifications:
                break
            await asyncio.sleep(0.01)

        self.assertEqual(notifications, [("/data/recordings/test.ts", TaskStatus.STOPPED, False)])
        await scheduler.shutdown()

    async def test_segment_limit_creates_one_shot_task(self) -> None:
        task = await self.store.create(make_config(monitor=True, segment_seconds=1800, segment_count=4))
        options_seen = []

        def recorder_factory(options):
            options_seen.append(options)
            return LimitReachedRecorder(options)

        scheduler = TaskScheduler(
            self.store,
            self.settings,
            client_factory=lambda: FakeClient(make_info(True)),
            recorder_factory=recorder_factory,
        )

        await scheduler.start(task.id)
        result = await wait_for_status(self.store, task.id, TaskStatus.STOPPED)

        self.assertFalse(result.enabled)
        self.assertFalse(task.monitor)
        self.assertEqual(result.output_path, "/data/recordings/test_%03d.ts")
        self.assertIn("4 段", result.status_message)
        self.assertEqual(options_seen[0].segment_count, 4)
        await scheduler.shutdown()

    async def test_natural_stream_end_before_limit_stops_task(self) -> None:
        task = await self.store.create(make_config(monitor=True, segment_seconds=1800, segment_count=4))
        scheduler = TaskScheduler(
            self.store,
            self.settings,
            client_factory=lambda: FakeClient(make_info(True)),
            recorder_factory=FakeRecorder,
        )

        await scheduler.start(task.id)
        result = await wait_for_status(self.store, task.id, TaskStatus.STOPPED)

        self.assertFalse(result.enabled)
        self.assertIn("未满 4 段", result.status_message)
        await scheduler.shutdown()

    async def test_shutdown_keeps_monitor_enabled_for_restart(self) -> None:
        task = await self.store.create(make_config(monitor=True))
        scheduler = TaskScheduler(
            self.store,
            self.settings,
            client_factory=lambda: FakeClient(make_info(False)),
        )

        await scheduler.start(task.id)
        await asyncio.sleep(0)  # Let the worker enter its cancellation handler before shutdown.
        await wait_for_status(self.store, task.id, TaskStatus.WAITING)
        await scheduler.shutdown()
        result = await self.store.get(task.id)

        self.assertTrue(result.enabled)
        self.assertEqual(result.status, TaskStatus.WAITING)
        self.assertIn("恢复", result.status_message)

    async def test_enrich_record_exposes_current_segment_progress(self) -> None:
        task = await self.store.create(make_config(monitor=False, segment_seconds=1800, segment_count=4))
        scheduler = TaskScheduler(self.store, self.settings)
        record = await self.store.update_runtime(
            task.id,
            status=TaskStatus.RECORDING,
            status_message="正在录制",
        )
        assert record is not None

        class LiveRecorder:
            progress_seconds = 1850.0
            current_output_path = None

        scheduler._recorders[task.id] = LiveRecorder()
        enriched = scheduler.enrich_record(record)

        self.assertEqual(enriched.recording_elapsed_seconds, 1850.0)
        self.assertEqual(enriched.recording_segment_index, 2)
        self.assertAlmostEqual(enriched.recording_segment_progress or 0.0, 50 / 1800, places=4)
        await scheduler.shutdown()

    async def test_recording_lifecycle_is_written_to_the_activity_log(self) -> None:
        task = await self.store.create(make_config(monitor=False))
        scheduler = TaskScheduler(
            self.store,
            self.settings,
            client_factory=lambda: FakeClient(make_info(True)),
            recorder_factory=FakeRecorder,
        )

        await scheduler.start(task.id)
        await wait_for_status(self.store, task.id, TaskStatus.STOPPED)
        await scheduler.shutdown()
        messages = [event.message for event in await self.store.list_events(limit=20)]

        self.assertIn("「测试主播」录制完成，单次任务已结束", messages)
        self.assertIn("「测试主播」已开播，开始录制", messages)
        self.assertIn(f"「{task.url}」已启动", messages)
        # Nothing but the three milestones: a poll loop must not flood the page.
        self.assertEqual(len(messages), 3)

    async def test_repeated_check_failures_report_once_and_then_recovery(self) -> None:
        """A room that keeps failing must not fill the page with one entry per retry."""
        task = await self.store.create(make_config(monitor=True))
        scheduler = TaskScheduler(self.store, self.settings)
        record = await self.store.get(task.id)
        assert record is not None

        failure = RuntimeError("抖音接口返回空数据")
        with patch.object(scheduler_module.asyncio, "sleep", AsyncMock()):
            self.assertTrue(await scheduler._record_failure(record, failure, stage="检查开播状态"))
            self.assertTrue(await scheduler._record_failure(record, failure, stage="检查开播状态"))
        await scheduler._clear_failure(record)

        events = [(event.level, event.message, event.detail) for event in await self.store.list_events(limit=20)]
        self.assertEqual(
            events,
            [
                ("success", f"「{task.url}」已恢复正常", None),
                ("error", f"「{task.url}」检查开播状态失败，将每 10 秒重试", "抖音接口返回空数据"),
            ],
        )

    async def test_restart_reports_the_config_change_once(self) -> None:
        task = await self.store.create(make_config(monitor=True))
        await self.store.update_runtime(task.id, enabled=True, anchor_name="测试主播")
        scheduler = TaskScheduler(
            self.store,
            self.settings,
            client_factory=lambda: FakeClient(make_info(False)),
        )

        await scheduler.restart(task.id)
        await scheduler.shutdown()
        messages = [event.message for event in await self.store.list_events(limit=20)]

        # Restarting for a config change is one entry, not a stop plus a start.
        self.assertEqual(messages, ["「测试主播」配置已更新，正在按新配置重新录制"])
