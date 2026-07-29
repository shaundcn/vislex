from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from typing import Sequence


DEFAULT_PUID = 10001
DEFAULT_PGID = 10001
MAX_ID = 2_147_483_647
MOUNT_DIRECTORIES = (
    Path("/app/input"),
    Path("/app/output"),
    Path("/app/data"),
)
DATA_DIRECTORY = Path("/app/data")
DATA_FILENAMES = {
    "ark_api_key",
    "vislex.sqlite3",
    "vislex.sqlite3-journal",
    "vislex.sqlite3-shm",
    "vislex.sqlite3-wal",
}


class EntrypointError(RuntimeError):
    pass


def _environment_id(name: str, default: int) -> int:
    raw_value = os.getenv(name, str(default)).strip()
    if not raw_value or not raw_value.isascii() or not raw_value.isdecimal():
        raise EntrypointError(f"{name} 必须是非负整数")
    value = int(raw_value)
    if value > MAX_ID:
        raise EntrypointError(f"{name} 不能大于 {MAX_ID}")
    return value


def configured_identity() -> tuple[int, int]:
    return (
        _environment_id("PUID", DEFAULT_PUID),
        _environment_id("PGID", DEFAULT_PGID),
    )


def _open_directory(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise EntrypointError(
            f"无法打开挂载目录 {path}：{exc.strerror or type(exc).__name__}"
        ) from exc
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        os.close(descriptor)
        raise EntrypointError(f"挂载路径不是目录：{path}")
    return descriptor


def _set_directory_owner(descriptor: int, puid: int, pgid: int) -> None:
    metadata = os.fstat(descriptor)
    os.fchown(descriptor, puid, pgid)
    os.fchmod(descriptor, stat.S_IMODE(metadata.st_mode) | 0o700)


def _is_owned_data_file(name: str) -> bool:
    return name in DATA_FILENAMES or (
        name.startswith(".ark_api_key.") and name.endswith(".part")
    )


def _set_data_file_owner(
    directory_descriptor: int,
    name: str,
    puid: int,
    pgid: int,
) -> None:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=directory_descriptor)
    except OSError as exc:
        raise EntrypointError(
            f"无法打开应用数据文件 {name}："
            f"{exc.strerror or type(exc).__name__}"
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise EntrypointError(f"拒绝修改非普通应用数据文件：{name}")
        os.fchown(descriptor, puid, pgid)
        mode = (
            0o600
            if name == "ark_api_key"
            or (name.startswith(".ark_api_key.") and name.endswith(".part"))
            else stat.S_IMODE(metadata.st_mode) | 0o600
        )
        os.fchmod(descriptor, mode)
    finally:
        os.close(descriptor)


def prepare_mounts(puid: int, pgid: int) -> None:
    descriptors: dict[Path, int] = {}
    try:
        for path in MOUNT_DIRECTORIES:
            descriptor = _open_directory(path)
            descriptors[path] = descriptor
            _set_directory_owner(descriptor, puid, pgid)

        data_descriptor = descriptors[DATA_DIRECTORY]
        with os.scandir(data_descriptor) as entries:
            for entry in entries:
                if not _is_owned_data_file(entry.name):
                    continue
                if entry.is_symlink():
                    raise EntrypointError(
                        f"拒绝修改符号链接应用数据文件：{entry.name}"
                    )
                _set_data_file_owner(
                    data_descriptor,
                    entry.name,
                    puid,
                    pgid,
                )
    except OSError as exc:
        raise EntrypointError(
            "无法设置挂载目录权限："
            f"{exc.strerror or type(exc).__name__}"
        ) from exc
    finally:
        for descriptor in descriptors.values():
            os.close(descriptor)


def drop_privileges(puid: int, pgid: int) -> None:
    current_uid = os.geteuid()
    current_gid = os.getegid()
    if current_uid != 0:
        if (current_uid, current_gid) != (puid, pgid):
            raise EntrypointError(
                "入口进程不是 root，无法切换到 "
                f"{puid}:{pgid}；请移除 Compose 的 user 配置"
            )
        return

    try:
        os.setgroups([])
        os.setgid(pgid)
        os.setuid(puid)
    except OSError as exc:
        raise EntrypointError(
            f"无法切换到 {puid}:{pgid}："
            f"{exc.strerror or type(exc).__name__}"
        ) from exc


def verify_mount_access(puid: int, pgid: int) -> None:
    required = os.R_OK | os.W_OK | os.X_OK
    for path in MOUNT_DIRECTORIES:
        if not os.access(path, required):
            raise EntrypointError(
                f"PUID/PGID {puid}:{pgid} 无法读写挂载目录 {path}"
            )


def run(command: Sequence[str]) -> None:
    if not command:
        raise EntrypointError("没有可执行的启动命令")

    puid, pgid = configured_identity()
    if os.geteuid() == 0:
        prepare_mounts(puid, pgid)
    drop_privileges(puid, pgid)
    verify_mount_access(puid, pgid)

    if puid == 0:
        print(
            "Vislex 警告：PUID=0，应用将以 root 身份运行。",
            file=sys.stderr,
            flush=True,
        )
    print(f"Vislex 运行用户：{puid}:{pgid}", flush=True)
    os.execvp(command[0], list(command))


def main() -> int:
    try:
        run(sys.argv[1:])
    except EntrypointError as exc:
        print(f"Vislex 启动失败：{exc}", file=sys.stderr, flush=True)
        return 78
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
