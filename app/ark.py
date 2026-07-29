from __future__ import annotations

import asyncio
import json
import math
import mimetypes
import os
import random
import re
import stat
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from .config import ARK_BASE_URL, AppConfig, validate_fps
from .media import fsync_directory


class ArkError(RuntimeError):
    pass


class ArkJSONError(ArkError):
    pass


class ArkResponseIncomplete(ArkError):
    pass


READY_FILE_STATUSES = {
    "active",
    "available",
    "completed",
    "done",
    "processed",
    "ready",
    "succeeded",
    "success",
}
FAILED_FILE_STATUSES = {"cancelled", "error", "expired", "failed", "rejected"}
PENDING_FILE_STATUSES = {
    "created",
    "in_progress",
    "pending",
    "processing",
    "queued",
    "uploaded",
    "uploading",
}
FENCED_JSON_PATTERN = re.compile(
    r"\A```(?:json)?[ \t]*\r?\n(?P<body>.*?)\r?\n```[ \t]*\Z",
    re.IGNORECASE | re.DOTALL,
)


def read_api_key(config: AppConfig) -> str | None:
    try:
        metadata = config.api_key_path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ArkError("API Key 文件必须是 data 中的普通文件")
    try:
        descriptor = os.open(
            config.api_key_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        )
    except OSError as exc:
        raise ArkError("无法安全读取 API Key 文件") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != metadata.st_dev
            or opened.st_ino != metadata.st_ino
        ):
            raise ArkError("API Key 文件在读取时发生变化")
        if stat.S_IMODE(opened.st_mode) != 0o600:
            raise ArkError("API Key 文件权限必须是 0600")
        try:
            with os.fdopen(
                descriptor, "r", encoding="utf-8", closefd=False
            ) as source:
                value = source.read(8193)
        except UnicodeError as exc:
            raise ArkError("API Key 文件不是有效 UTF-8 文本") from exc
        if len(value) > 8192:
            raise ArkError("API Key 文件过大")
    finally:
        os.close(descriptor)
    value = value.strip()
    return value or None


def save_api_key(config: AppConfig, api_key: str) -> None:
    value = api_key.strip()
    if not value:
        raise ArkError("API Key 不能为空")
    if len(value) > 4096:
        raise ArkError("API Key 过长")
    config.data_dir.mkdir(parents=True, exist_ok=True)
    try:
        existing = config.api_key_path.lstat()
        if stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode):
            raise ArkError("拒绝覆盖非普通 API Key 文件")
    except FileNotFoundError:
        pass

    temporary = config.data_dir / f".ark_api_key.{os.getpid()}.part"
    try:
        temporary.unlink(missing_ok=True)
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as target:
            target.write(value)
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, config.api_key_path)
        os.chmod(config.api_key_path, 0o600)
        fsync_directory(config.data_dir)
    finally:
        temporary.unlink(missing_ok=True)


def masked_api_key(config: AppConfig) -> str:
    key = read_api_key(config)
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}{'*' * (len(key) - 8)}{key[-4:]}"


class ArkClient:
    def __init__(
        self,
        config: AppConfig,
        api_key: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.config = config
        self.api_key = api_key.strip()
        if not self.api_key:
            raise ArkError("API Key 不能为空")
        self.client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {self.api_key}"},
            follow_redirects=False,
            transport=transport,
        )
        self.last_uploaded_file_id: str | None = None

    async def __aenter__(self) -> "ArkClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self.client.aclose()

    async def list_models(self) -> list[str]:
        models: list[str] = []
        path = "/models?limit=100"
        for _ in range(100):
            payload = await self._request_json(
                "GET", path, operation="获取可用模型列表"
            )
            items = _model_items(payload)
            for item in items:
                identifier = item.get("id") if isinstance(item, dict) else item
                if isinstance(identifier, str) and identifier:
                    models.append(identifier)
            if not payload.get("has_more"):
                break
            cursor = str(
                payload.get("last_id")
                or payload.get("next_cursor")
                or _last_model_id(items)
                or ""
            )
            if not cursor:
                raise ArkError("模型列表声明还有下一页，但未返回分页游标")
            path = f"/models?{urlencode({'after': cursor, 'limit': 100})}"
        else:
            raise ArkError("模型列表分页超过安全上限")
        if not models:
            raise ArkError("方舟没有返回可选择的模型")
        return models

    async def upload_file(
        self,
        path: Path,
        model_id: str,
        video_fps: float,
        *,
        expected_task: Any | None = None,
    ) -> dict[str, Any]:
        model = validate_model_id(model_id)
        fps = validate_fps(video_fps)
        data = {
            "purpose": "user_data",
            "model": model,
            "preprocess_configs[video][fps]": f"{fps:g}",
        }
        last_error: Exception | None = None
        for attempt in range(4):
            try:
                descriptor = os.open(
                    path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                )
                with os.fdopen(descriptor, "rb") as source:
                    before = os.fstat(source.fileno())
                    if not stat.S_ISREG(before.st_mode) or not _matches_expected(
                        before, expected_task
                    ):
                        raise ArkError("源文件在上传前发生了变化")
                    response = await self.client.post(
                        f"{ARK_BASE_URL}/files",
                        data=data,
                        files={
                            "file": (
                                path.name,
                                source,
                                _video_media_type(path),
                            )
                        },
                        timeout=httpx.Timeout(self.config.upload_timeout_seconds),
                    )
                    after = os.fstat(source.fileno())
                    source_changed = (
                        after.st_size != before.st_size
                        or after.st_mtime_ns != before.st_mtime_ns
                    )
                if _should_retry(response) and attempt < 3:
                    if source_changed:
                        raise ArkError("源文件在上传过程中发生了变化")
                    await _retry_delay(attempt, response)
                    continue
                payload = self._json_or_raise(response, "上传视频")
                file_id = _file_id(payload)
                if file_id:
                    self.last_uploaded_file_id = file_id
                if source_changed:
                    raise ArkError("源文件在上传过程中发生了变化")
                if not file_id:
                    raise ArkError("File API 响应中没有文件 ID")
                return payload
            except httpx.TransportError as exc:
                last_error = exc
                if attempt >= 3:
                    break
                await _retry_delay(attempt)
        raise ArkError("上传视频失败：网络错误（已重试3次）") from last_error

    async def wait_until_file_ready(self, upload_payload: dict[str, Any]) -> str:
        file_id = _file_id(upload_payload)
        if not file_id:
            raise ArkError("File API 响应中没有文件 ID")
        initial = str(_unwrap_data(upload_payload).get("status") or "").lower()
        if initial in READY_FILE_STATUSES:
            return file_id
        if initial in FAILED_FILE_STATUSES:
            raise ArkError(f"视频预处理失败，状态：{initial}")
        if initial and initial not in PENDING_FILE_STATUSES:
            raise ArkError(f"File API 返回未知文件状态：{initial}")

        try:
            async with asyncio.timeout(self.config.file_poll_timeout_seconds):
                while True:
                    payload = await self._request_json(
                        "GET",
                        f"/files/{file_id}",
                        operation="查询视频预处理状态",
                    )
                    current = _unwrap_data(payload)
                    status_value = str(current.get("status") or "").lower()
                    if status_value in READY_FILE_STATUSES:
                        return file_id
                    if status_value in FAILED_FILE_STATUSES:
                        raise ArkError(f"视频预处理失败，状态：{status_value}")
                    if not status_value:
                        raise ArkError("File API 查询响应中没有文件状态")
                    if status_value not in PENDING_FILE_STATUSES:
                        raise ArkError(
                            f"File API 返回未知文件状态：{status_value}"
                        )
                    await asyncio.sleep(self.config.file_poll_seconds)
        except TimeoutError as exc:
            raise ArkError("等待视频预处理超时") from exc

    async def create_video_response(
        self, file_id: str, prompt: str, model_id: str
    ) -> str:
        payload = {
            "model": validate_model_id(model_id),
            "max_output_tokens": self.config.max_output_tokens,
            "thinking": {"type": "disabled"},
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_video", "file_id": file_id},
                        {"type": "input_text", "text": prompt},
                    ],
                }
            ],
        }
        response = await self._request_json(
            "POST",
            "/responses",
            json_body=payload,
            operation="调用视频理解模型",
        )
        _raise_if_incomplete(response)
        text = _extract_output_text(response).strip()
        if not text:
            raise ArkError("Responses API 没有返回文本内容")
        return text

    async def delete_file(self, file_id: str) -> bool:
        if not file_id:
            return True
        try:
            response = await self.client.delete(
                f"{ARK_BASE_URL}/files/{file_id}",
                timeout=httpx.Timeout(5.0),
            )
            if response.status_code in {200, 202, 204, 404}:
                return True
            return False
        except httpx.HTTPError:
            return False

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        operation: str,
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(4):
            try:
                response = await self.client.request(
                    method,
                    f"{ARK_BASE_URL}{path}",
                    json=json_body,
                    timeout=httpx.Timeout(self.config.request_timeout_seconds),
                )
                if _should_retry(response) and attempt < 3:
                    await _retry_delay(attempt, response)
                    continue
                return self._json_or_raise(response, operation)
            except httpx.TransportError as exc:
                last_error = exc
                if attempt >= 3:
                    break
                await _retry_delay(attempt)
        raise ArkError(f"{operation}失败：网络错误（已重试3次）") from last_error

    def _json_or_raise(
        self, response: httpx.Response, operation: str
    ) -> dict[str, Any]:
        if response.status_code == 204:
            return {}
        try:
            payload = response.json()
        except ValueError as exc:
            if response.is_error:
                raise ArkError(f"{operation}失败：HTTP {response.status_code}") from exc
            raise ArkError(f"{operation}失败：服务返回的不是 JSON") from exc
        if response.is_error:
            message = _remote_error_message(payload)
            message = _redact(message, self.api_key)[:800]
            suffix = f"：{message}" if message else ""
            raise ArkError(f"{operation}失败：HTTP {response.status_code}{suffix}")
        if not isinstance(payload, dict):
            raise ArkError(f"{operation}失败：服务返回格式不正确")
        return payload


def parse_model_json(text: str) -> dict[str, Any]:
    candidate = text.strip()
    fenced = FENCED_JSON_PATTERN.fullmatch(candidate)
    if fenced is not None:
        candidate = fenced.group("body").strip()
    try:
        payload = json.loads(candidate, object_pairs_hook=_reject_duplicate_keys)
    except ArkJSONError:
        raise
    except json.JSONDecodeError as exc:
        raise ArkJSONError(
            f"模型返回的不是合法 JSON（第 {exc.lineno} 行，第 {exc.colno} 列）"
        ) from exc
    if not isinstance(payload, dict):
        raise ArkJSONError("模型返回的 JSON 顶层必须是对象")
    return payload


async def create_smoke_test_video(target: Path) -> None:
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "color=c=black:s=320x240:r=10:d=1",
        "-f",
        "lavfi",
        "-i",
        "anullsrc=channel_layout=mono:sample_rate=16000",
        "-shortest",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-movflags",
        "+faststart",
        "-t",
        "1",
        str(target),
    ]
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise ArkError("容器中找不到 FFmpeg") from exc
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
    except TimeoutError as exc:
        process.kill()
        await process.wait()
        raise ArkError("生成测试视频超时") from exc
    if process.returncode != 0 or not target.is_file():
        detail = stderr.decode("utf-8", errors="replace").strip()[-300:]
        raise ArkError(f"生成测试视频失败：{detail or 'FFmpeg 未生成文件'}")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ArkJSONError(f"模型 JSON 包含重复字段：{key}")
        result[key] = value
    return result


def _raise_if_incomplete(payload: dict[str, Any]) -> None:
    status_value = str(payload.get("status") or "").lower()
    if status_value != "completed":
        detail = payload.get("incomplete_details") or payload.get("error") or status_value
        raise ArkResponseIncomplete(f"模型响应未完整生成：{str(detail)[:400]}")
    if payload.get("incomplete_details"):
        raise ArkResponseIncomplete(
            f"模型响应未完整生成：{str(payload['incomplete_details'])[:400]}"
        )
    for output in payload.get("output") or []:
        if not isinstance(output, dict):
            continue
        output_status = str(output.get("status") or "").lower()
        if output_status and output_status != "completed":
            raise ArkResponseIncomplete(f"模型输出状态为 {output_status}")


def _extract_output_text(payload: dict[str, Any]) -> str:
    direct = payload.get("output_text")
    if isinstance(direct, str):
        return direct
    if isinstance(direct, list):
        return "".join(str(item) for item in direct)
    chunks: list[str] = []
    for output in payload.get("output") or []:
        if not isinstance(output, dict):
            continue
        for content in output.get("content") or []:
            if not isinstance(content, dict):
                continue
            if content.get("type") in {"output_text", "text"} and isinstance(
                content.get("text"), str
            ):
                chunks.append(content["text"])
    return "".join(chunks)


def validate_model_id(value: str) -> str:
    model = value.strip()
    if not model:
        raise ArkError("模型不能为空")
    if len(model) > 1024 or any(ord(character) < 32 for character in model):
        raise ArkError("模型 ID 格式不正确")
    return model


def _model_items(payload: dict[str, Any]) -> list[Any]:
    data = payload.get("data")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("items", "models", "data"):
            items = data.get(key)
            if isinstance(items, list):
                return items
    models = payload.get("models")
    return models if isinstance(models, list) else []


def _last_model_id(items: list[Any]) -> str:
    if not items:
        return ""
    item = items[-1]
    if isinstance(item, dict) and isinstance(item.get("id"), str):
        return item["id"]
    return item if isinstance(item, str) else ""


def _unwrap_data(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    return data if isinstance(data, dict) else payload


def _file_id(payload: dict[str, Any]) -> str:
    data = _unwrap_data(payload)
    return str(data.get("id") or data.get("file_id") or "").strip()


def _remote_error_message(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error.get("code") or "")
    return str(error or payload.get("message") or "")


def _redact(value: str, api_key: str) -> str:
    redacted = value.replace(api_key, "***") if api_key else value
    return re.sub(
        r"(?i)(authorization\s*:\s*bearer|bearer)\s+[^\s,;]+",
        r"\1 ***",
        redacted,
    )


def _video_media_type(path: Path) -> str:
    overrides = {".avi": "video/avi", ".mov": "video/quicktime", ".mp4": "video/mp4"}
    return overrides.get(
        path.suffix.casefold(),
        mimetypes.guess_type(path.name)[0] or "application/octet-stream",
    )


def _matches_expected(metadata: os.stat_result, task: Any | None) -> bool:
    if task is None:
        return True
    try:
        return (
            metadata.st_size == int(task["size_bytes"])
            and metadata.st_mtime_ns == int(task["mtime_ns"])
            and metadata.st_dev == int(task["device"])
            and metadata.st_ino == int(task["inode"])
        )
    except (KeyError, TypeError, ValueError):
        return False


def _should_retry(response: httpx.Response) -> bool:
    return response.status_code == 429 or 500 <= response.status_code < 600


async def _retry_delay(
    attempt: int, response: httpx.Response | None = None
) -> None:
    retry_after: float | None = None
    if response is not None:
        raw_retry_after = response.headers.get("Retry-After", "").strip()
        try:
            parsed_seconds = float(raw_retry_after)
            if math.isfinite(parsed_seconds):
                retry_after = max(parsed_seconds, 0.0)
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(raw_retry_after)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=UTC)
                retry_after = max(
                    (retry_at - datetime.now(UTC)).total_seconds(), 0.0
                )
            except (TypeError, ValueError, OverflowError):
                retry_after = None
    delay = retry_after if retry_after is not None else 2**attempt + random.random()
    await asyncio.sleep(max(delay, 0.2))
