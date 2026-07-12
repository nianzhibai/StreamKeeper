from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from .schemas import TaskConfig, TaskRecord, TaskStatus

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

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

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
                    segment_count INTEGER NOT NULL DEFAULT 4,
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
            connection.execute("DELETE FROM web_sessions WHERE expires_at <= ?", (utc_now().isoformat(),))

    @staticmethod
    def _decode_json_object(value: str) -> dict[str, Any] | None:
        try:
            decoded = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return None
        return decoded if isinstance(decoded, dict) else None

    async def sync_cloud_upload_config(
        self,
        source_fingerprint: str,
        defaults: dict[str, object],
    ) -> dict[str, Any]:
        """Seed from environment until a Web save makes SQLite authoritative."""

        return await asyncio.to_thread(
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
        return await asyncio.to_thread(self._get_cloud_upload_config_sync)

    def _get_cloud_upload_config_sync(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT config_json FROM cloud_upload_config WHERE id = 1").fetchone()
        return self._decode_json_object(row["config_json"]) if row is not None else None

    async def save_cloud_upload_config(self, config: dict[str, object]) -> None:
        await asyncio.to_thread(self._save_cloud_upload_config_sync, config)

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
        if provider not in {"quark", "wopan"}:
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
        return await asyncio.to_thread(
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
        await asyncio.to_thread(
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
        return await asyncio.to_thread(self._delete_cloud_credentials_sync, provider)

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

    async def sync_web_credentials(self, username: str, password: str) -> bool:
        """Persist a slow credential fingerprint and revoke sessions when it changes."""

        return await asyncio.to_thread(self._sync_web_credentials_sync, username, password)

    def _sync_web_credentials_sync(self, username: str, password: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT username, password_salt, password_digest FROM web_auth_state WHERE id = 1"
            ).fetchone()
            credentials_match = False
            if row is not None:
                username_matches = secrets.compare_digest(
                    row["username"].encode("utf-8"),
                    username.encode("utf-8"),
                )
                password_matches = secrets.compare_digest(
                    row["password_digest"],
                    self._credential_digest(password, row["password_salt"]),
                )
                credentials_match = username_matches and password_matches
            if credentials_match:
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
        return await asyncio.to_thread(self._is_login_blacklisted_sync, client_key)

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
        return await asyncio.to_thread(
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
        return await asyncio.to_thread(self._accept_login_success_sync, client_key)

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
        return await asyncio.to_thread(self._list_login_blacklist_sync)

    def _list_login_blacklist_sync(self) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT client_key FROM web_login_blacklist ORDER BY blacklisted_at DESC"
            ).fetchall()
        return [row["client_key"] for row in rows]

    async def unblock_login_client(self, client_key: str) -> bool:
        return await asyncio.to_thread(self._unblock_login_client_sync, client_key)

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
        return await asyncio.to_thread(self._create_sync, config)

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
        return await asyncio.to_thread(self._get_sync, task_id)

    def _get_sync(self, task_id: str) -> TaskRecord | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM recording_tasks WHERE id = ?", (task_id,)).fetchone()
        return self._to_record(row)

    async def list(self) -> list[TaskRecord]:
        return await asyncio.to_thread(self._list_sync)

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
        return await asyncio.to_thread(self._update_sync, task_id, changes)

    async def update_runtime(self, task_id: str, **changes: Any) -> TaskRecord | None:
        invalid = set(changes) - RUNTIME_COLUMNS
        if invalid:
            raise ValueError(f"Invalid runtime fields: {', '.join(sorted(invalid))}")
        return await asyncio.to_thread(self._update_sync, task_id, changes)

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
        return await asyncio.to_thread(self._delete_sync, task_id)

    def _delete_sync(self, task_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM recording_tasks WHERE id = ?", (task_id,))
        return cursor.rowcount > 0

    async def recover_interrupted(self) -> None:
        await asyncio.to_thread(self._recover_interrupted_sync)

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

    @staticmethod
    def _session_token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    async def create_session(self, username: str, ttl_seconds: int) -> tuple[str, WebSession]:
        return await asyncio.to_thread(self._create_session_sync, username, ttl_seconds)

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
        return await asyncio.to_thread(self._get_session_sync, token)

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
        return await asyncio.to_thread(
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
        return await asyncio.to_thread(self._delete_session_sync, token)

    def _delete_session_sync(self, token: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM web_sessions WHERE token_hash = ?",
                (self._session_token_hash(token),),
            )
        return cursor.rowcount > 0
