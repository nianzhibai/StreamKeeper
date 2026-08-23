from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch

from stream_keeper.errors import InsufficientDiskSpaceError
from stream_keeper.storage import MIN_FREE_SPACE_BYTES, ensure_disk_reserve, wait_until_disk_reserve_reached


class DiskReserveTests(TestCase):
    def test_reserve_accounts_for_the_planned_write(self) -> None:
        with patch("stream_keeper.storage.free_space_bytes", return_value=MIN_FREE_SPACE_BYTES + 100):
            self.assertEqual(ensure_disk_reserve(Path("missing/cache"), required_bytes=99), MIN_FREE_SPACE_BYTES + 100)
            with self.assertRaisesRegex(InsufficientDiskSpaceError, "至少保留 1 GB"):
                ensure_disk_reserve(Path("missing/cache"), required_bytes=100)

    def test_disk_usage_uses_an_existing_parent(self) -> None:
        with TemporaryDirectory() as tmp:
            missing = Path(tmp) / "future" / "cache"
            with patch("stream_keeper.storage.shutil.disk_usage") as disk_usage:
                disk_usage.return_value.free = MIN_FREE_SPACE_BYTES * 2
                ensure_disk_reserve(missing)
            disk_usage.assert_called_once_with(Path(tmp).resolve())


class ActiveDiskReserveTests(IsolatedAsyncioTestCase):
    async def test_monitor_returns_when_reserve_is_reached(self) -> None:
        with (
            patch("stream_keeper.storage.asyncio.sleep", new=AsyncMock()),
            patch("stream_keeper.storage.free_space_bytes", return_value=MIN_FREE_SPACE_BYTES),
        ):
            self.assertEqual(await wait_until_disk_reserve_reached(Path("recordings")), MIN_FREE_SPACE_BYTES)
