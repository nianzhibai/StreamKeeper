from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import sqlite3
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from ..cloud.config import CLOUD_PROVIDER_ORDER
from ..settings import MAX_RECORDING_CONCURRENCY, WEB_SETUP_PASSWORD
from .schemas import (
    EventCategory,
    EventLevel,
    RecordingDefaults,
    RecordingRuntimeSettings,
    RuntimeEventFacetsView,
    RuntimeEventSummaryView,
    RuntimeEventView,
    TaskConfig,
    TaskRecord,
    TaskStatus,
)

CONFIG_COLUMNS = {
    "url",
    "label",
    "quality",
    "output_format",
    "source",
    "segment_seconds",
    "segment_count",
    "monitor",
    "interval_seconds",
}
RUNTIME_COLUMNS = {
    "enabled",
    "status",
    "status_message",
    "anchor_name",
    "live_title",
    "is_live",
    "output_path",
    "last_checked_at",
    "started_at",
}
_AUTH_KDF_N = 2**14
_AUTH_KDF_R = 8
_AUTH_KDF_P = 1
_AUTH_KDF_LENGTH = 32


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class WebSession:
    username: str
    csrf_token: str
    created_at: datetime
    expires_at: datetime


class TaskStore:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    async def _run_sync(self, func: Callable[..., Any], /, *args: Any) -> Any:
        """to_thread, except cancellation waits for the thread instead of
        abandoning it.

        Workers are cancelled routinely (stop/restart/shutdown, client
        disconnects). A plain to_thread lets the awaiting task move on while
        the thread keeps writing: the caller's follow-up writes then race the
        abandoned one for the final row state, and on Windows the still-open
        connection holds the database file locked past the caller's cleanup.
        """
        inner = asyncio.ensure_future(asyncio.to_thread(func, *args))
        try:
            return await asyncio.shield(inner)
        except asyncio.CancelledError:
            if not inner.done():
                await asyncio.wait([inner])
            if not inner.cancelled():
                inner.exception()  # Superseded by the cancellation; keep the loop quiet.
            raise

    async def initialize(self) -> None:
        await self._run_sync(self._initialize_sync)

    def _initialize_sync(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS recording_tasks (
                    id TEXT PRIMARY KEY,
                    url TEXT NOT NULL,
                    label TEXT,
                    quality TEXT NOT NULL,
                    output_format TEXT NOT NULL,
                    source TEXT NOT NULL,
                    segment_seconds INTEGER NOT NULL,
                    segment_count INTEGER NOT NULL DEFAULT 0,
                    monitor INTEGER NOT NULL,
                    interval_seconds INTEGER NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    status_message TEXT,
                    anchor_name TEXT,
                    live_title TEXT,
                    is_live INTEGER NOT NULL DEFAULT 0,
                    output_path TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_checked_at TEXT,
                    started_at TEXT
                )
                """
            )
            task_columns = {row["name"] for row in connection.execute("PRAGMA table_info(recording_tasks)").fetchall()}
            if "segment_count" not in task_columns:
                connection.execute("ALTER TABLE recording_tasks ADD COLUMN segment_count INTEGER NOT NULL DEFAULT 0")
            # Earlier releases allowed the contradictory "finite cap +
            # continuous monitoring" combination.  Normalize persisted rows
            # before the scheduler restores enabled tasks at startup.
            connection.execute(
                "UPDATE recording_tasks SET monitor = 0, updated_at = ? WHERE segment_count > 0 AND monitor <> 0",
                (utc_now().isoformat(),),
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS recording_defaults (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    output_format TEXT NOT NULL CHECK (output_format IN ('ts', 'mp4', 'mkv', 'flv')),
                    segment_seconds INTEGER NOT NULL CHECK (segment_seconds BETWEEN 0 AND 86400),
                    segment_count INTEGER NOT NULL CHECK (segment_count BETWEEN 0 AND 10000),
                    updated_at TEXT NOT NULL,
                    CHECK (segment_count = 0 OR segment_seconds > 0)
                )
                """
            )
            recording_defaults = RecordingDefaults()
            connection.execute(
                """
                INSERT OR IGNORE INTO recording_defaults (
                    id, output_format, segment_seconds, segment_count, updated_at
                ) VALUES (1, ?, ?, ?, ?)
                """,
                (
                    recording_defaults.output_format,
                    recording_defaults.segment_seconds,
                    recording_defaults.segment_count,
                    utc_now().isoformat(),
                ),
            )
            connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS recording_runtime_settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    max_concurrent_recordings INTEGER NOT NULL
                        CHECK (max_concurrent_recordings BETWEEN 1 AND {MAX_RECORDING_CONCURRENCY}),
                    source_max_concurrent_recordings INTEGER
                        CHECK (
                            source_max_concurrent_recordings IS NULL
                            OR source_max_concurrent_recordings BETWEEN 1 AND {MAX_RECORDING_CONCURRENCY}
                        ),
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS web_sessions (
                    token_hash TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    csrf_token TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                )
                """
            )
            connection.execute("CREATE INDEX IF NOT EXISTS idx_web_sessions_expires_at ON web_sessions (expires_at)")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS web_auth_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    username TEXT NOT NULL,
                    password_salt BLOB NOT NULL,
                    password_digest BLOB NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS web_login_failures (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_key TEXT NOT NULL,
                    attempted_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_web_login_failures_client_time
                ON web_login_failures (client_key, attempted_at)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS web_login_blacklist (
                    client_key TEXT PRIMARY KEY,
                    blacklisted_at TEXT NOT NULL,
                    failure_count INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS cloud_credentials (
                    provider TEXT PRIMARY KEY,
                    source_fingerprint TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS cloud_upload_config (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    source_fingerprint TEXT,
                    config_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS runtime_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    category TEXT NOT NULL,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL,
                    detail TEXT
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_runtime_events_created_at ON runtime_events (created_at)"
            )
            event_columns = {row["name"] for row in connection.execute("PRAGMA table_info(runtime_events)").fetchall()}
            if "task_id" not in event_columns:
                connection.execute("ALTER TABLE runtime_events ADD COLUMN task_id TEXT")
            # Filtering the log down to one recording is the common drill-down, and
            # level/category drive the facet counts on every page load.
            connection.execute("CREATE INDEX IF NOT EXISTS idx_runtime_events_task_id ON runtime_events (task_id)")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_runtime_events_level_category ON runtime_events (level, category)"
            )
            connection.execute("DELETE FROM web_sessions WHERE expires_at <= ?", (utc_now().isoformat(),))

    @staticmethod
    def _decode_json_object(value: str) -> dict[str, Any] | None:
        try:
            decoded = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return None
        return decoded if isinstance(decoded, dict) else None

    async def get_recording_defaults(self) -> RecordingDefaults:
        return await self._run_sync(self._get_recording_defaults_sync)

    def _get_recording_defaults_sync(self) -> RecordingDefaults:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT output_format, segment_seconds, segment_count FROM recording_defaults WHERE id = 1"
            ).fetchone()
        return RecordingDefaults.model_validate(dict(row)) if row is not None else RecordingDefaults()

    async def save_recording_defaults(self, defaults: RecordingDefaults) -> None:
        await self._run_sync(self._save_recording_defaults_sync, defaults)

    def _save_recording_defaults_sync(self, defaults: RecordingDefaults) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO recording_defaults (id, output_format, segment_seconds, segment_count, updated_at)
                VALUES (1, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    output_format = excluded.output_format,
                    segment_seconds = excluded.segment_seconds,
                    segment_count = excluded.segment_count,
                    updated_at = excluded.updated_at
                """,
                (
                    defaults.output_format,
                    defaults.segment_seconds,
                    defaults.segment_count,
                    utc_now().isoformat(),
                ),
            )

    async def sync_recording_runtime_settings(
        self,
        defaults: RecordingRuntimeSettings,
    ) -> RecordingRuntimeSettings:
        """Seed runtime capacity from the environment until the Web UI takes ownership."""

        return await self._run_sync(self._sync_recording_runtime_settings_sync, defaults)

    def _sync_recording_runtime_settings_sync(
        self,
        defaults: RecordingRuntimeSettings,
    ) -> RecordingRuntimeSettings:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT max_concurrent_recordings, source_max_concurrent_recordings
                FROM recording_runtime_settings
                WHERE id = 1
                """
            ).fetchone()
            if row is not None:
                web_managed = row["source_max_concurrent_recordings"] is None
                source_matches = row["source_max_concurrent_recordings"] == defaults.max_concurrent_recordings
                if web_managed or source_matches:
                    return RecordingRuntimeSettings(
                        max_concurrent_recordings=row["max_concurrent_recordings"]
                    )

            connection.execute(
                """
                INSERT INTO recording_runtime_settings (
                    id, max_concurrent_recordings, source_max_concurrent_recordings, updated_at
                ) VALUES (1, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    max_concurrent_recordings = excluded.max_concurrent_recordings,
                    source_max_concurrent_recordings = excluded.source_max_concurrent_recordings,
                    updated_at = excluded.updated_at
                """,
                (
                    defaults.max_concurrent_recordings,
                    defaults.max_concurrent_recordings,
                    utc_now().isoformat(),
                ),
            )
        return defaults

    async def get_recording_runtime_settings(self) -> RecordingRuntimeSettings:
        return await self._run_sync(self._get_recording_runtime_settings_sync)

    def _get_recording_runtime_settings_sync(self) -> RecordingRuntimeSettings:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT max_concurrent_recordings
                FROM recording_runtime_settings
                WHERE id = 1
                """
            ).fetchone()
        if row is None:
            return RecordingRuntimeSettings()
        return RecordingRuntimeSettings(max_concurrent_recordings=row["max_concurrent_recordings"])

    async def save_recording_runtime_settings(self, settings: RecordingRuntimeSettings) -> None:
        await self._run_sync(self._save_recording_runtime_settings_sync, settings)

    def _save_recording_runtime_settings_sync(self, settings: RecordingRuntimeSettings) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO recording_runtime_settings (
                    id, max_concurrent_recordings, source_max_concurrent_recordings, updated_at
                ) VALUES (1, ?, NULL, ?)
                ON CONFLICT(id) DO UPDATE SET
                    max_concurrent_recordings = excluded.max_concurrent_recordings,
                    source_max_concurrent_recordings = NULL,
                    updated_at = excluded.updated_at
                """,
                (settings.max_concurrent_recordings, utc_now().isoformat()),
            )

    async def sync_cloud_upload_config(
        self,
        source_fingerprint: str,
        defaults: dict[str, object],
    ) -> dict[str, Any]:
        """Seed from environment until a Web save makes SQLite authoritative."""

        return await self._run_sync(
            self._sync_cloud_upload_config_sync,
            source_fingerprint,
            defaults,
        )

    def _sync_cloud_upload_config_sync(
        self,
        source_fingerprint: str,
        defaults: dict[str, object],
    ) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT source_fingerprint, config_json FROM cloud_upload_config WHERE id = 1"
            ).fetchone()
            if row is not None:
                stored = self._decode_json_object(row["config_json"])
                web_managed = row["source_fingerprint"] is None
                source_matches = not web_managed and secrets.compare_digest(
                    row["source_fingerprint"],
                    source_fingerprint,
                )
                if stored is not None and (web_managed or source_matches):
                    return stored

            config_json = json.dumps(defaults, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            connection.execute(
                """
                INSERT INTO cloud_upload_config (id, source_fingerprint, config_json, updated_at)
                VALUES (1, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    source_fingerprint = excluded.source_fingerprint,
                    config_json = excluded.config_json,
                    updated_at = excluded.updated_at
                """,
                (source_fingerprint, config_json, utc_now().isoformat()),
            )
        return dict(defaults)

    async def get_cloud_upload_config(self) -> dict[str, Any] | None:
        return await self._run_sync(self._get_cloud_upload_config_sync)

    def _get_cloud_upload_config_sync(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT config_json FROM cloud_upload_config WHERE id = 1").fetchone()
        return self._decode_json_object(row["config_json"]) if row is not None else None

    async def save_cloud_upload_config(self, config: dict[str, object]) -> None:
        await self._run_sync(self._save_cloud_upload_config_sync, config)

    def _save_cloud_upload_config_sync(self, config: dict[str, object]) -> None:
        config_json = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO cloud_upload_config (id, source_fingerprint, config_json, updated_at)
                VALUES (1, NULL, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    source_fingerprint = NULL,
                    config_json = excluded.config_json,
                    updated_at = excluded.updated_at
                """,
                (config_json, utc_now().isoformat()),
            )

    @staticmethod
    def _validate_cloud_state(provider: str, state: dict[str, str]) -> None:
        if provider not in CLOUD_PROVIDER_ORDER:
            raise ValueError(f"不支持的网盘凭据类型: {provider}")
        if not all(isinstance(key, str) and isinstance(value, str) for key, value in state.items()):
            raise ValueError("网盘凭据必须是字符串字典")

    async def resolve_cloud_credentials(
        self,
        provider: str,
        source_fingerprint: str,
        defaults: dict[str, str],
    ) -> dict[str, str]:
        """Use refreshed credentials until the source environment values change."""

        self._validate_cloud_state(provider, defaults)
        return await self._run_sync(
            self._resolve_cloud_credentials_sync,
            provider,
            source_fingerprint,
            defaults,
        )

    def _resolve_cloud_credentials_sync(
        self,
        provider: str,
        source_fingerprint: str,
        defaults: dict[str, str],
    ) -> dict[str, str]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT source_fingerprint, state_json FROM cloud_credentials WHERE provider = ?",
                (provider,),
            ).fetchone()
            if row is not None and secrets.compare_digest(row["source_fingerprint"], source_fingerprint):
                try:
                    state = json.loads(row["state_json"])
                except (TypeError, json.JSONDecodeError):
                    state = None
                if isinstance(state, dict) and all(
                    isinstance(key, str) and isinstance(value, str) for key, value in state.items()
                ):
                    return state
            state_json = json.dumps(defaults, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            connection.execute(
                """
                INSERT INTO cloud_credentials (provider, source_fingerprint, state_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(provider) DO UPDATE SET
                    source_fingerprint = excluded.source_fingerprint,
                    state_json = excluded.state_json,
                    updated_at = excluded.updated_at
                """,
                (provider, source_fingerprint, state_json, utc_now().isoformat()),
            )
        return dict(defaults)

    async def save_cloud_credentials(
        self,
        provider: str,
        source_fingerprint: str,
        state: dict[str, str],
    ) -> None:
        self._validate_cloud_state(provider, state)
        await self._run_sync(
            self._save_cloud_credentials_sync,
            provider,
            source_fingerprint,
            state,
        )

    def _save_cloud_credentials_sync(
        self,
        provider: str,
        source_fingerprint: str,
        state: dict[str, str],
    ) -> None:
        state_json = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO cloud_credentials (provider, source_fingerprint, state_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(provider) DO UPDATE SET
                    source_fingerprint = excluded.source_fingerprint,
                    state_json = excluded.state_json,
                    updated_at = excluded.updated_at
                """,
                (provider, source_fingerprint, state_json, utc_now().isoformat()),
            )

    async def delete_cloud_credentials(self, provider: str) -> bool:
        self._validate_cloud_state(provider, {})
        return await self._run_sync(self._delete_cloud_credentials_sync, provider)

    def _delete_cloud_credentials_sync(self, provider: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM cloud_credentials WHERE provider = ?", (provider,))
        return cursor.rowcount > 0

    @staticmethod
    def _credential_digest(password: str, salt: bytes) -> bytes:
        return hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=_AUTH_KDF_N,
            r=_AUTH_KDF_R,
            p=_AUTH_KDF_P,
            dklen=_AUTH_KDF_LENGTH,
        )

    @classmethod
    def _stored_credentials_match(cls, row: sqlite3.Row | None, username: str, password: str) -> bool:
        # Perform the expensive digest even when no account exists so login
        # timing does not expose whether first-run setup has completed.
        salt = row["password_salt"] if row is not None else bytes(16)
        expected_digest = row["password_digest"] if row is not None else bytes(_AUTH_KDF_LENGTH)
        stored_username = row["username"] if row is not None else ""
        username_matches = secrets.compare_digest(
            stored_username.encode("utf-8"),
            username.encode("utf-8"),
        )
        password_matches = secrets.compare_digest(
            expected_digest,
            cls._credential_digest(password, salt),
        )
        return row is not None and username_matches and password_matches

    async def web_credentials_configured(self) -> bool:
        return await self._run_sync(self._web_credentials_configured_sync)

    def _web_credentials_configured_sync(self) -> bool:
        with self._connect() as connection:
            row = connection.execute("SELECT 1 FROM web_auth_state WHERE id = 1").fetchone()
        return row is not None

    async def verify_web_credentials(self, username: str, password: str) -> bool:
        return await self._run_sync(self._verify_web_credentials_sync, username, password)

    def _verify_web_credentials_sync(self, username: str, password: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT username, password_salt, password_digest FROM web_auth_state WHERE id = 1"
            ).fetchone()
        return self._stored_credentials_match(row, username, password)

    async def initialize_web_credentials(self, username: str, password: str) -> bool:
        """Create the first Web account exactly once and clear pre-setup login state."""

        normalized_username = username.strip()
        if not normalized_username:
            raise ValueError("用户名不能为空")
        if len(password) < 10:
            raise ValueError("密码至少需要 10 个字符")
        if password == WEB_SETUP_PASSWORD:
            raise ValueError("不能使用默认占位密码")
        return await self._run_sync(self._initialize_web_credentials_sync, normalized_username, password)

    def _initialize_web_credentials_sync(self, username: str, password: str) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM web_auth_state WHERE id = 1").fetchone() is not None:
                return False
            salt = secrets.token_bytes(16)
            connection.execute(
                """
                INSERT INTO web_auth_state (id, username, password_salt, password_digest, updated_at)
                VALUES (1, ?, ?, ?, ?)
                """,
                (username, salt, self._credential_digest(password, salt), utc_now().isoformat()),
            )
            connection.execute("DELETE FROM web_sessions")
            connection.execute("DELETE FROM web_login_failures")
            connection.execute("DELETE FROM web_login_blacklist")
        return True

    async def discard_web_credentials_if_match(self, username: str, password: str) -> bool:
        """Remove a legacy placeholder account without touching real persisted credentials."""

        return await self._run_sync(self._discard_web_credentials_if_match_sync, username, password)

    def _discard_web_credentials_if_match_sync(self, username: str, password: str) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT username, password_salt, password_digest FROM web_auth_state WHERE id = 1"
            ).fetchone()
            if not self._stored_credentials_match(row, username, password):
                return False
            connection.execute("DELETE FROM web_auth_state WHERE id = 1")
            connection.execute("DELETE FROM web_sessions")
            connection.execute("DELETE FROM web_login_failures")
            connection.execute("DELETE FROM web_login_blacklist")
        return True

    async def sync_web_credentials(self, username: str, password: str) -> bool:
        """Persist environment-managed credentials and revoke sessions when they change."""

        return await self._run_sync(self._sync_web_credentials_sync, username, password)

    def _sync_web_credentials_sync(self, username: str, password: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT username, password_salt, password_digest FROM web_auth_state WHERE id = 1"
            ).fetchone()
            if self._stored_credentials_match(row, username, password):
                return False

            salt = secrets.token_bytes(16)
            digest = self._credential_digest(password, salt)
            connection.execute(
                """
                INSERT INTO web_auth_state (id, username, password_salt, password_digest, updated_at)
                VALUES (1, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    username = excluded.username,
                    password_salt = excluded.password_salt,
                    password_digest = excluded.password_digest,
                    updated_at = excluded.updated_at
                """,
                (username, salt, digest, utc_now().isoformat()),
            )
            connection.execute("DELETE FROM web_sessions")
        return True

    async def is_login_blacklisted(self, client_key: str) -> bool:
        return await self._run_sync(self._is_login_blacklisted_sync, client_key)

    def _is_login_blacklisted_sync(self, client_key: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM web_login_blacklist WHERE client_key = ?",
                (client_key,),
            ).fetchone()
        return row is not None

    async def register_login_failure(
        self,
        client_key: str,
        max_attempts: int,
        window_seconds: int,
    ) -> bool:
        return await self._run_sync(
            self._register_login_failure_sync,
            client_key,
            max_attempts,
            window_seconds,
        )

    def _register_login_failure_sync(
        self,
        client_key: str,
        max_attempts: int,
        window_seconds: int,
    ) -> bool:
        if max_attempts < 1:
            raise ValueError("max_attempts 必须大于 0")
        if window_seconds < 1:
            raise ValueError("window_seconds 必须大于 0")

        now = utc_now()
        cutoff = now - timedelta(seconds=window_seconds)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            blocked = connection.execute(
                "SELECT 1 FROM web_login_blacklist WHERE client_key = ?",
                (client_key,),
            ).fetchone()
            if blocked is not None:
                return True

            connection.execute("DELETE FROM web_login_failures WHERE attempted_at <= ?", (cutoff.isoformat(),))
            connection.execute(
                "INSERT INTO web_login_failures (client_key, attempted_at) VALUES (?, ?)",
                (client_key, now.isoformat()),
            )
            failure_count = connection.execute(
                "SELECT COUNT(*) FROM web_login_failures WHERE client_key = ? AND attempted_at > ?",
                (client_key, cutoff.isoformat()),
            ).fetchone()[0]
            if failure_count < max_attempts:
                return False

            connection.execute(
                """
                INSERT OR IGNORE INTO web_login_blacklist (client_key, blacklisted_at, failure_count)
                VALUES (?, ?, ?)
                """,
                (client_key, now.isoformat(), failure_count),
            )
            connection.execute("DELETE FROM web_login_failures WHERE client_key = ?", (client_key,))
        return True

    async def accept_login_success(self, client_key: str) -> bool:
        return await self._run_sync(self._accept_login_success_sync, client_key)

    def _accept_login_success_sync(self, client_key: str) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            blocked = connection.execute(
                "SELECT 1 FROM web_login_blacklist WHERE client_key = ?",
                (client_key,),
            ).fetchone()
            if blocked is not None:
                return False
            connection.execute("DELETE FROM web_login_failures WHERE client_key = ?", (client_key,))
        return True

    async def list_login_blacklist(self) -> list[str]:
        return await self._run_sync(self._list_login_blacklist_sync)

    def _list_login_blacklist_sync(self) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT client_key FROM web_login_blacklist ORDER BY blacklisted_at DESC"
            ).fetchall()
        return [row["client_key"] for row in rows]

    async def unblock_login_client(self, client_key: str) -> bool:
        return await self._run_sync(self._unblock_login_client_sync, client_key)

    def _unblock_login_client_sync(self, client_key: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM web_login_blacklist WHERE client_key = ?",
                (client_key,),
            )
            connection.execute("DELETE FROM web_login_failures WHERE client_key = ?", (client_key,))
        return cursor.rowcount > 0

    @staticmethod
    def _serialize(value: Any) -> Any:
        if isinstance(value, Enum):
            return value.value
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, bool):
            return int(value)
        return value

    @staticmethod
    def _to_record(row: sqlite3.Row | None) -> TaskRecord | None:
        if row is None:
            return None
        return TaskRecord.model_validate(dict(row))

    async def create(self, config: TaskConfig) -> TaskRecord:
        return await self._run_sync(self._create_sync, config)

    def _create_sync(self, config: TaskConfig) -> TaskRecord:
        task_id = uuid.uuid4().hex
        now = utc_now().isoformat()
        data = config.model_dump()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO recording_tasks (
                    id, url, label, quality, output_format, source, segment_seconds,
                    segment_count, monitor, interval_seconds, enabled, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
                """,
                (
                    task_id,
                    data["url"],
                    data["label"],
                    data["quality"],
                    data["output_format"],
                    data["source"],
                    data["segment_seconds"],
                    data["segment_count"],
                    int(data["monitor"]),
                    data["interval_seconds"],
                    TaskStatus.STOPPED.value,
                    now,
                    now,
                ),
            )
        record = self._get_sync(task_id)
        assert record is not None
        return record

    async def get(self, task_id: str) -> TaskRecord | None:
        return await self._run_sync(self._get_sync, task_id)

    def _get_sync(self, task_id: str) -> TaskRecord | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM recording_tasks WHERE id = ?", (task_id,)).fetchone()
        return self._to_record(row)

    async def list(self) -> list[TaskRecord]:
        return await self._run_sync(self._list_sync)

    def _list_sync(self) -> list[TaskRecord]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM recording_tasks ORDER BY created_at DESC").fetchall()
        return [record for row in rows if (record := self._to_record(row)) is not None]

    async def list_enabled(self) -> list[TaskRecord]:
        records = await self.list()
        return [record for record in records if record.enabled]

    async def update_config(self, task_id: str, changes: dict[str, Any]) -> TaskRecord | None:
        invalid = set(changes) - CONFIG_COLUMNS
        if invalid:
            raise ValueError(f"Invalid config fields: {', '.join(sorted(invalid))}")
        return await self._run_sync(self._update_sync, task_id, changes)

    async def update_runtime(self, task_id: str, **changes: Any) -> TaskRecord | None:
        invalid = set(changes) - RUNTIME_COLUMNS
        if invalid:
            raise ValueError(f"Invalid runtime fields: {', '.join(sorted(invalid))}")
        return await self._run_sync(self._update_sync, task_id, changes)

    def _update_sync(self, task_id: str, changes: dict[str, Any]) -> TaskRecord | None:
        if not changes:
            return self._get_sync(task_id)
        values = {key: self._serialize(value) for key, value in changes.items()}
        values["updated_at"] = utc_now().isoformat()
        assignments = ", ".join(f"{key} = ?" for key in values)
        with self._connect() as connection:
            connection.execute(
                f"UPDATE recording_tasks SET {assignments} WHERE id = ?",  # Columns are allow-listed above.
                (*values.values(), task_id),
            )
        return self._get_sync(task_id)

    async def delete(self, task_id: str) -> bool:
        return await self._run_sync(self._delete_sync, task_id)

    def _delete_sync(self, task_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM recording_tasks WHERE id = ?", (task_id,))
        return cursor.rowcount > 0

    async def recover_interrupted(self) -> None:
        await self._run_sync(self._recover_interrupted_sync)

    def _recover_interrupted_sync(self) -> None:
        now = utc_now().isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE recording_tasks
                SET status = CASE WHEN enabled = 1 THEN ? ELSE ? END,
                    status_message = CASE WHEN enabled = 1 THEN ? ELSE status_message END,
                    is_live = 0,
                    updated_at = ?
                WHERE status IN (?, ?, ?)
                """,
                (
                    TaskStatus.WAITING.value,
                    TaskStatus.STOPPED.value,
                    "服务重启，任务等待恢复",
                    now,
                    TaskStatus.CHECKING.value,
                    TaskStatus.QUEUED.value,
                    TaskStatus.RECORDING.value,
                ),
            )

    async def append_event(
        self,
        category: EventCategory,
        level: EventLevel,
        message: str,
        detail: str | None = None,
        *,
        retention: int,
        task_id: str | None = None,
    ) -> None:
        await self._run_sync(self._append_event_sync, category, level, message, detail, retention, task_id)

    def _append_event_sync(
        self,
        category: str,
        level: str,
        message: str,
        detail: str | None,
        retention: int,
        task_id: str | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO runtime_events (created_at, category, level, message, detail, task_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (utc_now().isoformat(), category, level, message, detail, task_id),
            )
            # Trimming on write keeps the log bounded without a separate cleanup job.
            connection.execute(
                "DELETE FROM runtime_events WHERE id <= (SELECT MAX(id) FROM runtime_events) - ?",
                (retention,),
            )

    @staticmethod
    def _event_filters(
        *,
        categories: Sequence[str] | None,
        levels: Sequence[str] | None,
        search: str | None,
        task_id: str | None,
        before_id: int | None,
        after_id: int | None,
    ) -> tuple[str, list[Any]]:
        """Builds the shared WHERE clause for listing, counting and exporting.

        Only placeholders derived from the caller's list lengths are interpolated;
        every value stays bound.
        """
        conditions: list[str] = []
        params: list[Any] = []
        if categories:
            conditions.append(f"category IN ({', '.join('?' * len(categories))})")
            params.extend(categories)
        if levels:
            conditions.append(f"level IN ({', '.join('?' * len(levels))})")
            params.extend(levels)
        if task_id:
            conditions.append("task_id = ?")
            params.append(task_id)
        if search:
            # LIKE wildcards in the user's text are escaped so a literal % or _
            # searches for itself instead of matching everything.
            pattern = f"%{search.replace('!', '!!').replace('%', '!%').replace('_', '!_')}%"
            conditions.append("(message LIKE ? ESCAPE '!' OR detail LIKE ? ESCAPE '!')")
            params.extend((pattern, pattern))
        if before_id is not None:
            conditions.append("id < ?")
            params.append(before_id)
        if after_id is not None:
            conditions.append("id > ?")
            params.append(after_id)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        return where, params

    async def list_events(
        self,
        *,
        limit: int,
        category: EventCategory | None = None,
        alerts_only: bool = False,
        categories: Sequence[EventCategory] | None = None,
        levels: Sequence[EventLevel] | None = None,
        search: str | None = None,
        task_id: str | None = None,
        before_id: int | None = None,
        after_id: int | None = None,
    ) -> list[RuntimeEventView]:
        """Newest first. `category` and `alerts_only` are the older single-value form."""
        selected_categories = list(categories) if categories else ([category] if category else [])
        selected_levels = list(levels) if levels else (["warning", "error"] if alerts_only else [])
        return await self._run_sync(
            self._list_events_sync,
            limit,
            selected_categories,
            selected_levels,
            search,
            task_id,
            before_id,
            after_id,
        )

    def _list_events_sync(
        self,
        limit: int,
        categories: Sequence[str],
        levels: Sequence[str],
        search: str | None,
        task_id: str | None,
        before_id: int | None,
        after_id: int | None,
    ) -> list[RuntimeEventView]:
        where, params = self._event_filters(
            categories=categories,
            levels=levels,
            search=search,
            task_id=task_id,
            before_id=before_id,
            after_id=after_id,
        )
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, created_at, category, level, message, detail, task_id FROM runtime_events "
                f"{where} ORDER BY id DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
        return [RuntimeEventView.model_validate(dict(row)) for row in rows]

    async def event_facets(
        self,
        *,
        search: str | None = None,
        task_id: str | None = None,
    ) -> RuntimeEventFacetsView:
        """Per-level and per-category counts for the filter chips.

        Deliberately ignores the level/category selection so a chip's count does
        not collapse to zero the moment a different chip is picked.
        """
        return await self._run_sync(self._event_facets_sync, search, task_id)

    def _event_facets_sync(self, search: str | None, task_id: str | None) -> RuntimeEventFacetsView:
        where, params = self._event_filters(
            categories=None,
            levels=None,
            search=search,
            task_id=task_id,
            before_id=None,
            after_id=None,
        )
        with self._connect() as connection:
            level_rows = connection.execute(
                f"SELECT level, COUNT(*) AS count FROM runtime_events {where} GROUP BY level",
                params,
            ).fetchall()
            category_rows = connection.execute(
                f"SELECT category, COUNT(*) AS count FROM runtime_events {where} GROUP BY category",
                params,
            ).fetchall()
        return RuntimeEventFacetsView(
            levels={row["level"]: row["count"] for row in level_rows},
            categories={row["category"]: row["count"] for row in category_rows},
            matched=sum(row["count"] for row in level_rows),
        )

    async def clear_events(self) -> int:
        return await self._run_sync(self._clear_events_sync)

    def _clear_events_sync(self) -> int:
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM runtime_events")
        return cursor.rowcount if cursor.rowcount > 0 else 0

    def iter_events(
        self,
        *,
        categories: Sequence[str] | None = None,
        levels: Sequence[str] | None = None,
        search: str | None = None,
        task_id: str | None = None,
        chunk_size: int = 500,
    ) -> Iterator[RuntimeEventView]:
        """Oldest first, in chunks, so an export never holds the whole log in memory."""
        where, params = self._event_filters(
            categories=categories,
            levels=levels,
            search=search,
            task_id=task_id,
            before_id=None,
            after_id=None,
        )
        cursor_id = 0
        joiner = "AND" if where else "WHERE"
        while True:
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT id, created_at, category, level, message, detail, task_id FROM runtime_events "
                    f"{where} {joiner} id > ? ORDER BY id ASC LIMIT ?",
                    (*params, cursor_id, chunk_size),
                ).fetchall()
            if not rows:
                return
            for row in rows:
                yield RuntimeEventView.model_validate(dict(row))
            cursor_id = rows[-1]["id"]

    async def event_summary(self, since: datetime) -> RuntimeEventSummaryView:
        return await self._run_sync(self._event_summary_sync, since)

    def _event_summary_sync(self, since: datetime) -> RuntimeEventSummaryView:
        cutoff = since.isoformat()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM runtime_events) AS total,
                    (SELECT COUNT(*) FROM runtime_events WHERE level = 'error' AND created_at > ?) AS errors,
                    (SELECT COUNT(*) FROM runtime_events WHERE level = 'warning' AND created_at > ?) AS warnings,
                    (SELECT created_at FROM runtime_events ORDER BY id DESC LIMIT 1) AS latest_at,
                    (SELECT id FROM runtime_events ORDER BY id DESC LIMIT 1) AS latest_id,
                    (SELECT created_at FROM runtime_events ORDER BY id ASC LIMIT 1) AS oldest_at
                """,
                (cutoff, cutoff),
            ).fetchone()
        return RuntimeEventSummaryView(
            total=row["total"],
            errors=row["errors"],
            warnings=row["warnings"],
            latest_at=datetime.fromisoformat(row["latest_at"]) if row["latest_at"] else None,
            latest_id=row["latest_id"],
            oldest_at=datetime.fromisoformat(row["oldest_at"]) if row["oldest_at"] else None,
        )

    @staticmethod
    def _session_token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    async def create_session(self, username: str, ttl_seconds: int) -> tuple[str, WebSession]:
        return await self._run_sync(self._create_session_sync, username, ttl_seconds)

    def _create_session_sync(self, username: str, ttl_seconds: int) -> tuple[str, WebSession]:
        token = secrets.token_urlsafe(32)
        now = utc_now()
        session = WebSession(
            username=username,
            csrf_token=secrets.token_urlsafe(24),
            created_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds),
        )
        with self._connect() as connection:
            connection.execute("DELETE FROM web_sessions WHERE expires_at <= ?", (now.isoformat(),))
            connection.execute(
                """
                INSERT INTO web_sessions (token_hash, username, csrf_token, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    self._session_token_hash(token),
                    session.username,
                    session.csrf_token,
                    session.created_at.isoformat(),
                    session.expires_at.isoformat(),
                ),
            )
        return token, session

    async def get_session(self, token: str) -> WebSession | None:
        return await self._run_sync(self._get_session_sync, token)

    def _get_session_sync(self, token: str) -> WebSession | None:
        token_hash = self._session_token_hash(token)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT username, csrf_token, created_at, expires_at FROM web_sessions WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
            if row is None:
                return None
            expires_at = datetime.fromisoformat(row["expires_at"])
            if expires_at <= utc_now():
                connection.execute("DELETE FROM web_sessions WHERE token_hash = ?", (token_hash,))
                return None
        return WebSession(
            username=row["username"],
            csrf_token=row["csrf_token"],
            created_at=datetime.fromisoformat(row["created_at"]),
            expires_at=expires_at,
        )

    async def renew_session_if_needed(
        self,
        token: str,
        ttl_seconds: int,
        renew_before_seconds: int,
    ) -> tuple[WebSession | None, bool]:
        return await self._run_sync(
            self._renew_session_if_needed_sync,
            token,
            ttl_seconds,
            renew_before_seconds,
        )

    def _renew_session_if_needed_sync(
        self,
        token: str,
        ttl_seconds: int,
        renew_before_seconds: int,
    ) -> tuple[WebSession | None, bool]:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds 必须大于 0")
        if renew_before_seconds < 0:
            raise ValueError("renew_before_seconds 不能小于 0")

        token_hash = self._session_token_hash(token)
        now = utc_now()
        renew_before = now + timedelta(seconds=renew_before_seconds)
        renewed_expires_at = now + timedelta(seconds=ttl_seconds)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE web_sessions
                SET expires_at = ?
                WHERE token_hash = ? AND expires_at > ? AND expires_at <= ?
                """,
                (
                    renewed_expires_at.isoformat(),
                    token_hash,
                    now.isoformat(),
                    renew_before.isoformat(),
                ),
            )
            row = connection.execute(
                "SELECT username, csrf_token, created_at, expires_at FROM web_sessions WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
            if row is None:
                return None, False
            expires_at = datetime.fromisoformat(row["expires_at"])
            if expires_at <= now:
                connection.execute("DELETE FROM web_sessions WHERE token_hash = ?", (token_hash,))
                return None, False

        session = WebSession(
            username=row["username"],
            csrf_token=row["csrf_token"],
            created_at=datetime.fromisoformat(row["created_at"]),
            expires_at=expires_at,
        )
        return session, cursor.rowcount > 0

    async def delete_session(self, token: str) -> bool:
        return await self._run_sync(self._delete_session_sync, token)

    def _delete_session_sync(self, token: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM web_sessions WHERE token_hash = ?",
                (self._session_token_hash(token),),
            )
        return cursor.rowcount > 0
