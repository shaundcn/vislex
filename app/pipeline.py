from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .ark import (
    ArkClient,
    ArkError,
    ArkJSONError,
    ArkResponseIncomplete,
    parse_model_json,
    read_api_key,
)
from .config import AppConfig
from .db import Database, utc_now
from .media import (
    MediaError,
    OutputCollisionError,
    calculate_output_proof,
    choose_output_stem,
    fsync_directory,
    owned_source_quarantines,
    outputs_complete,
    probe_video,
    published_outputs_match,
    quarantine_and_delete_source,
    render_markdown,
    restore_quarantined_source,
    safe_flat_path,
    source_matches_task,
    stage_output_pair,
    stored_output_proof_matches,
    validate_output_extension,
)
from .models import (
    VideoAnalysis,
    normalize_model_payload,
    validation_error_text,
)


logger = logging.getLogger(__name__)


class TaskProcessor:
    def __init__(self, config: AppConfig, database: Database):
        self.config = config
        self.database = database

    async def process(self, task_id: int) -> None:
        task = self.database.get_task(task_id)
        if task is None:
            return
        source = Path(str(task["source_path"]))
        client: ArkClient | None = None
        cleanup_file_id = str(task["remote_file_id"] or "")
        api_key = ""
        try:
            extension = validate_output_extension(str(task["extension"]))
            if task["response_json"] and task["video_output_path"] and task["md_output_path"]:
                await self._finish_saved_result(task, source)
                return

            self._validate_source(source, task)
            if int(task["size_bytes"]) > self.config.max_video_bytes:
                self.database.mark_ignored(
                    task_id,
                    f"文件超过 {self.config.max_video_bytes} 字节限制",
                )
                return

            video_info = await probe_video(source)
            self.database.update_task(
                task_id, duration_seconds=video_info.duration_seconds
            )
            if video_info.duration_seconds > self.config.max_video_seconds:
                self.database.mark_ignored(
                    task_id,
                    f"视频超过 {self.config.max_video_seconds:g} 秒限制",
                )
                return
            self._validate_source(source, task)

            api_key = read_api_key(self.config) or ""
            if not api_key:
                raise ArkError("尚未保存 API Key")
            model = str(task["model_snapshot"] or "").strip()
            prompt = str(task["prompt_snapshot"] or "")
            fps = float(task["video_fps_snapshot"])
            client = ArkClient(self.config, api_key)

            self.database.update_task(task_id, status="uploading")
            upload_payload = await client.upload_file(
                source, model, fps, expected_task=task
            )
            cleanup_file_id = client.last_uploaded_file_id or ""
            self.database.update_task(
                task_id, remote_file_id=cleanup_file_id or None
            )

            self.database.update_task(task_id, status="processing")
            file_id = await client.wait_until_file_ready(upload_payload)
            analysis = await request_valid_analysis(client, file_id, prompt, model)
            self._validate_source(source, task)

            final_stem = choose_output_stem(
                self.config.output_dir,
                analysis.new_filename,
                extension,
            )
            final_video = self.config.output_dir / (final_stem + extension)
            final_markdown = self.config.output_dir / f"{final_stem}.md"
            markdown = render_markdown(
                analysis, final_video.name, str(task["original_name"])
            )
            self.database.update_task(
                task_id,
                status="moving",
                response_json=analysis.model_dump_json(),
                final_stem=final_stem,
                video_output_path=str(final_video),
                md_output_path=str(final_markdown),
            )
            proof = await stage_output_pair(
                task_id=task_id,
                source_video=source,
                final_video=final_video,
                final_markdown=final_markdown,
                markdown=markdown,
                task=task,
            )
            self.database.update_task(
                task_id,
                video_sha256=proof.video_sha256,
                md_sha256=proof.md_sha256,
            )
            self._delete_source(source, task)
            self.database.update_task(
                task_id,
                status="success",
                error=None,
                completed_at=utc_now(),
            )
        except (ArkResponseIncomplete, ArkError, MediaError, OSError, ValueError) as exc:
            self.database.mark_failed(task_id, _safe_error(exc, api_key))
        except Exception as exc:
            logger.exception("task %s failed unexpectedly", task_id)
            self.database.mark_failed(
                task_id, f"未预期的处理错误：{_safe_error(exc, api_key)}"
            )
        finally:
            if client is not None:
                try:
                    if cleanup_file_id:
                        deleted = await client.delete_file(cleanup_file_id)
                        if deleted:
                            self.database.clear_remote_file_id(
                                task_id, cleanup_file_id
                            )
                        else:
                            logger.warning(
                                "remote cleanup deferred for task %s", task_id
                            )
                except Exception:
                    logger.warning(
                        "remote cleanup failed for task %s", task_id, exc_info=True
                    )
                finally:
                    await client.close()

    async def _finish_saved_result(self, task: Any, source: Path) -> None:
        task_id = int(task["id"])
        video_path = safe_flat_path(
            self.config.output_dir, task["video_output_path"]
        )
        markdown_path = safe_flat_path(
            self.config.output_dir, task["md_output_path"]
        )
        if video_path is None or markdown_path is None:
            raise MediaError("任务保存的输出路径不安全")
        try:
            payload = json.loads(str(task["response_json"]))
            analysis = VideoAnalysis.model_validate(payload)
        except (json.JSONDecodeError, ValidationError) as exc:
            raise MediaError("保存的模型结果无法用于恢复") from exc
        markdown = render_markdown(
            analysis, video_path.name, str(task["original_name"])
        )
        if outputs_complete(task, video_path, markdown_path):
            if source.exists() or source.is_symlink():
                self._validate_source(source, task)
                if not published_outputs_match(
                    task=task,
                    source_video=source,
                    video_path=video_path,
                    markdown_path=markdown_path,
                    expected_markdown=markdown,
                ):
                    raise OutputCollisionError("恢复输出与当前任务内容不一致")
                proof = calculate_output_proof(video_path, markdown_path)
                self.database.update_task(
                    task_id,
                    video_sha256=proof.video_sha256,
                    md_sha256=proof.md_sha256,
                )
                self._delete_source(source, task)
            elif not stored_output_proof_matches(task, video_path, markdown_path):
                raise MediaError("源文件不存在，且现有输出未通过完整性校验")
            self.database.update_task(
                task_id, status="success", error=None, completed_at=utc_now()
            )
            return
        self._validate_source(source, task)
        self.database.update_task(task_id, status="moving")
        proof = await stage_output_pair(
            task_id=task_id,
            source_video=source,
            final_video=video_path,
            final_markdown=markdown_path,
            markdown=markdown,
            task=task,
        )
        self.database.update_task(
            task_id,
            video_sha256=proof.video_sha256,
            md_sha256=proof.md_sha256,
        )
        self._delete_source(source, task)
        self.database.update_task(
            task_id, status="success", error=None, completed_at=utc_now()
        )

    def _validate_source(self, source: Path, task: Any) -> None:
        if source.parent.resolve() != self.config.input_dir.resolve():
            raise MediaError("任务源文件不在 input 顶层")
        if not source_matches_task(source, task):
            raise MediaError("源文件不存在、已变化或不是普通文件")

    def _delete_source(self, source: Path, task: Any) -> None:
        quarantine_and_delete_source(
            source, task, self.config.input_dir
        )


async def request_valid_analysis(
    client: ArkClient, file_id: str, prompt: str, model: str
) -> VideoAnalysis:
    last_error = ""
    repair_hint = ""
    for attempt in range(2):
        request_prompt = (
            prompt if attempt == 0 else _repair_prompt(prompt, repair_hint)
        )
        text = await client.create_video_response(file_id, request_prompt, model)
        try:
            payload = normalize_model_payload(parse_model_json(text))
            return VideoAnalysis.model_validate(payload)
        except ArkResponseIncomplete:
            raise
        except ArkJSONError as exc:
            last_error = str(exc)
            repair_hint = "返回内容不是单一合法 JSON 对象，或包含重复字段"
        except ValidationError as exc:
            last_error = validation_error_text(exc)
            repair_hint = last_error
    raise ArkError(f"模型返回内容连续两次未通过校验：{last_error[:1600]}")


def recover_quarantined_sources(
    config: AppConfig, database: Database
) -> None:
    for task_id, quarantine in owned_source_quarantines(config.input_dir):
        task = database.get_task(task_id)
        if task is None or not source_matches_task(quarantine, task):
            continue
        source = Path(str(task["source_path"]))
        video_path = safe_flat_path(config.output_dir, task["video_output_path"])
        markdown_path = safe_flat_path(config.output_dir, task["md_output_path"])
        if stored_output_proof_matches(task, video_path, markdown_path):
            try:
                os.unlink(quarantine)
                fsync_directory(config.input_dir)
                database.update_task(
                    task_id,
                    status="success",
                    error=None,
                    completed_at=utc_now(),
                )
            except OSError as exc:
                database.mark_failed(
                    task_id, f"恢复安全删除失败：{_safe_error(exc)}"
                )
            continue
        restored = restore_quarantined_source(
            quarantine, source, config.input_dir
        )
        detail = "源文件已恢复" if restored else f"文件保留在 {quarantine.name}"
        database.mark_failed(task_id, f"安全删除恢复时输出校验失败，{detail}")


def recover_interrupted_tasks(
    config: AppConfig, database: Database
) -> None:
    reset_ids: list[int] = []
    for task in database.active_tasks():
        task_id = int(task["id"])
        source = Path(str(task["source_path"]))
        video_path = safe_flat_path(config.output_dir, task["video_output_path"])
        markdown_path = safe_flat_path(config.output_dir, task["md_output_path"])
        if outputs_complete(task, video_path, markdown_path):
            if source.exists() or source.is_symlink():
                if source_matches_task(source, task) and task["response_json"]:
                    reset_ids.append(task_id)
                else:
                    database.mark_failed(
                        task_id, "恢复时无法确认输出与源文件属于同一任务"
                    )
            elif task["response_json"] and stored_output_proof_matches(
                task, video_path, markdown_path
            ):
                database.update_task(
                    task_id,
                    status="success",
                    error=None,
                    completed_at=utc_now(),
                )
            else:
                database.mark_failed(
                    task_id, "恢复时源文件不存在，且输出未通过完整性校验"
                )
            continue
        if source_matches_task(source, task):
            if task["status"] == "moving" and not task["response_json"]:
                database.mark_failed(task_id, "移动中断且缺少已验证的模型结果")
            else:
                reset_ids.append(task_id)
        elif source.exists() or source.is_symlink():
            database.mark_failed(task_id, "容器重启时发现源文件已变化")
        else:
            database.mark_failed(task_id, "容器重启后找不到源文件")
    database.reset_active_to_queue(reset_ids)


def retry_failed_task(
    config: AppConfig, database: Database, task_id: int
) -> tuple[bool, str]:
    task = database.get_task(task_id)
    if task is None or task["status"] != "failed":
        return False, "该任务不存在或当前不能重试"
    source = Path(str(task["source_path"]))
    video_path = safe_flat_path(config.output_dir, task["video_output_path"])
    markdown_path = safe_flat_path(config.output_dir, task["md_output_path"])
    if (
        outputs_complete(task, video_path, markdown_path)
        and task["response_json"]
    ):
        try:
            analysis = VideoAnalysis.model_validate_json(
                str(task["response_json"])
            )
            if video_path is None or markdown_path is None:
                return False, "保存的输出路径不安全"
            expected_markdown = render_markdown(
                analysis, video_path.name, str(task["original_name"])
            )
            if source.exists() or source.is_symlink():
                if not source_matches_task(source, task):
                    return False, "源文件已变化，拒绝删除"
                if not published_outputs_match(
                    task=task,
                    source_video=source,
                    video_path=video_path,
                    markdown_path=markdown_path,
                    expected_markdown=expected_markdown,
                ):
                    return False, "现有输出与失败任务内容不一致"
                proof = calculate_output_proof(video_path, markdown_path)
                database.update_task(
                    task_id,
                    video_sha256=proof.video_sha256,
                    md_sha256=proof.md_sha256,
                )
                quarantine_and_delete_source(source, task, config.input_dir)
            elif not stored_output_proof_matches(task, video_path, markdown_path):
                return False, "源文件不存在，且现有输出未通过完整性校验"
            database.update_task(
                task_id, status="success", error=None, completed_at=utc_now()
            )
            return True, "已完成中断任务"
        except (MediaError, OSError, ValidationError) as exc:
            return False, f"无法完成中断任务：{_safe_error(exc)}"
    if not source_matches_task(source, task):
        return False, "源文件已变化或不存在，无法重试"
    if database.retry_task(task_id):
        return True, "任务已使用最新设置重新排队"
    return False, "任务状态已变化，请刷新页面"


def _safe_error(error: BaseException, api_key: str = "") -> str:
    value = str(error).replace("\r", " ").replace("\n", " ").strip()
    if api_key:
        value = value.replace(api_key, "***")
    return value[:4000] or type(error).__name__


def _repair_prompt(prompt: str, error: str) -> str:
    compact_error = " ".join(error.replace("\x00", " ").split())[:800]
    return (
        f"{prompt}\n\n"
        "输出纠错要求：上一次输出未通过机器校验。"
        f"错误是：{compact_error or '返回格式不符合要求'}。"
        "请重新理解同一视频，只返回一个合法 JSON 对象；"
        "不要使用 Markdown 代码块、解释文字或额外字段。"
        "必须包含 new_filename、content、transcript 三个字段。"
    )
