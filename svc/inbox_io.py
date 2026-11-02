"""只读地取收件箱快照（n077）。读取服务与写回服务共用；本模块不写盘。"""

from __future__ import annotations

from dataclasses import dataclass
import os
import stat

from inbox import MAX_INBOX_BYTES, InboxError, sha256_hex


@dataclass(frozen=True)
class Snapshot:
    path: str
    data: bytes
    sha256: str
    size: int
    mtime_ns: int
    mode: int
    dev: int
    ino: int


def read_snapshot(path: str) -> Snapshot:
    """读整份文件。软链、非普通文件、多硬链接都拒绝（写回要换文件，这些情况换了会改变别处的语义）。"""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
    except FileNotFoundError as exc:
        raise InboxError("inbox_unreadable") from exc
    except OSError as exc:
        # O_NOFOLLOW 碰到软链返回 ELOOP（macOS 也可能是 EMLINK）。
        raise InboxError("inbox_unsafe" if os.path.islink(path) else "inbox_unreadable") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise InboxError("inbox_unsafe")
        if info.st_size > MAX_INBOX_BYTES:
            raise InboxError("inbox_too_large")
        chunks = []
        remaining = MAX_INBOX_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > MAX_INBOX_BYTES:
            raise InboxError("inbox_too_large")
    finally:
        os.close(fd)
    return Snapshot(
        path=path,
        data=data,
        sha256=sha256_hex(data),
        size=len(data),
        mtime_ns=info.st_mtime_ns,
        mode=stat.S_IMODE(info.st_mode),
        dev=info.st_dev,
        ino=info.st_ino,
    )
