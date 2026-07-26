from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from .ark import ArkClient, ArkError, read_api_key
from .config import AppConfig
from .db import Database
from .media import remove_owned_part_files
from .pipeline import (
    TaskProcessor,
    recover_interrupted_tasks,
    recover_quarantined_sources,
)


logger = logging.getLogger(__name__)


@dataclass
class ObservedFile:
    size_bytes: int
    mtime_ns: int
    device: int
    inode: int
    unchanged_since: float
    submitted_signature: str | None = None


class InputScanner:
    def __init__(self, config: AppConfig, database: Database):
        self.config = config
        self.database = database
        self.observed: dict[str, ObservedFile] = {}

    def scan_once(self, now: float) -> int:
        submitted = 0
        live_names: set[str] = set()
        with os.scandir(self.config.input_dir) as entries:
            for entry in entries:
                if entry.name.startswith("."):
                    continue
                try:
                    if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                        continue
                    metadata = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                live_names.add(entry.name)
                observed = self.observed.get(entry.name)
                identity = (
                    metadata.st_size,
                    metadata.st_mtime_ns,
                    metadata.st_dev,
                    metadata.st_ino,
                )
                if observed is None or identity != (
                    observed.size_bytes,
                    observed.mtime_ns,
                    observed.device,
                    observed.inode,
                ):
                    self.observed[entry.name] = ObservedFile(
                        size_bytes=metadata.st_size,
                        mtime_ns=metadata.st_mtime_ns,
                        device=metadata.st_dev,
                        inode=metadata.st_ino,
                        unchanged_since=now,
                    )
                    continue
                if now - observed.unchanged_since < self.config.stable_seconds:
                    continue
                signature = _file_signature(
                    entry.name,
                    observed.size_bytes,
                    observed.mtime_ns,
                    observed.device,
                    observed.inode,
                )
                if observed.submitted_signature == signature:
                    continue
                source_path = self.config.input_dir / entry.name
                if self.database.task_exists_for_identity(
                    source_path=source_path,
                    size_bytes=observed.size_bytes,
                    mtime_ns=observed.mtime_ns,
                    device=observed.device,
                    inode=observed.inode,
                ):
                    observed.submitted_signature = signature
                    continue
                task_id = self.database.create_task(
                    signature=signature,
                    source_path=source_path,
                    original_name=entry.name,
                    extension=Path(entry.name).suffix,
                    size_bytes=observed.size_bytes,
                    mtime_ns=observed.mtime_ns,
                    device=observed.device,
                    inode=observed.inode,
                )
                observed.submitted_signature = signature
                if task_id is not None:
                    submitted += 1
        for missing in set(self.observed) - live_names:
            self.observed.pop(missing, None)
        return submitted


class ApplicationRuntime:
    def __init__(self, config: AppConfig, database: Database):
        self.config = config
        self.database = database
        self.scanner = InputScanner(config, database)
        self.processor = TaskProcessor(config, database)
        self.stop_event = asyncio.Event()
        self.scan_event = asyncio.Event()
        self.background_tasks: list[asyncio.Task[None]] = []
        self.cleanup_task: asyncio.Task[None] | None = None
        self.loop_errors: dict[str, str | None] = {"scan": None, "worker": None}

    async def start(self) -> None:
        remove_owned_part_files(self.config.output_dir)
        recover_quarantined_sources(self.config, self.database)
        recover_interrupted_tasks(self.config, self.database)
        pending_remote_files = self.database.pending_remote_files()
        self.background_tasks = [
            asyncio.create_task(self._scan_loop(), name="vislex-scanner"),
            asyncio.create_task(self._worker_loop(), name="vislex-worker"),
        ]
        if pending_remote_files:
            self.cleanup_task = asyncio.create_task(
                self._cleanup_remote_files(pending_remote_files),
                name="vislex-recovery-cleanup",
            )

    async def stop(self) -> None:
        self.stop_event.set()
        self.scan_event.set()
        for task in self.background_tasks:
            task.cancel()
        tasks: list[asyncio.Task[None]] = list(self.background_tasks)
        if self.cleanup_task is not None:
            self.cleanup_task.cancel()
            tasks.append(self.cleanup_task)
        await asyncio.gather(*tasks, return_exceptions=True)
        self.background_tasks.clear()
        self.cleanup_task = None

    def request_scan(self) -> None:
        self.scan_event.set()

    def is_healthy(self) -> bool:
        return (
            len(self.background_tasks) == 2
            and all(not task.done() for task in self.background_tasks)
            and not any(self.loop_errors.values())
        )

    async def _scan_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while not self.stop_event.is_set():
            try:
                self.scanner.scan_once(loop.time())
                self.loop_errors["scan"] = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("input scan failed")
                self.loop_errors["scan"] = f"{type(exc).__name__}: {exc}"
            try:
                await asyncio.wait_for(
                    self.scan_event.wait(),
                    timeout=self.config.scan_interval_seconds,
                )
                self.scan_event.clear()
            except TimeoutError:
                pass

    async def _worker_loop(self) -> None:
        while not self.stop_event.is_set():
            task = None
            try:
                if read_api_key(self.config):
                    task = self.database.claim_next_task()
                if task is not None:
                    await self.processor.process(int(task["id"]))
                self.loop_errors["worker"] = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("worker loop failed")
                self.loop_errors["worker"] = f"{type(exc).__name__}: {exc}"
            if task is None:
                try:
                    await asyncio.wait_for(self.stop_event.wait(), timeout=1.0)
                except TimeoutError:
                    pass

    async def _cleanup_remote_files(
        self, remote_files: list[tuple[int, str]]
    ) -> None:
        try:
            key = read_api_key(self.config)
        except ArkError:
            return
        if not key:
            return
        try:
            async with ArkClient(self.config, key) as client:
                for task_id, file_id in remote_files:
                    if await client.delete_file(file_id):
                        self.database.clear_remote_file_id(task_id, file_id)
                    else:
                        logger.warning(
                            "remote recovery cleanup deferred for task %s",
                            task_id,
                        )
        except Exception:
            logger.warning("remote recovery cleanup failed", exc_info=True)


def _file_signature(
    filename: str,
    size_bytes: int,
    mtime_ns: int,
    device: int,
    inode: int,
) -> str:
    raw = f"v2\0{filename}\0{size_bytes}\0{mtime_ns}\0{device}\0{inode}".encode(
        "utf-8", errors="surrogatepass"
    )
    return hashlib.sha256(raw).hexdigest()
