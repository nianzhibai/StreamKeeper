import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase

from douyin_recorder.models import LiveInfo, RecordingResult, SelectedSource
from douyin_recorder.settings import Settings
from douyin_recorder.web.scheduler import TaskScheduler
from douyin_recorder.web.schemas import TaskConfig, TaskStatus
from douyin_recorder.web.store import TaskStore


def make_settings(root: Path) -> Settings:
    return Settings(
        data_dir=root,
        recordings_dir=root / "recordings",
        database_path=root / "tasks.db",
        web_password="test-password",
        validate_binaries=False,
    )


def make_config(*, monitor: bool = False) -> TaskConfig:
    return TaskConfig(
        url="https://live.douyin.com/123456789",
        quality="OD",
        output_format="ts",
        source="auto",
        segment_seconds=0,
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

    async def test_shutdown_keeps_monitor_enabled_for_restart(self) -> None:
        task = await self.store.create(make_config(monitor=True))
        scheduler = TaskScheduler(
            self.store,
            self.settings,
            client_factory=lambda: FakeClient(make_info(False)),
        )

        await scheduler.start(task.id)
        await wait_for_status(self.store, task.id, TaskStatus.WAITING)
        await scheduler.shutdown()
        result = await self.store.get(task.id)

        self.assertTrue(result.enabled)
        self.assertEqual(result.status, TaskStatus.WAITING)
        self.assertIn("恢复", result.status_message)
