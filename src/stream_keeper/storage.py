from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from .errors import InsufficientDiskSpaceError

MIN_FREE_SPACE_BYTES = 1024**3
DISK_SPACE_CHECK_INTERVAL_SECONDS = 1.0


def _existing_path(path: Path) -> Path:
    current = path.expanduser().resolve(strict=False)
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def free_space_bytes(path: Path) -> int:
    return shutil.disk_usage(_existing_path(path)).free


def ensure_disk_reserve(path: Path, *, required_bytes: int = 0) -> int:
    """Require enough capacity for a write while preserving the 1 GiB reserve."""
    free = free_space_bytes(path)
    required = max(0, required_bytes)
    if free - required <= MIN_FREE_SPACE_BYTES:
        raise InsufficientDiskSpaceError(
            f"磁盘可用空间不足，必须至少保留 {MIN_FREE_SPACE_BYTES // 1024**3} GB"
        )
    return free


async def wait_until_disk_reserve_reached(path: Path) -> int:
    """Return once an active writer has consumed the configured reserve."""
    while True:
        await asyncio.sleep(DISK_SPACE_CHECK_INTERVAL_SECONDS)
        free = await asyncio.to_thread(free_space_bytes, path)
        if free <= MIN_FREE_SPACE_BYTES:
            return free
