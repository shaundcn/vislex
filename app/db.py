from __future__ import annotations

import json
import secrets
import sqlite3
from collections.abc import Iterable
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import validate_fps
from .defaults import ACTIVE_STATUSES, DEFAULT_PROMPT, TASK_STATUSES


TASK_MUTABLE_FIELDS = {
    "duration_seconds",
    "status",
    "error",
    "prompt_snapshot",
    "model_snapshot",
    "video_fps_snapshot",
    "remote_file_id",
    "response_json",
    "final_stem",
    "video_output_path",
    "md_output_path",
    "video_sha256",
    "md_sha256",
    "started_at",
    "completed_at",
}


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = FULL")
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        statuses = ",".join(f"'{status}'" for status in TASK_STATUSES)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                f"""
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY,
                    signature TEXT NOT NULL UNIQUE,
                    source_path TEXT NOT NULL,
                    original_name TEXT NOT NULL,
                    extension TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    device INTEGER NOT NULL,
                    inode INTEGER NOT NULL,
                    duration_seconds REAL,
                    status TEXT NOT NULL CHECK (status IN ({statuses})),
                    error TEXT,
                    prompt_snapshot TEXT,
                    model_snapshot TEXT,
                    video_fps_snapshot REAL,
                    remote_file_id TEXT,
                    response_json TEXT,
                    final_stem TEXT,
                    video_output_path TEXT,
                    md_output_path TEXT,
                    video_sha256 TEXT,
                    md_sha256 TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_tasks_status_created
                    ON tasks(status, created_at);
                CREATE INDEX IF NOT EXISTS idx_tasks_source
                    ON tasks(source_path);

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            task_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(tasks)").fetchall()
            }
            if "markdown_draft" in task_columns:
                connection.execute("ALTER TABLE tasks DROP COLUMN markdown_draft")
                task_columns.remove("markdown_draft")
            for column in ("video_sha256", "md_sha256"):
                if column not in task_columns:
                    connection.execute(f"ALTER TABLE tasks ADD COLUMN {column} TEXT")
            now = utc_now()
            defaults = {
                "prompt": DEFAULT_PROMPT,
                "model_id": "",
                "video_fps": "0.3",
                "models_json": "[]",
                "models_updated_at": "",
                "csrf_secret": secrets.token_urlsafe(48),
            }
            connection.executemany(
                """
                INSERT OR IGNORE INTO settings(key, value, updated_at)
                VALUES (?, ?, ?)
                """,
                [(key, value, now) for key, value in defaults.items()],
            )
            connection.commit()

    def health_check(self) -> bool:
        try:
            with self.connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False

    def journal_mode(self) -> str:
        with self.connect() as connection:
            row = connection.execute("PRAGMA journal_mode").fetchone()
        return str(row[0]).lower() if row else ""

    def get_setting(self, key: str, default: str = "") -> str:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        return str(row["value"]) if row else default

    def get_settings(self, keys: Iterable[str]) -> dict[str, str]:
        key_list = list(keys)
        if not key_list:
            return {}
        placeholders = ",".join("?" for _ in key_list)
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT key, value FROM settings WHERE key IN ({placeholders})",
                key_list,
            ).fetchall()
        return {str(row["key"]): str(row["value"]) for row in rows}

    def set_settings(self, values: dict[str, str]) -> None:
        if not values:
            return
        now = utc_now()
        with self.connect() as connection:
            connection.executemany(
                """
                INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
                """,
                [(key, value, now) for key, value in values.items()],
            )
            connection.commit()

    def cached_models(self) -> list[str]:
        try:
            payload = json.loads(self.get_setting("models_json", "[]"))
        except json.JSONDecodeError:
            return []
        if not isinstance(payload, list):
            return []
        return [item for item in payload if isinstance(item, str) and item]

    def create_task(
        self,
        *,
        signature: str,
        source_path: Path,
        original_name: str,
        extension: str,
        size_bytes: int,
        mtime_ns: int,
        device: int,
        inode: int,
    ) -> int | None:
        now = utc_now()
        try:
            with self.connect() as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO tasks(
                        signature, source_path, original_name, extension,
                        size_bytes, mtime_ns, device, inode, status,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)
                    """,
                    (
                        signature,
                        str(source_path),
                        original_name,
                        extension,
                        size_bytes,
                        mtime_ns,
                        device,
                        inode,
                        now,
                        now,
                    ),
                )
                connection.commit()
                return int(cursor.lastrowid)
        except sqlite3.IntegrityError:
            return None

    def task_exists_for_identity(
        self,
        *,
        source_path: Path,
        size_bytes: int,
        mtime_ns: int,
        device: int,
        inode: int,
    ) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM tasks
                WHERE source_path = ?
                  AND size_bytes = ?
                  AND mtime_ns = ?
                  AND device = ?
                  AND inode = ?
                LIMIT 1
                """,
                (
                    str(source_path),
                    size_bytes,
                    mtime_ns,
                    device,
                    inode,
                ),
            ).fetchone()
        return row is not None

    def get_task(self, task_id: int) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()

    def list_tasks(
        self, status: str | None = None, limit: int = 200, offset: int = 0
    ) -> list[sqlite3.Row]:
        limit = min(max(limit, 1), 500)
        offset = max(offset, 0)
        with self.connect() as connection:
            if status in TASK_STATUSES:
                rows = connection.execute(
                    """
                    SELECT * FROM tasks WHERE status = ?
                    ORDER BY id DESC LIMIT ? OFFSET ?
                    """,
                    (status, limit, offset),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM tasks ORDER BY id DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall()
        return list(rows)

    def task_count(self, status: str | None = None) -> int:
        with self.connect() as connection:
            if status in TASK_STATUSES:
                row = connection.execute(
                    "SELECT COUNT(*) FROM tasks WHERE status = ?", (status,)
                ).fetchone()
            else:
                row = connection.execute("SELECT COUNT(*) FROM tasks").fetchone()
        return int(row[0]) if row else 0

    def status_counts(self) -> dict[str, int]:
        counts = {status: 0 for status in TASK_STATUSES}
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM tasks GROUP BY status"
            ).fetchall()
        for row in rows:
            counts[str(row["status"])] = int(row["count"])
        return counts

    def claim_next_task(self) -> sqlite3.Row | None:
        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            settings_rows = connection.execute(
                """
                SELECT key, value FROM settings
                WHERE key IN ('prompt', 'model_id', 'video_fps')
                """
            ).fetchall()
            settings = {
                str(row["key"]): str(row["value"]) for row in settings_rows
            }
            model = settings.get("model_id", "").strip()
            if not model:
                connection.commit()
                return None
            try:
                fps = validate_fps(settings.get("video_fps", "0.3"))
            except ValueError:
                connection.commit()
                return None
            row = connection.execute(
                "SELECT id FROM tasks WHERE status = 'queued' ORDER BY id LIMIT 1"
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            task_id = int(row["id"])
            connection.execute(
                """
                UPDATE tasks SET
                    status = 'checking',
                    error = NULL,
                    prompt_snapshot = COALESCE(NULLIF(prompt_snapshot, ''), ?),
                    model_snapshot = COALESCE(NULLIF(model_snapshot, ''), ?),
                    video_fps_snapshot = COALESCE(video_fps_snapshot, ?),
                    attempts = attempts + 1,
                    started_at = COALESCE(started_at, ?),
                    completed_at = NULL,
                    updated_at = ?
                WHERE id = ? AND status = 'queued'
                """,
                (
                    settings.get("prompt") or DEFAULT_PROMPT,
                    model,
                    fps,
                    now,
                    now,
                    task_id,
                ),
            )
            claimed = connection.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            connection.commit()
            return claimed

    def update_task(self, task_id: int, **fields: Any) -> None:
        invalid = set(fields) - TASK_MUTABLE_FIELDS
        if invalid:
            raise ValueError(f"不允许更新任务字段：{', '.join(sorted(invalid))}")
        if not fields:
            return
        if "status" in fields and fields["status"] not in TASK_STATUSES:
            raise ValueError("未知任务状态")
        fields["updated_at"] = utc_now()
        assignments = ", ".join(f"{key} = ?" for key in fields)
        values = list(fields.values()) + [task_id]
        with self.connect() as connection:
            connection.execute(
                f"UPDATE tasks SET {assignments} WHERE id = ?", values
            )
            connection.commit()

    def mark_failed(self, task_id: int, error: str) -> None:
        self.update_task(
            task_id,
            status="failed",
            error=error[:4000],
            completed_at=utc_now(),
        )

    def mark_ignored(self, task_id: int, reason: str) -> None:
        self.update_task(
            task_id,
            status="ignored",
            error=reason[:4000],
            completed_at=utc_now(),
        )

    def retry_task(self, task_id: int) -> bool:
        now = utc_now()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE tasks SET
                    status = 'queued',
                    error = NULL,
                    prompt_snapshot = NULL,
                    model_snapshot = NULL,
                    video_fps_snapshot = NULL,
                    remote_file_id = NULL,
                    response_json = NULL,
                    final_stem = NULL,
                    video_output_path = NULL,
                    md_output_path = NULL,
                    video_sha256 = NULL,
                    md_sha256 = NULL,
                    started_at = NULL,
                    completed_at = NULL,
                    updated_at = ?
                WHERE id = ? AND status = 'failed'
                """,
                (now, task_id),
            )
            connection.commit()
            return cursor.rowcount == 1

    def active_tasks(self) -> list[sqlite3.Row]:
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM tasks WHERE status IN ({placeholders}) ORDER BY id",
                ACTIVE_STATUSES,
            ).fetchall()
        return list(rows)

    def pending_remote_files(self) -> list[tuple[int, str]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, remote_file_id FROM tasks
                WHERE remote_file_id IS NOT NULL AND remote_file_id != ''
                ORDER BY id
                """
            ).fetchall()
        return [(int(row["id"]), str(row["remote_file_id"])) for row in rows]

    def clear_remote_file_id(self, task_id: int, expected_file_id: str) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE tasks SET remote_file_id = NULL, updated_at = ?
                WHERE id = ? AND remote_file_id = ?
                """,
                (utc_now(), task_id, expected_file_id),
            )
            connection.commit()
            return cursor.rowcount == 1

    def reset_active_to_queue(self, task_ids: Iterable[int]) -> None:
        ids = list(task_ids)
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        with self.connect() as connection:
            connection.execute(
                f"""
                UPDATE tasks SET status = 'queued', error = NULL, updated_at = ?
                WHERE id IN ({placeholders})
                """,
                [utc_now(), *ids],
            )
            connection.commit()
