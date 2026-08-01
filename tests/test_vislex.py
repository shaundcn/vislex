from __future__ import annotations

import asyncio
import json
import os
import re
import stat
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.ark import (
    ArkClient,
    ArkError,
    ArkJSONError,
    ArkResponseIncomplete,
    _retry_delay,
    create_smoke_test_video,
    masked_api_key,
    parse_model_json,
    read_api_key,
    save_api_key,
)
from app.config import CHINA_TIMEZONE, AppConfig
from app.db import DATABASE_SCHEMA_VERSION, Database
from app.defaults import API_TEST_PROMPT, DEFAULT_PROMPT
from app.main import _run_api_test, create_app
from app.media import (
    MediaError,
    OutputCollisionError,
    VideoInfo,
    choose_output_stem,
    input_source_relative_path,
    outputs_complete,
    probe_video,
    quarantine_and_delete_source,
    render_markdown,
    safe_regular_output,
    stage_output_pair,
    validate_output_extension,
)
from app.models import VideoAnalysis, normalize_model_payload
from app.pipeline import (
    TaskProcessor,
    _repair_prompt,
    current_markdown_date,
    recover_interrupted_tasks,
    recover_quarantined_sources,
    request_valid_analysis,
)
from app.runtime import ApplicationRuntime, InputScanner


def make_config(root: Path, *, stable_seconds: int = 60) -> AppConfig:
    input_dir = root / "input"
    output_dir = root / "output"
    data_dir = root / "data"
    for directory in (input_dir, output_dir, data_dir):
        directory.mkdir(parents=True, exist_ok=True)
    return AppConfig(
        input_dir=input_dir,
        output_dir=output_dir,
        data_dir=data_dir,
        database_path=data_dir / "vislex.sqlite3",
        api_key_path=data_dir / "ark_api_key",
        stable_seconds=stable_seconds,
        file_poll_seconds=0.2,
        file_poll_timeout_seconds=2,
        request_timeout_seconds=2,
        upload_timeout_seconds=2,
    )


def create_db(config: AppConfig) -> Database:
    database = Database(config.database_path)
    database.initialize()
    return database


def add_task(database: Database, path: Path) -> int:
    metadata = path.lstat()
    task_id = database.create_task(
        signature=f"{path.name}-{metadata.st_size}-{metadata.st_mtime_ns}",
        source_path=path,
        original_name=path.name,
        extension=path.suffix,
        size_bytes=metadata.st_size,
        mtime_ns=metadata.st_mtime_ns,
        device=metadata.st_dev,
        inode=metadata.st_ino,
    )
    assert task_id is not None
    return task_id


class ModelAndMarkdownTests(unittest.TestCase):
    def test_built_in_prompts_use_only_the_title_contract(self):
        repair = _repair_prompt(DEFAULT_PROMPT, "new_filename: 多余字段")
        for name, prompt in (
            ("default", DEFAULT_PROMPT),
            ("api-test", API_TEST_PROMPT),
            ("repair", repair),
        ):
            with self.subTest(prompt=name):
                self.assertIn("title", prompt)
                self.assertNotIn("new_filename", prompt)

    def test_strict_model_accepts_only_contract(self):
        analysis = VideoAnalysis.model_validate(
            {
                "title": "中文Title123",
                "content": "主要内容。\n\n- 第一点。",
                "transcript": [],
            }
        )
        self.assertEqual(analysis.title, "中文Title123")
        with self.assertRaises(ValidationError):
            VideoAnalysis.model_validate(
                {
                    "title": "bad/path",
                    "content": "内容",
                    "transcript": [],
                }
            )
        with self.assertRaises(ValidationError):
            VideoAnalysis.model_validate(
                {
                    "title": "标题",
                    "content": "内容",
                    "transcript": [],
                    "extra": True,
                }
            )
        with self.assertRaises(ValidationError):
            VideoAnalysis.model_validate(
                {
                    "title": "标题",
                    "content": "内容",
                    "transcript": [""],
                }
            )

    def test_safe_model_payload_normalization(self):
        analysis = VideoAnalysis.model_validate(
            normalize_model_payload(
                {
                    "title": " #桌面_工作台-CyboPal.mp4 ",
                    "content": "内容",
                    "transcript": [" 第一段 ", "", " \n", "第二段"],
                    "language": "zh",
                }
            )
        )
        self.assertEqual(analysis.title, "桌面工作台CyboPal")
        self.assertEqual(analysis.transcript, ["第一段", "第二段"])
        self.assertEqual(
            set(analysis.model_dump()), {"title", "content", "transcript"}
        )

        string_transcript = VideoAnalysis.model_validate(
            normalize_model_payload(
                {
                    "title": "标题.mov",
                    "content": "内容",
                    "transcript": "完整语音",
                }
            )
        )
        self.assertEqual(string_transcript.title, "标题")
        self.assertEqual(string_transcript.transcript, ["完整语音"])

        long_title = VideoAnalysis.model_validate(
            normalize_model_payload(
                {
                    "title": "长" * 30,
                    "content": "内容",
                    "transcript": [],
                }
            )
        )
        self.assertEqual(long_title.title, "长" * 20)

        for unsafe_filename in ("bad/path.mp4", "bad\\path.mp4", "bad\x00name"):
            with self.assertRaises(ValidationError):
                VideoAnalysis.model_validate(
                    normalize_model_payload(
                        {
                            "title": unsafe_filename,
                            "content": "内容",
                            "transcript": [],
                        }
                    )
                )
        with self.assertRaises(ValidationError):
            VideoAnalysis.model_validate(
                normalize_model_payload(
                    {"title": "标题", "transcript": []}
                )
            )

        for legacy_payload in (
            {
                "new_filename": "旧标题",
                "content": "内容",
                "transcript": [],
            },
            {
                "title": "新标题",
                "new_filename": "旧标题",
                "content": "内容",
                "transcript": [],
            },
        ):
            with self.subTest(payload=legacy_payload):
                with self.assertRaises(ValidationError):
                    VideoAnalysis.model_validate(
                        normalize_model_payload(legacy_payload)
                    )

    def test_duplicate_json_keys_are_rejected(self):
        with self.assertRaises(ArkJSONError):
            parse_model_json(
                '{"title":"甲","title":"乙",'
                '"content":"内容","transcript":[]}'
            )
        fenced = parse_model_json(
            "```json\n"
            '{"title":"标题","content":"内容","transcript":[]}'
            "\n```"
        )
        self.assertEqual(fenced["title"], "标题")
        with self.assertRaises(ArkJSONError):
            parse_model_json("说明如下：\n```json\n{}\n```")
        with self.assertRaises(ArkJSONError):
            parse_model_json("{}\n{}")

    def test_markdown_escapes_model_text_but_keeps_dash_lists(self):
        analysis = VideoAnalysis(
            title="标题",
            content="<b>内容</b>\n\n- *列表* [链接](x)",
            transcript=["<script>alert(1)</script>"],
        )
        output = render_markdown(
            analysis,
            "标题-2.mp4",
            '一层/二层/原"文件.mp4',
            "2026-08-02",
        )
        self.assertTrue(
            output.startswith(
                '---\ntitle: "标题"\ntags:\n'
                'source: "原\\"文件.mp4"\ncreated: 2026-08-02\n---\n'
            )
        )
        self.assertIn("![[标题-2.mp4]]", output)
        self.assertIn("&lt;b&gt;内容&lt;/b&gt;", output)
        self.assertIn("- \\*列表\\* \\[链接\\]\\(x\\)", output)
        self.assertNotIn("<script>", output)
        self.assertNotIn("一层/二层", output)

    def test_markdown_date_uses_the_china_timezone(self):
        fixed = datetime(2026, 8, 2, 0, 1, tzinfo=CHINA_TIMEZONE)
        with patch("app.pipeline.datetime") as clock:
            clock.now.return_value = fixed
            self.assertEqual(current_markdown_date(), "2026-08-02")
            clock.now.assert_called_once_with(CHINA_TIMEZONE)

    def test_collision_suffix_keeps_twenty_character_stem(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            requested = "一二三四五六七八九十一二三四五六七八九十"
            (output / f"{requested}.mp4").write_bytes(b"x")
            (output / f"{requested}.md").write_text("x", encoding="utf-8")
            candidate = choose_output_stem(output, requested, ".mp4")
            self.assertTrue(candidate.endswith("-2"))
            self.assertLessEqual(len(candidate), 20)

    def test_unsafe_video_extension_cannot_break_markdown(self):
        self.assertEqual(validate_output_extension(".mp4"), ".mp4")
        self.assertEqual(validate_output_extension(""), "")
        for extension in (".md", ".MD", ".mp4]]", ".mp4\n"):
            with self.assertRaises(MediaError):
                validate_output_extension(extension)
        analysis = VideoAnalysis(
            title="标题", content="内容", transcript=[]
        )
        with self.assertRaises(MediaError):
            render_markdown(
                analysis, "标题.mp4]]\n# 注入", "原文件.mp4", "2026-08-02"
            )
        for invalid_date in ("20260802", "2026-8-2", "2026-02-30"):
            with self.subTest(created=invalid_date):
                with self.assertRaises(MediaError):
                    render_markdown(
                        analysis, "标题.mp4", "原文件.mp4", invalid_date
                    )


class DatabaseAndScannerTests(unittest.TestCase):
    def test_scanner_waits_for_sixty_stable_seconds(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary), stable_seconds=60)
            database = create_db(config)
            source = config.input_dir / "video.mp4"
            source.write_bytes(b"video")
            scanner = InputScanner(config, database)
            self.assertEqual(scanner.scan_once(0), 0)
            self.assertEqual(scanner.scan_once(59.9), 0)
            self.assertEqual(scanner.scan_once(60), 1)
            self.assertEqual(scanner.scan_once(120), 0)
            self.assertEqual(len(database.list_tasks()), 1)

    def test_scanner_skips_hidden_entries_and_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary), stable_seconds=1)
            database = create_db(config)
            (config.input_dir / ".hidden.mp4").write_bytes(b"x")
            hidden_directory = config.input_dir / ".hidden-folder"
            hidden_directory.mkdir()
            (hidden_directory / "ignored.mp4").write_bytes(b"x")
            outside = Path(temporary) / "outside"
            outside.mkdir()
            (outside / "ignored.mp4").write_bytes(b"x")
            (config.input_dir / "linked-folder").symlink_to(outside)
            target = config.input_dir / "real.mp4"
            target.write_bytes(b"x")
            (config.input_dir / "link.mp4").symlink_to(target)
            scanner = InputScanner(config, database)
            scanner.scan_once(0)
            scanner.scan_once(1)
            tasks = database.list_tasks()
            self.assertEqual([row["original_name"] for row in tasks], ["real.mp4"])

    def test_scanner_recurses_exactly_three_subdirectory_levels(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary), stable_seconds=1)
            database = create_db(config)
            expected = {
                "root.mp4",
                "one/level-one.mp4",
                "one/two/level-two.mp4",
                "one/two/three/level-three.mp4",
            }
            for relative_name in (
                *sorted(expected),
                "one/two/three/four/too-deep.mp4",
            ):
                path = config.input_dir / relative_name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(relative_name.encode("utf-8"))

            scanner = InputScanner(config, database)
            self.assertEqual(scanner.scan_once(0), 0)
            self.assertEqual(scanner.scan_once(1), len(expected))
            tasks = database.list_tasks()
            self.assertEqual(
                {str(row["original_name"]) for row in tasks},
                expected,
            )
            self.assertEqual(
                {
                    Path(str(row["source_path"])).relative_to(
                        config.input_dir
                    ).as_posix()
                    for row in tasks
                },
                expected,
            )

    def test_input_source_path_rejects_a_symlinked_parent(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            outside = Path(temporary) / "outside"
            outside.mkdir()
            source = outside / "source.mp4"
            source.write_bytes(b"video")
            linked = config.input_dir / "linked"
            linked.symlink_to(outside)
            self.assertIsNone(
                input_source_relative_path(
                    linked / source.name,
                    config.input_dir,
                )
            )

    def test_delete_rejects_a_source_below_the_fourth_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            source = (
                config.input_dir
                / "one"
                / "two"
                / "three"
                / "four"
                / "source.mp4"
            )
            source.parent.mkdir(parents=True)
            source.write_bytes(b"video")
            metadata = source.lstat()
            task = {
                "id": 1,
                "size_bytes": metadata.st_size,
                "mtime_ns": metadata.st_mtime_ns,
                "device": metadata.st_dev,
                "inode": metadata.st_ino,
            }
            with self.assertRaisesRegex(MediaError, "三层目录"):
                quarantine_and_delete_source(
                    source,
                    task,
                    config.input_dir,
                )
            self.assertEqual(source.read_bytes(), b"video")

    def test_scanner_queues_replacement_with_same_name_size_and_mtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary), stable_seconds=1)
            database = create_db(config)
            source = config.input_dir / "same.mp4"
            source.write_bytes(b"video")
            first_metadata = source.lstat()
            scanner = InputScanner(config, database)
            scanner.scan_once(0)
            self.assertEqual(scanner.scan_once(1), 1)

            source.rename(config.input_dir / ".first-file")
            source.write_bytes(b"video")
            os.utime(
                source,
                ns=(first_metadata.st_atime_ns, first_metadata.st_mtime_ns),
            )
            second_metadata = source.lstat()
            self.assertNotEqual(first_metadata.st_ino, second_metadata.st_ino)

            scanner.scan_once(2)
            self.assertEqual(scanner.scan_once(3), 1)
            tasks = database.list_tasks()
            self.assertEqual(len(tasks), 2)
            self.assertEqual(len({row["inode"] for row in tasks}), 2)

    def test_scanner_does_not_duplicate_legacy_signature_for_same_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary), stable_seconds=1)
            database = create_db(config)
            source = config.input_dir / "legacy.mp4"
            source.write_bytes(b"video")
            add_task(database, source)
            scanner = InputScanner(config, database)
            scanner.scan_once(0)
            self.assertEqual(scanner.scan_once(1), 0)
            self.assertEqual(len(database.list_tasks()), 1)

    def test_claim_snapshots_and_failed_retry_uses_latest_settings(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            database = create_db(config)
            source = config.input_dir / "video.mp4"
            source.write_bytes(b"video")
            task_id = add_task(database, source)
            database.set_settings(
                {"model_id": "model-a", "video_fps": "0.3", "prompt": "prompt-a"}
            )
            first = database.claim_next_task()
            self.assertEqual(first["model_snapshot"], "model-a")
            self.assertEqual(first["prompt_snapshot"], "prompt-a")
            self.assertEqual(first["video_fps_snapshot"], 0.3)
            database.update_task(task_id, remote_file_id="file-pending")
            database.update_task(
                task_id, markdown_created_date="2026-08-01"
            )
            database.mark_failed(task_id, "expected")
            database.set_settings(
                {"model_id": "model-b", "video_fps": "1.2", "prompt": "prompt-b"}
            )
            self.assertTrue(database.retry_task(task_id))
            second = database.claim_next_task()
            self.assertEqual(second["model_snapshot"], "model-b")
            self.assertEqual(second["prompt_snapshot"], "prompt-b")
            self.assertEqual(second["video_fps_snapshot"], 1.2)
            self.assertEqual(second["remote_file_id"], "file-pending")
            self.assertIsNone(second["markdown_created_date"])

    def test_wal_and_only_two_business_tables(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            database = create_db(config)
            self.assertEqual(database.journal_mode(), "wal")
            with database.connect() as connection:
                version = int(
                    connection.execute("PRAGMA user_version").fetchone()[0]
                )
                names = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                columns = {
                    str(row["name"])
                    for row in connection.execute(
                        "PRAGMA table_info(tasks)"
                    ).fetchall()
                }
            self.assertEqual(version, DATABASE_SCHEMA_VERSION)
            self.assertEqual(names, {"tasks", "settings"})
            self.assertIn("markdown_created_date", columns)

    def test_initialize_preserves_existing_tasks_settings_and_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = make_config(root)
            database = create_db(config)
            api_key = config.data_dir / "ark_api_key"
            unrelated_data = config.data_dir / "keep.bin"
            output = config.output_dir / "已有归档.md"
            output_video = config.output_dir / "已有归档.mp4"
            video_directory = root / "video"
            video_directory.mkdir()
            video_fixture = video_directory / "keep.mp4"
            source = config.input_dir / "source.mp4"
            source.write_bytes(b"video")
            output.write_text("existing output", encoding="utf-8")
            output_video.write_bytes(b"existing video")
            api_key.write_text("secret", encoding="utf-8")
            unrelated_data.write_bytes(b"keep")
            video_fixture.write_bytes(b"video")
            task_id = add_task(database, source)
            previous_csrf = database.get_setting("csrf_secret")
            database.set_settings(
                {
                    "prompt": "custom prompt",
                    "model_id": "custom-model",
                    "video_fps": "1.2",
                    "models_json": '["custom-model"]',
                }
            )

            database.initialize()

            self.assertIsNotNone(database.get_task(task_id))
            self.assertEqual(database.get_setting("prompt"), "custom prompt")
            self.assertEqual(database.get_setting("model_id"), "custom-model")
            self.assertEqual(database.get_setting("video_fps"), "1.2")
            self.assertEqual(database.cached_models(), ["custom-model"])
            self.assertEqual(database.get_setting("csrf_secret"), previous_csrf)
            self.assertEqual(source.read_bytes(), b"video")
            self.assertEqual(output.read_text(encoding="utf-8"), "existing output")
            self.assertEqual(output_video.read_bytes(), b"existing video")
            self.assertEqual(api_key.read_text(encoding="utf-8"), "secret")
            self.assertEqual(unrelated_data.read_bytes(), b"keep")
            self.assertEqual(video_fixture.read_bytes(), b"video")
            with database.connect() as connection:
                version = int(
                    connection.execute("PRAGMA user_version").fetchone()[0]
                )
            self.assertEqual(version, DATABASE_SCHEMA_VERSION)

    def test_database_rejects_a_future_schema_version(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            database = create_db(config)
            source = config.input_dir / "future.mp4"
            source.write_bytes(b"future")
            task_id = add_task(database, source)
            with database.connect() as connection:
                connection.execute(
                    f"PRAGMA user_version = {DATABASE_SCHEMA_VERSION + 1}"
                )
                connection.commit()
            with self.assertRaisesRegex(RuntimeError, "拒绝降级"):
                database.initialize()
            self.assertIsNotNone(database.get_task(task_id))

    def test_remote_file_is_cleared_only_when_identifier_still_matches(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            database = create_db(config)
            source = config.input_dir / "remote.mp4"
            source.write_bytes(b"video")
            task_id = add_task(database, source)
            database.update_task(task_id, remote_file_id="file-current")
            self.assertFalse(
                database.clear_remote_file_id(task_id, "file-old")
            )
            self.assertEqual(
                database.get_task(task_id)["remote_file_id"], "file-current"
            )
            self.assertTrue(
                database.clear_remote_file_id(task_id, "file-current")
            )
            self.assertIsNone(database.get_task(task_id)["remote_file_id"])

    def test_non_finite_environment_numbers_are_rejected(self):
        for value in ("nan", "inf", "-inf"):
            with self.subTest(value=value), patch.dict(
                os.environ, {"MAX_VIDEO_SECONDS": value}
            ):
                with self.assertRaises(RuntimeError):
                    AppConfig.from_environment()

    def test_interrupted_task_returns_to_queue_without_losing_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            database = create_db(config)
            source = config.input_dir / "video.mp4"
            source.write_bytes(b"video")
            add_task(database, source)
            database.set_settings(
                {"model_id": "model-a", "video_fps": "0.4", "prompt": "snapshot"}
            )
            claimed = database.claim_next_task()
            recover_interrupted_tasks(config, database)
            recovered = database.get_task(int(claimed["id"]))
            self.assertEqual(recovered["status"], "queued")
            self.assertEqual(recovered["model_snapshot"], "model-a")
            self.assertEqual(recovered["prompt_snapshot"], "snapshot")


class FileAndApiAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_atomic_output_is_flat_and_cleans_parts(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            source = config.input_dir / "source.mp4"
            source.write_bytes(b"0123456789")
            metadata = source.lstat()
            task = {
                "size_bytes": metadata.st_size,
                "mtime_ns": metadata.st_mtime_ns,
                "device": metadata.st_dev,
                "inode": metadata.st_ino,
            }
            video = config.output_dir / "标题.mp4"
            markdown = config.output_dir / "标题.md"
            await stage_output_pair(
                task_id=1,
                source_video=source,
                final_video=video,
                final_markdown=markdown,
                markdown="# 内容\n",
                task=task,
            )
            self.assertEqual(video.read_bytes(), source.read_bytes())
            self.assertEqual(markdown.read_text(encoding="utf-8"), "# 内容\n")
            self.assertFalse(any(path.suffix == ".part" for path in config.output_dir.iterdir()))
            self.assertTrue(all(path.parent == config.output_dir for path in config.output_dir.iterdir()))

    async def test_same_size_foreign_output_is_not_accepted(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            source = config.input_dir / "source.mp4"
            source.write_bytes(b"correct")
            metadata = source.lstat()
            task = {
                "size_bytes": metadata.st_size,
                "mtime_ns": metadata.st_mtime_ns,
                "device": metadata.st_dev,
                "inode": metadata.st_ino,
            }
            video = config.output_dir / "标题.mp4"
            markdown = config.output_dir / "标题.md"
            video.write_bytes(b"foreign")
            with self.assertRaises(OutputCollisionError):
                await stage_output_pair(
                    task_id=2,
                    source_video=source,
                    final_video=video,
                    final_markdown=markdown,
                    markdown="# 内容\n",
                    task=task,
                )
            self.assertFalse(markdown.exists())

    async def test_source_replacement_is_restored_instead_of_deleted(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            source = config.input_dir / "source.mp4"
            replacement = config.input_dir / ".replacement.mp4"
            displaced = config.input_dir / ".displaced.mp4"
            source.write_bytes(b"original")
            replacement.write_bytes(b"new-file")
            metadata = source.lstat()
            task = {
                "id": 7,
                "size_bytes": metadata.st_size,
                "mtime_ns": metadata.st_mtime_ns,
                "device": metadata.st_dev,
                "inode": metadata.st_ino,
            }
            real_rename = os.rename
            raced = False

            def racing_rename(left, right):
                nonlocal raced
                if not raced and Path(left) == source:
                    raced = True
                    real_rename(source, displaced)
                    real_rename(replacement, source)
                return real_rename(left, right)

            with patch("app.media.os.rename", side_effect=racing_rename):
                with self.assertRaises(MediaError):
                    quarantine_and_delete_source(
                        source, task, config.input_dir
                    )
            self.assertEqual(source.read_bytes(), b"new-file")
            self.assertEqual(displaced.read_bytes(), b"original")
            self.assertFalse(
                any(
                    path.name.startswith(".vislex-delete-")
                    for path in config.input_dir.iterdir()
                )
            )

    async def test_quarantined_source_recovers_after_interrupted_delete(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            database = create_db(config)
            source = config.input_dir / "one" / "two" / "source.mp4"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"video")
            task_id = add_task(database, source)
            task = database.get_task(task_id)
            analysis = VideoAnalysis(
                title="恢复测试", content="内容", transcript=[]
            )
            video = config.output_dir / "恢复测试.mp4"
            markdown = config.output_dir / "恢复测试.md"
            markdown_text = render_markdown(
                analysis, video.name, source.name, "2026-08-02"
            )
            proof = await stage_output_pair(
                task_id=task_id,
                source_video=source,
                final_video=video,
                final_markdown=markdown,
                markdown=markdown_text,
                task=task,
            )
            database.update_task(
                task_id,
                status="moving",
                response_json=analysis.model_dump_json(),
                markdown_created_date="2026-08-02",
                video_output_path=str(video),
                md_output_path=str(markdown),
                video_sha256=proof.video_sha256,
                md_sha256=proof.md_sha256,
            )
            quarantine = (
                config.input_dir
                / f".vislex-delete-{task_id}-0123456789abcdef.part"
            )
            source.rename(quarantine)
            recover_quarantined_sources(config, database)
            self.assertFalse(quarantine.exists())
            self.assertFalse(source.exists())
            self.assertEqual(database.get_task(task_id)["status"], "success")

    async def test_moving_recovery_keeps_saved_date_and_actual_collision_name(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            database = create_db(config)
            source = config.input_dir / "nested" / "source.mp4"
            source.parent.mkdir()
            source.write_bytes(b"video")
            task_id = add_task(database, source)
            analysis = VideoAnalysis(title="归档", content="内容", transcript=[])
            existing_video = config.output_dir / "归档.mp4"
            existing_markdown = config.output_dir / "归档.md"
            existing_video.write_bytes(b"existing")
            existing_markdown.write_text("existing", encoding="utf-8")
            final_video = config.output_dir / "归档-2.mp4"
            final_markdown = config.output_dir / "归档-2.md"
            database.update_task(
                task_id,
                status="moving",
                response_json=analysis.model_dump_json(),
                markdown_created_date="2026-08-01",
                final_stem="归档-2",
                video_output_path=str(final_video),
                md_output_path=str(final_markdown),
            )

            with patch(
                "app.pipeline.current_markdown_date",
                side_effect=AssertionError("恢复时不应重新生成日期"),
            ):
                await TaskProcessor(config, database).process(task_id)

            rendered = final_markdown.read_text(encoding="utf-8")
            self.assertIn('title: "归档"', rendered)
            self.assertIn("created: 2026-08-01", rendered)
            self.assertIn("![[归档-2.mp4]]", rendered)
            self.assertEqual(existing_video.read_bytes(), b"existing")
            self.assertEqual(
                existing_markdown.read_text(encoding="utf-8"), "existing"
            )
            self.assertEqual(database.get_task(task_id)["status"], "success")

    async def test_nested_quarantine_restores_source_when_outputs_are_missing(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            database = create_db(config)
            source = config.input_dir / "one" / "two" / "source.mp4"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"video")
            task_id = add_task(database, source)
            database.update_task(task_id, status="moving")
            quarantine = (
                config.input_dir
                / f".vislex-delete-{task_id}-0123456789abcdef.part"
            )
            source.rename(quarantine)

            recover_quarantined_sources(config, database)

            self.assertFalse(quarantine.exists())
            self.assertEqual(source.read_bytes(), b"video")
            recovered = database.get_task(task_id)
            self.assertEqual(recovered["status"], "failed")
            self.assertIn("源文件已恢复", recovered["error"])

    async def test_missing_source_recovery_requires_output_hashes(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            database = create_db(config)
            analysis = VideoAnalysis(
                title="标题",
                content="正确内容",
                transcript=[],
            )

            bad_source = config.input_dir / "bad-source.mp4"
            bad_source.write_bytes(b"correct")
            bad_task_id = add_task(database, bad_source)
            bad_video = config.output_dir / "错误.mp4"
            bad_markdown = config.output_dir / "错误.md"
            bad_video.write_bytes(b"foreign")
            bad_markdown.write_text("WRONG MARKDOWN", encoding="utf-8")
            database.update_task(
                bad_task_id,
                status="moving",
                response_json=analysis.model_dump_json(),
                markdown_created_date="2026-08-02",
                video_output_path=str(bad_video),
                md_output_path=str(bad_markdown),
            )
            bad_source.unlink()
            recover_interrupted_tasks(config, database)
            self.assertEqual(database.get_task(bad_task_id)["status"], "failed")

            good_source = config.input_dir / "good-source.mp4"
            good_source.write_bytes(b"correct")
            good_task_id = add_task(database, good_source)
            good_task = database.get_task(good_task_id)
            good_video = config.output_dir / "标题.mp4"
            good_markdown = config.output_dir / "标题.md"
            markdown = render_markdown(
                analysis, good_video.name, good_source.name, "2026-08-02"
            )
            proof = await stage_output_pair(
                task_id=good_task_id,
                source_video=good_source,
                final_video=good_video,
                final_markdown=good_markdown,
                markdown=markdown,
                task=good_task,
            )
            database.update_task(
                good_task_id,
                status="moving",
                response_json=analysis.model_dump_json(),
                markdown_created_date="2026-08-02",
                video_output_path=str(good_video),
                md_output_path=str(good_markdown),
                video_sha256=proof.video_sha256,
                md_sha256=proof.md_sha256,
            )
            good_source.unlink()
            recover_interrupted_tasks(config, database)
            self.assertEqual(database.get_task(good_task_id)["status"], "success")

    async def test_ffmpeg_smoke_video_is_valid(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "one-second.mp4"
            await create_smoke_test_video(target)
            info = await probe_video(target)
            self.assertGreater(info.duration_seconds, 0)
            self.assertLessEqual(info.duration_seconds, 1.2)

    async def test_full_task_process_uses_one_validation_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            database = create_db(config)
            save_api_key(config, "abcd-secret-wxyz")
            database.set_settings(
                {
                    "model_id": "model-a",
                    "video_fps": "0.3",
                    "prompt": "prompt-a",
                }
            )
            source = (
                config.input_dir
                / "one"
                / "two"
                / "three"
                / "source.mp4"
            )
            source.parent.mkdir(parents=True)
            source.write_bytes(b"video-content")
            task_id = add_task(database, source)
            claimed = database.claim_next_task()
            self.assertEqual(int(claimed["id"]), task_id)

            class FakeArkClient:
                def __init__(self, *_):
                    self.last_uploaded_file_id = None

                async def upload_file(self, *_args, **_kwargs):
                    self.last_uploaded_file_id = "file-1"
                    return {"id": "file-1", "status": "ready"}

                async def wait_until_file_ready(self, _payload):
                    return "file-1"

                async def create_video_response(self, *_):
                    return (
                        '{"title":"归档_测试.mp4",'
                        '"content":"内容","transcript":"语音","extra":true}'
                    )

                async def delete_file(self, _file_id):
                    return True

                async def close(self):
                    return None

            async def fake_probe(_path):
                return VideoInfo(duration_seconds=1.0)

            with patch("app.pipeline.ArkClient", FakeArkClient), patch(
                "app.pipeline.probe_video", fake_probe
            ):
                await TaskProcessor(config, database).process(task_id)

            finished = database.get_task(task_id)
            self.assertEqual(finished["status"], "success")
            self.assertIsNone(finished["remote_file_id"])
            self.assertFalse(source.exists())
            self.assertTrue(source.parent.is_dir())
            self.assertTrue((config.output_dir / "归档测试.mp4").is_file())
            self.assertTrue(
                all(
                    item.parent == config.output_dir
                    for item in config.output_dir.iterdir()
                )
            )
            markdown = (
                config.output_dir / "归档测试.md"
            ).read_text(encoding="utf-8")
            self.assertIn("语音", markdown)
            self.assertRegex(
                str(finished["markdown_created_date"]),
                r"^\d{4}-\d{2}-\d{2}$",
            )
            self.assertIn(
                f"created: {finished['markdown_created_date']}", markdown
            )
            self.assertEqual(
                set(json.loads(str(finished["response_json"]))),
                {"title", "content", "transcript"},
            )

    async def test_analysis_repairs_soft_drift_without_second_request(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            class FakeClient:
                def __init__(self):
                    self.prompts: list[str] = []

                async def create_video_response(
                    self, file_id: str, prompt: str, model: str
                ) -> str:
                    del file_id, model
                    self.prompts.append(prompt)
                    return (
                        "```json\n"
                        '{"title":"桌面_工作台#CyboPal.mp4",'
                        '"content":"内容","transcript":"语音","extra":true}'
                        "\n```"
                    )

            client = FakeClient()
            analysis = await request_valid_analysis(
                client, "file-1", "原提示词", "model-1"
            )
            self.assertEqual(analysis.title, "桌面工作台CyboPal")
            self.assertEqual(analysis.transcript, ["语音"])
            self.assertEqual(client.prompts, ["原提示词"])

    async def test_second_analysis_request_includes_validation_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            class FakeClient:
                def __init__(self):
                    self.prompts: list[str] = []
                    self.responses = [
                        (
                            '{"new_filename":"旧字段","content":"内容",'
                            '"transcript":[]}'
                        ),
                        (
                            '{"title":"安全标题","content":"内容",'
                            '"transcript":[]}'
                        ),
                    ]

                async def create_video_response(
                    self, file_id: str, prompt: str, model: str
                ) -> str:
                    del file_id, model
                    self.prompts.append(prompt)
                    return self.responses.pop(0)

            client = FakeClient()
            analysis = await request_valid_analysis(
                client, "file-1", "原提示词", "model-1"
            )
            self.assertEqual(analysis.title, "安全标题")
            self.assertEqual(client.prompts[0], "原提示词")
            self.assertIn("原提示词", client.prompts[1])
            self.assertNotIn("new_filename", client.prompts[1])
            self.assertIn("旧版字段", client.prompts[1])
            self.assertIn("只返回一个合法 JSON 对象", client.prompts[1])

    async def test_legacy_field_twice_fails_after_one_repair_request(self):
        class FakeClient:
            def __init__(self):
                self.prompts: list[str] = []

            async def create_video_response(
                self, file_id: str, prompt: str, model: str
            ) -> str:
                del file_id, model
                self.prompts.append(prompt)
                return (
                    '{"new_filename":"旧标题","content":"内容",'
                    '"transcript":[]}'
                )

        client = FakeClient()
        with self.assertRaisesRegex(ArkError, "连续两次"):
            await request_valid_analysis(
                client, "file-1", "原提示词", "model-1"
            )
        self.assertEqual(len(client.prompts), 2)
        self.assertNotIn("new_filename", client.prompts[1])

    async def test_responses_payload_and_output_extraction(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            captured: dict[str, object] = {}

            def handler(request: httpx.Request) -> httpx.Response:
                captured.update(json.loads(request.content))
                return httpx.Response(
                    200,
                    json={
                        "status": "completed",
                        "output": [
                            {
                                "status": "completed",
                                "content": [
                                    {
                                        "type": "output_text",
                                        "text": '{"title":"测试","content":"内容","transcript":[]}',
                                    }
                                ],
                            }
                        ],
                    },
                )

            client = ArkClient(
                config, "secret-key", transport=httpx.MockTransport(handler)
            )
            try:
                text = await client.create_video_response(
                    "file-123", "prompt snapshot", "model-snapshot"
                )
            finally:
                await client.close()
            self.assertEqual(captured["model"], "model-snapshot")
            self.assertEqual(captured["max_output_tokens"], 32768)
            self.assertEqual(captured["thinking"], {"type": "disabled"})
            self.assertNotIn("duration", json.dumps(captured))
            self.assertEqual(parse_model_json(text)["title"], "测试")

    async def test_incomplete_response_fails_without_parsing(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))

            def handler(_: httpx.Request) -> httpx.Response:
                return httpx.Response(
                    200,
                    json={
                        "status": "incomplete",
                        "incomplete_details": {"reason": "max_output_tokens"},
                        "output": [],
                    },
                )

            client = ArkClient(
                config, "secret-key", transport=httpx.MockTransport(handler)
            )
            try:
                with self.assertRaises(ArkResponseIncomplete):
                    await client.create_video_response(
                        "file-123", "prompt", "model"
                    )
            finally:
                await client.close()

    async def test_network_status_retries_three_times(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            calls = 0

            def handler(_: httpx.Request) -> httpx.Response:
                nonlocal calls
                calls += 1
                if calls < 4:
                    return httpx.Response(500, json={"error": {"message": "busy"}})
                return httpx.Response(
                    200, json={"data": [{"id": "all-models-are-kept"}]}
                )

            async def no_delay(*_: object, **__: object) -> None:
                return None

            client = ArkClient(
                config, "secret-key", transport=httpx.MockTransport(handler)
            )
            try:
                with patch("app.ark._retry_delay", no_delay):
                    models = await client.list_models()
            finally:
                await client.close()
            self.assertEqual(calls, 4)
            self.assertEqual(models, ["all-models-are-kept"])

    async def test_upload_source_change_keeps_file_id_for_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            source = config.input_dir / "source.mp4"
            source.write_bytes(b"original-video")
            metadata = source.lstat()
            expected = {
                "size_bytes": metadata.st_size,
                "mtime_ns": metadata.st_mtime_ns,
                "device": metadata.st_dev,
                "inode": metadata.st_ino,
            }

            async def handler(request: httpx.Request) -> httpx.Response:
                await request.aread()
                source.write_bytes(b"changed-after-upload")
                return httpx.Response(
                    200,
                    json={"id": "file-upload-race", "status": "processing"},
                    request=request,
                )

            client = ArkClient(
                config,
                "secret-key",
                transport=httpx.MockTransport(handler),
            )
            try:
                with self.assertRaisesRegex(
                    ArkError, "源文件在上传过程中发生了变化"
                ):
                    await client.upload_file(
                        source,
                        "model-a",
                        0.3,
                        expected_task=expected,
                    )
                self.assertEqual(
                    client.last_uploaded_file_id, "file-upload-race"
                )
            finally:
                await client.close()

    async def test_upload_race_cleanup_is_saved_when_delete_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            database = create_db(config)
            save_api_key(config, "abcd-secret-wxyz")
            database.set_settings(
                {
                    "model_id": "model-a",
                    "video_fps": "0.3",
                    "prompt": "prompt-a",
                }
            )
            source = config.input_dir / "source.mp4"
            source.write_bytes(b"video")
            task_id = add_task(database, source)
            database.claim_next_task()
            deleted: list[str] = []

            class FakeArkClient:
                def __init__(self, *_):
                    self.last_uploaded_file_id = None

                async def upload_file(self, *_args, **_kwargs):
                    self.last_uploaded_file_id = "file-upload-race"
                    raise ArkError("源文件在上传过程中发生了变化")

                async def delete_file(self, file_id):
                    deleted.append(file_id)
                    return False

                async def close(self):
                    return None

            async def fake_probe(_path):
                return VideoInfo(duration_seconds=1.0)

            with patch("app.pipeline.ArkClient", FakeArkClient), patch(
                "app.pipeline.probe_video", fake_probe
            ):
                await TaskProcessor(config, database).process(task_id)

            failed = database.get_task(task_id)
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["remote_file_id"], "file-upload-race")
            self.assertEqual(deleted, ["file-upload-race"])

    async def test_retry_cleans_previous_remote_before_new_upload(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            database = create_db(config)
            save_api_key(config, "abcd-secret-wxyz")
            database.set_settings(
                {
                    "model_id": "model-a",
                    "video_fps": "0.3",
                    "prompt": "prompt-a",
                }
            )
            source = config.input_dir / "source.mp4"
            source.write_bytes(b"video")
            task_id = add_task(database, source)
            database.update_task(task_id, remote_file_id="file-previous")
            database.mark_failed(task_id, "expected")
            self.assertTrue(database.retry_task(task_id))
            database.claim_next_task()
            events: list[str] = []

            class FakeArkClient:
                def __init__(self, *_):
                    self.last_uploaded_file_id = None

                async def delete_file(self, file_id):
                    events.append(f"delete:{file_id}")
                    return True

                async def upload_file(self, *_args, **_kwargs):
                    events.append("upload")
                    self.last_uploaded_file_id = "file-current"
                    return {"id": "file-current", "status": "ready"}

                async def wait_until_file_ready(self, _payload):
                    return "file-current"

                async def create_video_response(self, *_):
                    return (
                        '{"title":"重试清理","content":"内容",'
                        '"transcript":[]}'
                    )

                async def close(self):
                    return None

            async def fake_probe(_path):
                return VideoInfo(duration_seconds=1.0)

            with patch("app.pipeline.ArkClient", FakeArkClient), patch(
                "app.pipeline.probe_video", fake_probe
            ):
                await TaskProcessor(config, database).process(task_id)

            self.assertEqual(
                events,
                [
                    "delete:file-previous",
                    "upload",
                    "delete:file-current",
                ],
            )
            finished = database.get_task(task_id)
            self.assertEqual(finished["status"], "success")
            self.assertIsNone(finished["remote_file_id"])

    async def test_remote_protocol_errors_are_retried(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            calls = 0

            def handler(request: httpx.Request) -> httpx.Response:
                nonlocal calls
                calls += 1
                if calls < 4:
                    raise httpx.RemoteProtocolError(
                        "peer closed before response", request=request
                    )
                return httpx.Response(
                    200, json={"data": [{"id": "model-after-retry"}]}
                )

            async def no_delay(*_: object, **__: object) -> None:
                return None

            client = ArkClient(
                config, "secret-key", transport=httpx.MockTransport(handler)
            )
            try:
                with patch("app.ark._retry_delay", no_delay):
                    models = await client.list_models()
            finally:
                await client.close()
            self.assertEqual(calls, 4)
            self.assertEqual(models, ["model-after-retry"])

    async def test_retry_after_is_not_shortened(self):
        delays: list[float] = []

        async def capture_delay(seconds: float) -> None:
            delays.append(seconds)

        response = httpx.Response(429, headers={"Retry-After": "120"})
        with patch("app.ark.asyncio.sleep", capture_delay):
            await _retry_delay(0, response)
        self.assertEqual(delays, [120.0])

    async def test_file_preprocessing_timeout_is_a_hard_deadline(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = replace(
                make_config(Path(temporary)),
                file_poll_timeout_seconds=0.05,
                request_timeout_seconds=5,
            )

            async def handler(_: httpx.Request) -> httpx.Response:
                await asyncio.sleep(0.2)
                return httpx.Response(
                    200, json={"id": "file-1", "status": "processing"}
                )

            client = ArkClient(
                config,
                "secret-key",
                transport=httpx.MockTransport(handler),
            )
            started = asyncio.get_running_loop().time()
            try:
                with self.assertRaisesRegex(ArkError, "预处理超时"):
                    await client.wait_until_file_ready(
                        {"id": "file-1", "status": "processing"}
                    )
            finally:
                await client.close()
            elapsed = asyncio.get_running_loop().time() - started
            self.assertLess(elapsed, 0.15)

    async def test_api_test_accepts_the_same_safe_drift_as_tasks(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            prompts: list[str] = []

            class FakeArkClient:
                def __init__(self, *_):
                    self.last_uploaded_file_id = None

                async def upload_file(self, *_):
                    self.last_uploaded_file_id = "file-test"
                    return {"id": "file-test", "status": "ready"}

                async def wait_until_file_ready(self, _payload):
                    return "file-test"

                async def create_video_response(
                    self, _file_id, prompt, _model
                ):
                    prompts.append(prompt)
                    return (
                        '{"title":"接口_测试.mp4",'
                        '"content":"成功","transcript":"语音","extra":true}'
                    )

                async def delete_file(self, _file_id):
                    return True

                async def close(self):
                    return None

            async def fake_video(target):
                target.write_bytes(b"video")

            with patch("app.main.ArkClient", FakeArkClient), patch(
                "app.main.create_smoke_test_video", fake_video
            ):
                await _run_api_test(
                    config, "secret-key", "model-a", 0.3
                )
            self.assertEqual(len(prompts), 1)

    async def test_remote_cleanup_failure_remains_pending_for_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = make_config(Path(temporary))
            database = create_db(config)
            save_api_key(config, "abcd-secret-wxyz")
            first_source = config.input_dir / "first.mp4"
            second_source = config.input_dir / "second.mp4"
            first_source.write_bytes(b"first")
            second_source.write_bytes(b"second")
            first_id = add_task(database, first_source)
            second_id = add_task(database, second_source)
            database.update_task(first_id, remote_file_id="file-ok")
            database.update_task(second_id, remote_file_id="file-later")

            class FakeArkClient:
                def __init__(self, *_):
                    pass

                async def __aenter__(self):
                    return self

                async def __aexit__(self, *_):
                    return None

                async def delete_file(self, file_id):
                    return file_id == "file-ok"

            runtime = ApplicationRuntime(config, database)
            with self.assertLogs("app.runtime", level="WARNING") as logs:
                with patch("app.runtime.ArkClient", FakeArkClient):
                    await runtime._cleanup_remote_files(
                        database.pending_remote_files()
                    )
            self.assertIn("task 2", "\n".join(logs.output))
            self.assertIsNone(
                database.get_task(first_id)["remote_file_id"]
            )
            self.assertEqual(
                database.get_task(second_id)["remote_file_id"],
                "file-later",
            )


class WebAndSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.config = make_config(Path(self.temporary.name))
        self.application = create_app(self.config)
        self.database: Database = self.application.state.database

    def tearDown(self):
        self.temporary.cleanup()

    def test_pages_have_no_javascript_and_include_security_headers(self):
        with TestClient(self.application) as client:
            dashboard = client.get("/")
            settings = client.get("/settings")
            stylesheet = client.get("/static/style.css")
            self.assertEqual(dashboard.status_code, 200)
            self.assertEqual(settings.status_code, 200)
            self.assertEqual(stylesheet.status_code, 200)
            self.assertNotIn("<script", dashboard.text.lower())
            self.assertNotIn("<script", settings.text.lower())
            self.assertIn('http-equiv="refresh" content="8"', dashboard.text)
            self.assertIn(".filename-column { width: 56%; }", stylesheet.text)
            self.assertIn(".status-column { width: 9%; }", stylesheet.text)
            self.assertIn(".datetime-column { width: 12.6%; }", stylesheet.text)
            self.assertIn(".actions-column { width: 22.4%; }", stylesheet.text)
            self.assertIn(
                "grid-template-columns: minmax(0, 7fr) minmax(260px, 3fr);",
                stylesheet.text,
            )
            self.assertIn("max-height: min(76vh, 900px);", stylesheet.text)
            self.assertIn("script-src 'none'", dashboard.headers["content-security-policy"])
            self.assertEqual(dashboard.headers["x-frame-options"], "DENY")
            self.assertEqual(client.get("/healthz").json(), {"status": "ok"})

    def test_csrf_rejects_bad_token_and_persists_prompt(self):
        with TestClient(self.application) as client:
            page = client.get("/settings")
            token_match = re.search(
                r'name="csrf_token" value="([^"]+)"', page.text
            )
            self.assertIsNotNone(token_match)
            token = token_match.group(1)
            rejected = client.post(
                "/settings/prompt/save",
                data={"csrf_token": "wrong", "prompt": "bad"},
                follow_redirects=False,
            )
            self.assertEqual(rejected.status_code, 403)
            saved = client.post(
                "/settings/prompt/save",
                data={"csrf_token": token, "prompt": "persistent prompt"},
                follow_redirects=False,
            )
            self.assertEqual(saved.status_code, 303)
            self.assertEqual(
                self.database.get_setting("prompt"), "persistent prompt"
            )
        recreated = create_app(self.config)
        self.assertEqual(
            recreated.state.database.get_setting("prompt"), "persistent prompt"
        )

    def test_oversized_form_is_rejected_before_parsing(self):
        with TestClient(self.application) as client:
            response = client.post(
                "/settings/prompt/save",
                content=b"x" * (64 * 1024 + 1),
                headers={
                    "Content-Type": "application/x-www-form-urlencoded"
                },
            )
        self.assertEqual(response.status_code, 413)

    def test_range_and_head_video_requests(self):
        video = self.config.output_dir / "标题.mp4"
        markdown = self.config.output_dir / "标题.md"
        video.write_bytes(b"0123456789")
        markdown.write_text("# report\n", encoding="utf-8")
        source = self.config.input_dir / "original.mp4"
        source.write_bytes(b"0123456789")
        task_id = add_task(self.database, source)
        self.database.update_task(
            task_id,
            status="success",
            video_output_path=str(video),
            md_output_path=str(markdown),
        )
        with TestClient(self.application) as client:
            dashboard = client.get("/")
            self.assertIn(f"/tasks/{task_id}/preview", dashboard.text)
            self.assertIn(f"/tasks/{task_id}/video/download", dashboard.text)
            self.assertIn(f"/tasks/{task_id}/markdown/download", dashboard.text)
            self.assertIn(">预览</a>", dashboard.text)
            self.assertIn(">下载视频</a>", dashboard.text)
            self.assertIn(">下载MD</a>", dashboard.text)
            head = client.head(f"/tasks/{task_id}/video")
            self.assertEqual(head.status_code, 200)
            self.assertEqual(head.headers["content-length"], "10")
            self.assertTrue(
                head.headers["content-disposition"].startswith("inline;")
            )
            partial = client.get(
                f"/tasks/{task_id}/video", headers={"Range": "bytes=2-5"}
            )
            self.assertEqual(partial.status_code, 206)
            self.assertEqual(partial.content, b"2345")
            self.assertEqual(partial.headers["content-range"], "bytes 2-5/10")
            preview = client.get(f"/tasks/{task_id}/preview")
            self.assertEqual(preview.status_code, 200)
            self.assertIn('<div class="preview-layout">', preview.text)
            self.assertIn(f'src="/tasks/{task_id}/video"', preview.text)
            self.assertIn(
                f'href="/tasks/{task_id}/video" target="_blank" '
                'rel="noopener">全屏播放</a>',
                preview.text,
            )
            self.assertIn("# report", preview.text)
            legacy_preview = client.get(f"/tasks/{task_id}/markdown")
            self.assertEqual(legacy_preview.status_code, 200)
            video_download = client.get(f"/tasks/{task_id}/video/download")
            self.assertEqual(video_download.content, b"0123456789")
            self.assertTrue(
                video_download.headers["content-disposition"].startswith(
                    "attachment;"
                )
            )
            markdown_download = client.get(
                f"/tasks/{task_id}/markdown/download"
            )
            self.assertEqual(markdown_download.content, b"# report\n")
            self.assertTrue(
                markdown_download.headers["content-disposition"].startswith(
                    "attachment;"
                )
            )

    def test_zero_length_video_has_zero_content_length(self):
        video = self.config.output_dir / "空视频.mp4"
        markdown = self.config.output_dir / "空视频.md"
        video.write_bytes(b"")
        markdown.write_text("# empty\n", encoding="utf-8")
        source = self.config.input_dir / "empty.mp4"
        source.write_bytes(b"")
        task_id = add_task(self.database, source)
        self.database.update_task(
            task_id,
            status="success",
            video_output_path=str(video),
            md_output_path=str(markdown),
        )

        with TestClient(self.application) as client:
            head = client.head(f"/tasks/{task_id}/video")
            self.assertEqual(head.status_code, 200)
            self.assertEqual(head.headers["content-length"], "0")
            response = client.get(f"/tasks/{task_id}/video")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["content-length"], "0")
            self.assertEqual(response.content, b"")
            unsatisfied = client.get(
                f"/tasks/{task_id}/video",
                headers={"Range": "bytes=0-"},
            )
            self.assertEqual(unsatisfied.status_code, 416)
            self.assertEqual(
                unsatisfied.headers["content-range"], "bytes */0"
            )

    def test_output_symlink_is_never_served(self):
        target = self.config.output_dir / "target.mp4"
        target.write_bytes(b"video")
        link = self.config.output_dir / "link.mp4"
        link.symlink_to(target)
        self.assertIsNone(safe_regular_output(self.config.output_dir, link))

    def test_api_key_permissions_and_masking(self):
        save_api_key(self.config, "abcd-very-secret-wxyz")
        mode = stat.S_IMODE(self.config.api_key_path.lstat().st_mode)
        self.assertEqual(mode, 0o600)
        mask = masked_api_key(self.config)
        self.assertTrue(mask.startswith("abcd"))
        self.assertTrue(mask.endswith("wxyz"))
        self.assertNotIn("very-secret", mask)
        self.config.api_key_path.chmod(0o644)
        with self.assertRaises(ArkError):
            read_api_key(self.config)

    def test_proxy_host_is_accepted_and_static_url_is_relative(self):
        with TestClient(self.application) as client:
            response = client.get(
                "/", headers={"Host": "device.user.fnos.net"}
            )
            self.assertEqual(response.status_code, 200)
            self.assertIn('href="/static/style.css"', response.text)
            self.assertNotIn(
                'href="http://device.user.fnos.net/static/style.css"',
                response.text,
            )

    def test_dashboard_paginates_tasks_after_two_hundred(self):
        for number in range(1, 202):
            task_id = self.database.create_task(
                signature=f"pagination-{number}",
                source_path=self.config.input_dir / f"task-{number:03d}.mp4",
                original_name=f"task-{number:03d}.mp4",
                extension=".mp4",
                size_bytes=number,
                mtime_ns=number,
                device=1,
                inode=number,
            )
            self.assertIsNotNone(task_id)
        with TestClient(self.application) as client:
            first_page = client.get("/")
            second_page = client.get("/?page=2")
        self.assertEqual(first_page.status_code, 200)
        self.assertEqual(second_page.status_code, 200)
        self.assertNotIn("task-001.mp4", first_page.text)
        self.assertIn("task-001.mp4", second_page.text)
        self.assertIn("第 1 / 2 页，共 201 项", first_page.text)
        self.assertIn('href="/?page=2"', first_page.text)


if __name__ == "__main__":
    unittest.main()
