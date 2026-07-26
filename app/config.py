from __future__ import annotations

import math
import os
from dataclasses import dataclass
from datetime import timedelta, timezone
from pathlib import Path


ARK_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
CHINA_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"环境变量 {name} 必须是整数") from exc
    if value < minimum:
        raise RuntimeError(f"环境变量 {name} 不能小于 {minimum}")
    return value


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"环境变量 {name} 必须是数字") from exc
    if not math.isfinite(value):
        raise RuntimeError(f"环境变量 {name} 必须是有限数字")
    if value < minimum:
        raise RuntimeError(f"环境变量 {name} 不能小于 {minimum:g}")
    return value


def validate_fps(value: str | float) -> float:
    try:
        fps = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("抽帧频率必须是数字") from exc
    if not 0.2 <= fps <= 5:
        raise ValueError("抽帧频率必须在 0.2 至 5 FPS 之间")
    return fps


@dataclass(frozen=True)
class AppConfig:
    input_dir: Path
    output_dir: Path
    data_dir: Path
    database_path: Path
    api_key_path: Path
    trusted_hosts: tuple[str, ...]
    scan_interval_seconds: int = 30
    stable_seconds: int = 60
    max_video_seconds: float = 1800.0
    max_video_bytes: int = 500_000_000
    request_timeout_seconds: float = 600.0
    upload_timeout_seconds: float = 1800.0
    file_poll_seconds: float = 3.0
    file_poll_timeout_seconds: float = 1800.0
    max_output_tokens: int = 32_768

    @classmethod
    def from_environment(cls) -> "AppConfig":
        input_dir = Path(os.getenv("INPUT_DIR", "/app/input")).resolve()
        output_dir = Path(os.getenv("OUTPUT_DIR", "/app/output")).resolve()
        data_dir = Path(os.getenv("DATA_DIR", "/app/data")).resolve()
        hosts = tuple(
            part.strip()
            for part in os.getenv("TRUSTED_HOSTS", "127.0.0.1,localhost").split(",")
            if part.strip()
        )
        if not hosts:
            raise RuntimeError("TRUSTED_HOSTS 不能为空")
        return cls(
            input_dir=input_dir,
            output_dir=output_dir,
            data_dir=data_dir,
            database_path=data_dir / "vislex.sqlite3",
            api_key_path=data_dir / "ark_api_key",
            trusted_hosts=hosts,
            scan_interval_seconds=_env_int("SCAN_INTERVAL_SECONDS", 30),
            stable_seconds=_env_int("FILE_STABLE_SECONDS", 60),
            max_video_seconds=_env_float("MAX_VIDEO_SECONDS", 1800.0),
            max_video_bytes=_env_int("MAX_VIDEO_BYTES", 500_000_000),
            request_timeout_seconds=_env_float(
                "ARK_REQUEST_TIMEOUT_SECONDS", 600.0, 1.0
            ),
            upload_timeout_seconds=_env_float(
                "ARK_UPLOAD_TIMEOUT_SECONDS", 1800.0, 1.0
            ),
            file_poll_seconds=_env_float("ARK_FILE_POLL_SECONDS", 3.0, 0.2),
            file_poll_timeout_seconds=_env_float(
                "ARK_FILE_POLL_TIMEOUT_SECONDS", 1800.0, 1.0
            ),
            max_output_tokens=_env_int("ARK_MAX_OUTPUT_TOKENS", 32_768),
        )

    def ensure_directories(self) -> None:
        for directory in (self.input_dir, self.output_dir, self.data_dir):
            directory.mkdir(parents=True, exist_ok=True)
