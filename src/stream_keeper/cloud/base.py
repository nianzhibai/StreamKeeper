from __future__ import annotations

import posixpath
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol


class CloudUploadError(RuntimeError):
    """A cloud API rejected an operation or returned an invalid response."""


CredentialUpdate = Callable[[dict[str, str]], Awaitable[None]]

UploadStage = Literal["preparing", "uploading", "verifying"]
# Called with the current stage and the absolute number of bytes handed to the
# network for this file. Streaming runs slightly ahead of what the remote has
# acknowledged, and a retried part rewinds the count to the part boundary.
UploadProgress = Callable[[UploadStage, int], None]


class CloudUploadClient(Protocol):
    async def upload_verified(
        self,
        local_path: Path,
        remote_path: str,
        *,
        progress: UploadProgress | None = None,
    ) -> bool: ...

    async def aclose(self) -> None: ...


@dataclass(frozen=True, slots=True)
class RemoteEntry:
    id: str
    name: str
    size: int
    is_directory: bool


def split_remote_file(remote_path: str) -> tuple[list[str], str]:
    """Split a normalized absolute POSIX path into directory parts and filename."""

    normalized = posixpath.normpath(remote_path)
    if not remote_path.startswith("/") or normalized != remote_path or remote_path == "/":
        raise CloudUploadError(f"无效的远端文件路径：{remote_path}")
    parts = [part for part in remote_path.split("/") if part]
    if not parts or any(part in {".", ".."} or any(ord(char) < 32 for char in part) for part in parts):
        raise CloudUploadError(f"无效的远端文件路径：{remote_path}")
    return parts[:-1], parts[-1]
