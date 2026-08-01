from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


TITLE_PATTERN = re.compile(
    r"^[A-Za-z0-9\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF"
    r"\U00020000-\U0002EBEF]+$"
)
VIDEO_FILENAME_EXTENSIONS = (
    ".mp4",
    ".mov",
    ".m4v",
    ".avi",
    ".mkv",
    ".webm",
    ".wmv",
    ".flv",
    ".mpeg",
    ".mpg",
    ".ts",
)
MODEL_OUTPUT_FIELDS = ("title", "content", "transcript")


def normalize_model_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = {
        field: payload[field] for field in MODEL_OUTPUT_FIELDS if field in payload
    }
    if "new_filename" in payload:
        normalized["new_filename"] = payload["new_filename"]
    title = normalized.get("title")
    if isinstance(title, str):
        candidate = title.strip()
        unsafe = any(
            character in {"/", "\\"}
            or ord(character) < 32
            or ord(character) == 127
            for character in title
        )
        if not unsafe:
            lowered = candidate.casefold()
            for extension in VIDEO_FILENAME_EXTENSIONS:
                if lowered.endswith(extension) and len(candidate) > len(extension):
                    candidate = candidate[: -len(extension)].rstrip()
                    break
            candidate = "".join(
                character
                for character in candidate
                if TITLE_PATTERN.fullmatch(character)
            )
            normalized["title"] = candidate[:20]

    transcript = normalized.get("transcript")
    if isinstance(transcript, str):
        normalized["transcript"] = [transcript]
    elif isinstance(transcript, list):
        normalized["transcript"] = [
            item
            for item in transcript
            if not isinstance(item, str) or item.strip()
        ]
    return normalized


class VideoAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    title: str = Field(min_length=1, max_length=20)
    content: str = Field(min_length=1)
    transcript: list[str]

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("title 首尾不能包含空白")
        if not TITLE_PATTERN.fullmatch(value):
            raise ValueError("title 只能包含中文、英文字母或数字")
        return value

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not normalized:
            raise ValueError("content 不能为空")
        return normalized

    @field_validator("transcript")
    @classmethod
    def validate_transcript(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in value:
            normalized = item.replace("\r\n", "\n").replace("\r", "\n").strip()
            if not normalized:
                raise ValueError("transcript 不能包含空字符串")
            cleaned.append(normalized)
        return cleaned


def validation_error_text(error: Any) -> str:
    entries = getattr(error, "errors", lambda: [])()
    if not entries:
        return str(error)
    messages: list[str] = []
    for entry in entries:
        location = ".".join(str(part) for part in entry.get("loc", ()))
        message = str(entry.get("msg") or "格式错误")
        messages.append(f"{location}: {message}" if location else message)
    return "; ".join(messages)
