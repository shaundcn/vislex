from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import json
import math
import os
import re
import secrets
import stat
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from .config import MAX_INPUT_SUBDIRECTORY_DEPTH
from .models import VideoAnalysis


class MediaError(RuntimeError):
    pass


class OutputCollisionError(MediaError):
    pass


@dataclass(frozen=True)
class VideoInfo:
    duration_seconds: float


@dataclass(frozen=True)
class OutputProof:
    video_sha256: str
    md_sha256: str


async def probe_video(path: Path) -> VideoInfo:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise MediaError("容器中找不到 ffprobe") from exc
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=120)
    except TimeoutError as exc:
        process.kill()
        await process.wait()
        raise MediaError("ffprobe 检查超时") from exc
    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip().splitlines()
        suffix = f"：{detail[-1][:300]}" if detail else ""
        raise MediaError(f"ffprobe 无法读取该文件{suffix}")
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise MediaError("ffprobe 返回了无法解析的数据") from exc
    streams = payload.get("streams") or []
    video_streams = [
        item
        for item in streams
        if isinstance(item, dict) and item.get("codec_type") == "video"
    ]
    if not video_streams:
        raise MediaError("文件中没有视频流")
    candidates = [(payload.get("format") or {}).get("duration")]
    candidates.extend(item.get("duration") for item in video_streams)
    duration: float | None = None
    for candidate in candidates:
        try:
            parsed = float(candidate)
        except (TypeError, ValueError):
            continue
        if math.isfinite(parsed) and parsed >= 0:
            duration = parsed if duration is None else max(duration, parsed)
    if duration is None:
        raise MediaError("无法确定视频时长")
    return VideoInfo(duration_seconds=duration)


MARKDOWN_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+.!|>\-])")
SAFE_VIDEO_EXTENSION = re.compile(r"^(?:\.[A-Za-z0-9]{1,10})?$")
SOURCE_QUARANTINE_PATTERN = re.compile(
    r"^\.vislex-delete-(?P<task_id>\d+)-[0-9a-f]{16}\.part$"
)


def escape_markdown_text(value: str) -> str:
    escaped_html = html.escape(value, quote=False)
    return MARKDOWN_SPECIAL.sub(r"\\\1", escaped_html)


def render_content_markdown(value: str) -> str:
    rendered: list[str] = []
    for line in value.splitlines():
        stripped = line.strip()
        if not stripped:
            rendered.append("")
        elif stripped.startswith("- "):
            rendered.append(f"- {escape_markdown_text(stripped[2:].strip())}")
        else:
            rendered.append(escape_markdown_text(stripped))
    return "\n".join(rendered)


def render_markdown(
    analysis: VideoAnalysis,
    video_filename: str,
    original_filename: str,
    created_date: str,
) -> str:
    if (
        Path(video_filename).name != video_filename
        or any(character in video_filename for character in ("/", "\\", "[", "]"))
        or any(ord(character) < 32 or ord(character) == 127 for character in video_filename)
    ):
        raise MediaError("视频文件名不能安全写入 Markdown")
    try:
        parsed_date = date.fromisoformat(created_date)
    except ValueError as exc:
        raise MediaError("Markdown 建立日期格式无效") from exc
    if parsed_date.isoformat() != created_date:
        raise MediaError("Markdown 建立日期必须使用 YYYY-MM-DD 格式")
    source_filename = original_filename.rsplit("/", 1)[-1]
    if not source_filename:
        raise MediaError("视频原文件名不能为空")
    lines = [
        "---",
        f"title: {json.dumps(analysis.title, ensure_ascii=False)}",
        "tags:",
        f"source: {json.dumps(source_filename, ensure_ascii=False)}",
        f"created: {created_date}",
        "---",
        "",
        f"![[{video_filename}]]",
        "",
        "## 视频内容",
        "",
        render_content_markdown(analysis.content),
        "",
        "## 完整语音转写",
        "",
    ]
    if analysis.transcript:
        lines.append(
            "\n\n".join(escape_markdown_text(item) for item in analysis.transcript)
        )
    else:
        lines.append("无可转写语音。")
    lines.append("")
    return "\n".join(lines)


def choose_output_stem(output_dir: Path, requested: str, extension: str) -> str:
    extension = validate_output_extension(extension)
    existing = {entry.name.casefold() for entry in output_dir.iterdir()}
    for number in range(1, 100_000):
        suffix = "" if number == 1 else f"-{number}"
        available = 20 - len(suffix)
        if available < 1:
            break
        candidate = f"{requested[:available]}{suffix}"
        if (
            f"{candidate}{extension}".casefold() not in existing
            and f"{candidate}.md".casefold() not in existing
        ):
            return candidate
    raise MediaError("无法为重名输出生成可用文件名")


def validate_output_extension(extension: str) -> str:
    if not SAFE_VIDEO_EXTENSION.fullmatch(extension):
        raise MediaError("视频扩展名不安全")
    if extension.casefold() == ".md":
        raise MediaError("视频扩展名不能是 .md")
    return extension


def task_temp_paths(output_dir: Path, task_id: int) -> tuple[Path, Path]:
    return (
        output_dir / f".vislex-task-{task_id}.video.part",
        output_dir / f".vislex-task-{task_id}.md.part",
    )


def remove_task_parts(output_dir: Path, task_id: int) -> None:
    for path in task_temp_paths(output_dir, task_id):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def remove_owned_part_files(output_dir: Path) -> None:
    try:
        entries = list(output_dir.iterdir())
    except FileNotFoundError:
        return
    pattern = re.compile(r"^\.vislex-task-\d+\.(?:video|md)\.part$")
    for path in entries:
        if pattern.fullmatch(path.name):
            try:
                path.unlink()
            except OSError:
                pass


def source_matches_task(path: Path, task: Any) -> bool:
    try:
        metadata = path.lstat()
        return (
            stat.S_ISREG(metadata.st_mode)
            and not stat.S_ISLNK(metadata.st_mode)
            and metadata.st_size == int(task["size_bytes"])
            and metadata.st_mtime_ns == int(task["mtime_ns"])
            and metadata.st_dev == int(task["device"])
            and metadata.st_ino == int(task["inode"])
        )
    except (FileNotFoundError, OSError, KeyError, TypeError, ValueError):
        return False


def input_source_relative_path(
    source: Path,
    input_dir: Path,
) -> Path | None:
    try:
        root = input_dir.resolve(strict=True)
    except OSError:
        return None
    if not source.is_absolute():
        return None
    try:
        relative = source.relative_to(root)
    except ValueError:
        return None
    parts = relative.parts
    if (
        not parts
        or len(parts) - 1 > MAX_INPUT_SUBDIRECTORY_DEPTH
        or any(part in {"", ".", ".."} or part.startswith(".") for part in parts)
    ):
        return None
    current = root
    try:
        for part in parts[:-1]:
            current /= part
            metadata = current.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(
                metadata.st_mode
            ):
                return None
    except OSError:
        return None
    return relative


def quarantine_and_delete_source(
    source: Path, task: Any, input_dir: Path
) -> None:
    if input_source_relative_path(source, input_dir) is None:
        raise MediaError("任务源文件不在 input 允许的三层目录内")
    if not source_matches_task(source, task):
        raise MediaError("输出完成后源文件身份发生变化，拒绝删除")
    task_id = int(task["id"])
    source_parent = source.parent
    quarantine = input_dir / (
        f".vislex-delete-{task_id}-{secrets.token_hex(8)}.part"
    )
    os.rename(source, quarantine)
    fsync_directory(source_parent)
    if source_parent != input_dir:
        fsync_directory(input_dir)
    if not source_matches_task(quarantine, task):
        restored = restore_quarantined_source(quarantine, source, input_dir)
        suffix = "" if restored else f"，文件保留在 {quarantine.name}"
        raise MediaError(f"删除前检测到源文件被并发替换{suffix}")
    try:
        os.unlink(quarantine)
        fsync_directory(input_dir)
    except OSError:
        restore_quarantined_source(quarantine, source, input_dir)
        raise


def owned_source_quarantines(input_dir: Path) -> list[tuple[int, Path]]:
    quarantines: list[tuple[int, Path]] = []
    try:
        entries = list(input_dir.iterdir())
    except OSError:
        return quarantines
    for path in entries:
        matched = SOURCE_QUARANTINE_PATTERN.fullmatch(path.name)
        if matched:
            quarantines.append((int(matched.group("task_id")), path))
    return quarantines


def restore_quarantined_source(
    quarantine: Path, source: Path, input_dir: Path
) -> bool:
    if input_source_relative_path(source, input_dir) is None:
        return False
    if source.exists() or source.is_symlink():
        return False
    try:
        os.link(quarantine, source, follow_symlinks=False)
        os.unlink(quarantine)
        fsync_directory(source.parent)
        if source.parent != input_dir:
            fsync_directory(input_dir)
        return True
    except OSError:
        return False


def safe_flat_path(root: Path, stored: object) -> Path | None:
    if not stored:
        return None
    candidate = Path(str(stored))
    if candidate.name in {"", ".", ".."}:
        return None
    try:
        if candidate.parent.resolve() != root.resolve():
            return None
    except OSError:
        return None
    return candidate


def safe_regular_output(root: Path, stored: object) -> Path | None:
    candidate = safe_flat_path(root, stored)
    if candidate is None:
        return None
    try:
        metadata = candidate.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        return None
    return candidate


def outputs_complete(
    task: Any, video_path: Path | None, markdown_path: Path | None
) -> bool:
    if video_path is None or markdown_path is None:
        return False
    safe_video = safe_regular_output(video_path.parent, video_path)
    safe_markdown = safe_regular_output(markdown_path.parent, markdown_path)
    if safe_video is None or safe_markdown is None:
        return False
    try:
        return (
            safe_video.stat().st_size == int(task["size_bytes"])
            and safe_markdown.stat().st_size > 0
        )
    except (OSError, KeyError, TypeError, ValueError):
        return False


def published_outputs_match(
    *,
    task: Any,
    source_video: Path,
    video_path: Path,
    markdown_path: Path,
    expected_markdown: str,
) -> bool:
    if not outputs_complete(task, video_path, markdown_path):
        return False
    try:
        return (
            _regular_text(markdown_path, 5 * 1024 * 1024) == expected_markdown
            and _same_file_contents(source_video, video_path)
        )
    except (OSError, UnicodeError, MediaError):
        return False


def calculate_output_proof(
    video_path: Path, markdown_path: Path
) -> OutputProof:
    safe_video = safe_regular_output(video_path.parent, video_path)
    safe_markdown = safe_regular_output(markdown_path.parent, markdown_path)
    if safe_video is None or safe_markdown is None:
        raise MediaError("无法为非普通输出文件生成校验值")
    return OutputProof(
        video_sha256=_regular_file_sha256(safe_video),
        md_sha256=_regular_file_sha256(safe_markdown, 5 * 1024 * 1024),
    )


def stored_output_proof_matches(
    task: Any, video_path: Path | None, markdown_path: Path | None
) -> bool:
    if not outputs_complete(task, video_path, markdown_path):
        return False
    expected_video = str(task["video_sha256"] or "")
    expected_markdown = str(task["md_sha256"] or "")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_video) or not re.fullmatch(
        r"[0-9a-f]{64}", expected_markdown
    ):
        return False
    try:
        proof = calculate_output_proof(video_path, markdown_path)
    except (OSError, MediaError):
        return False
    return hmac.compare_digest(
        proof.video_sha256, expected_video
    ) and hmac.compare_digest(proof.md_sha256, expected_markdown)


def read_regular_text(path: Path, maximum_bytes: int) -> str:
    return _regular_text(path, maximum_bytes)


async def stage_output_pair(
    *,
    task_id: int,
    source_video: Path,
    final_video: Path,
    final_markdown: Path,
    markdown: str,
    task: Any,
) -> OutputProof:
    if final_video.parent.resolve() != final_markdown.parent.resolve():
        raise MediaError("输出文件必须位于同一目录")
    output_dir = final_video.parent
    video_temp, markdown_temp = task_temp_paths(output_dir, task_id)
    remove_task_parts(output_dir, task_id)
    markdown_created = False
    video_created = False
    proof: OutputProof | None = None
    try:
        video_sha256 = await _copy_source_to_temp(source_video, video_temp, task)
        md_sha256 = _write_text_temp(markdown_temp, markdown)
        proof = OutputProof(
            video_sha256=video_sha256,
            md_sha256=md_sha256,
        )
        if not source_matches_task(source_video, task):
            raise MediaError("源文件在输出写入前发生了变化")

        if final_markdown.exists() or final_markdown.is_symlink():
            existing = safe_regular_output(output_dir, final_markdown)
            if (
                existing is None
                or _regular_text(existing, 5 * 1024 * 1024) != markdown
            ):
                raise OutputCollisionError("Markdown 输出文件名已被占用")
        else:
            os.link(markdown_temp, final_markdown, follow_symlinks=False)
            markdown_created = True
            fsync_directory(output_dir)

        if final_video.exists() or final_video.is_symlink():
            existing_video = safe_regular_output(output_dir, final_video)
            if (
                existing_video is None
                or not _same_file_contents(source_video, existing_video)
            ):
                raise OutputCollisionError("视频输出文件名已被占用")
        else:
            os.link(video_temp, final_video, follow_symlinks=False)
            video_created = True
            fsync_directory(output_dir)
    except Exception:
        if markdown_created and not video_created:
            try:
                final_markdown.unlink()
                fsync_directory(output_dir)
            except OSError:
                pass
        raise
    finally:
        remove_task_parts(output_dir, task_id)
    if proof is None:
        raise MediaError("未能生成输出校验值")
    return proof


async def _copy_source_to_temp(source: Path, target: Path, task: Any) -> str:
    source_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    target_flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
    )
    digest = hashlib.sha256()
    try:
        source_fd = os.open(source, source_flags)
    except OSError as exc:
        raise MediaError("无法安全打开源视频") from exc
    try:
        before = os.fstat(source_fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size != int(task["size_bytes"])
            or before.st_mtime_ns != int(task["mtime_ns"])
            or before.st_dev != int(task["device"])
            or before.st_ino != int(task["inode"])
        ):
            raise MediaError("源文件在复制开始前发生了变化")
        target_fd = os.open(target, target_flags, 0o600)
        try:
            while True:
                chunk = os.read(source_fd, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(target_fd, view)
                    view = view[written:]
                await asyncio.sleep(0)
            os.fsync(target_fd)
        finally:
            os.close(target_fd)
        after = os.fstat(source_fd)
        if (
            after.st_size != before.st_size
            or after.st_mtime_ns != before.st_mtime_ns
        ):
            raise MediaError("源文件在复制过程中发生了变化")
    finally:
        os.close(source_fd)
    if target.stat().st_size != int(task["size_bytes"]):
        raise MediaError("视频复制后的大小不正确")
    return digest.hexdigest()


def _write_text_temp(path: Path, value: str) -> str:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as target:
        target.write(value)
        target.flush()
        os.fsync(target.fileno())
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _regular_text(path: Path, maximum_bytes: int) -> str:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum_bytes:
            raise MediaError("文本文件不是安全的普通文件")
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > maximum_bytes:
            raise MediaError("文本文件超过安全读取上限")
        return payload.decode("utf-8")
    finally:
        os.close(descriptor)


def _same_file_contents(left: Path, right: Path) -> bool:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    left_descriptor = os.open(left, flags)
    try:
        right_descriptor = os.open(right, flags)
        try:
            left_stat = os.fstat(left_descriptor)
            right_stat = os.fstat(right_descriptor)
            if (
                not stat.S_ISREG(left_stat.st_mode)
                or not stat.S_ISREG(right_stat.st_mode)
                or left_stat.st_size != right_stat.st_size
            ):
                return False
            while True:
                left_chunk = os.read(left_descriptor, 1024 * 1024)
                right_chunk = os.read(right_descriptor, 1024 * 1024)
                if left_chunk != right_chunk:
                    return False
                if not left_chunk:
                    return True
        finally:
            os.close(right_descriptor)
    finally:
        os.close(left_descriptor)


def _regular_file_sha256(path: Path, maximum_bytes: int | None = None) -> str:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    digest = hashlib.sha256()
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise MediaError("输出文件不是普通文件")
        if maximum_bytes is not None and metadata.st_size > maximum_bytes:
            raise MediaError("输出文件超过校验上限")
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def fsync_directory(directory: Path) -> None:
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)
