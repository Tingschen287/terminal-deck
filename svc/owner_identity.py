"""写回入口的调用者身份判定（n077）：只有 owner 界面能经写回 socket 回答决定。

沿用 deckd（work/internal/identity）的思路，按「SwiftUI 客户端 + 本机服务」重审：

1. socket 权限：写回 socket 在 0700 目录里、本身 0600，只有本用户能连。
2. 对端凭据（macOS）：LOCAL_PEERCRED 的 uid、LOCAL_PEERTOKEN 审计令牌里的 euid 都必须是本用户；
   令牌里的 pid 与 LOCAL_PEERPID 一致。
3. 生命周期绑定：令牌里的 pidversion 是「建立连接的那个进程」的代际号；判定前后各核对一次
   proc_pidinfo(PROC_PIDUNIQIDENTIFIERINFO) 读到的代际号，pid 被复用或对端 exec 过就拒绝。
4. 主判定——可执行文件白名单：对端进程的可执行文件（proc_pidpath，取 realpath）必须是服务启动时
   用 --owner-client 登记的 owner 界面程序之一；登记时记下 (dev, inode, 大小, mtime)，之后文件被换掉就拒绝。
   主管（Claude Code、codex、opencode……）与分身是别的程序，直接连 socket 一律被拒。
5. 辅助判定——进程链：从对端沿父进程向上到 launchd，任何一层是已知 Agent CLI（按可执行文件路径与
   argv 判定）就拒绝。它挡的是「主管或分身把 owner 界面程序当子进程拉起来」这种直接做法；
   双 fork 脱离进程链可以绕过它，所以它只是辅助，主判定是第 4 条，前提是 owner 界面程序没有
   无界面的答复入口（不接受命令行参数、URL scheme、AppleScript 代为答复）。

与 deckd 相同的边界：防的是模型越权与误操作；同一系统用户下的恶意代码（ptrace、注入 owner 界面进程、
替换后重启服务）防不住。非 macOS 平台一律拒绝（fail closed）。
"""

from __future__ import annotations

import ctypes
import os
import socket
import stat
import struct
import sys
from dataclasses import dataclass
from typing import Callable


SOL_LOCAL = 0
LOCAL_PEERCRED = 0x001
LOCAL_PEERPID = 0x002
LOCAL_PEERTOKEN = 0x006
XUCRED_SIZE = 76
AUDIT_TOKEN_SIZE = 32
PROC_PIDTBSDINFO = 3
PROC_PIDTBSDINFO_SIZE = 136
PROC_PIDUNIQIDENTIFIERINFO = 17
PROC_UNIQIDENTIFIERINFO_SIZE = 56
PROC_PIDPATHINFO_MAXSIZE = 4096
CTL_KERN = 1
KERN_PROCARGS2 = 49

AGENT_NAMES = frozenset(
    {
        "claude", "codex", "grok", "opencode", "kimi", "agy", "gemini", "aider", "cursor-agent",
        "goose", "amp", "qwen", "crush", "droid",
    }
)
AGENT_PATH_MARKERS = (
    "/claude/versions/", "/@anthropic-ai/claude-code/", "/@openai/codex/", "/opencode/bin/",
)
INTERPRETERS = ("node", "bun", "deno", "python", "python3", "ruby", "perl")


class IdentityError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class RegisteredClient:
    path: str
    dev: int
    ino: int
    size: int
    mtime_ns: int


def register_client(path: str) -> RegisteredClient:
    """登记一个 owner 界面程序：绝对路径、普通文件、可执行、组和其他人不可写。"""
    if not os.path.isabs(path):
        raise IdentityError("owner_client_invalid")
    real = os.path.realpath(path)
    try:
        info = os.stat(real)
    except OSError as exc:
        raise IdentityError("owner_client_invalid") from exc
    if not stat.S_ISREG(info.st_mode) or not os.access(real, os.X_OK) or info.st_mode & 0o022:
        raise IdentityError("owner_client_invalid")
    return RegisteredClient(real, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


class _Darwin:
    def __init__(self) -> None:
        self.proc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        self.proc.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        self.proc.proc_pidpath.restype = ctypes.c_int
        self.proc.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
        self.proc.proc_pidinfo.restype = ctypes.c_int
        self.libc = ctypes.CDLL(None, use_errno=True)
        self.libc.sysctl.argtypes = [
            ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t),
            ctypes.c_void_p, ctypes.c_size_t,
        ]
        self.libc.sysctl.restype = ctypes.c_int

    def pid_version(self, pid: int) -> int:
        buffer = ctypes.create_string_buffer(PROC_UNIQIDENTIFIERINFO_SIZE)
        size = self.proc.proc_pidinfo(pid, PROC_PIDUNIQIDENTIFIERINFO, 0, buffer, PROC_UNIQIDENTIFIERINFO_SIZE)
        if size != PROC_UNIQIDENTIFIERINFO_SIZE:
            raise IdentityError("identity_unavailable")
        return struct.unpack_from("<I", buffer.raw, 32)[0]

    def parent(self, pid: int) -> int:
        buffer = ctypes.create_string_buffer(PROC_PIDTBSDINFO_SIZE)
        size = self.proc.proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, buffer, PROC_PIDTBSDINFO_SIZE)
        if size != PROC_PIDTBSDINFO_SIZE:
            raise IdentityError("identity_unavailable")
        return struct.unpack_from("<I", buffer.raw, 16)[0]

    def path(self, pid: int) -> str:
        buffer = ctypes.create_string_buffer(PROC_PIDPATHINFO_MAXSIZE)
        size = self.proc.proc_pidpath(pid, buffer, PROC_PIDPATHINFO_MAXSIZE)
        if size <= 0:
            raise IdentityError("identity_unavailable")
        return os.fsdecode(buffer.raw[:size])

    def argv(self, pid: int, limit: int = 2) -> list[str]:
        """KERN_PROCARGS2 的前几个参数；读不到返回空列表（只作辅助判定）。"""
        mib = (ctypes.c_int * 3)(CTL_KERN, KERN_PROCARGS2, pid)
        size = ctypes.c_size_t(0)
        if self.libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0 or size.value < 4:
            return []
        buffer = ctypes.create_string_buffer(size.value)
        if self.libc.sysctl(mib, 3, buffer, ctypes.byref(size), None, 0) != 0:
            return []
        raw = buffer.raw[: size.value]
        argc = struct.unpack_from("<i", raw, 0)[0]
        cursor = raw.find(b"\0", 4)
        if cursor < 0:
            return []
        while cursor < len(raw) and raw[cursor] == 0:
            cursor += 1
        result = []
        for _ in range(max(0, min(argc, limit))):
            end = raw.find(b"\0", cursor)
            if end < 0:
                break
            result.append(os.fsdecode(raw[cursor:end]))
            cursor = end + 1
        return result


def is_agent_process(path: str, argv: list[str]) -> bool:
    lowered = path.lower()
    if os.path.basename(lowered) in AGENT_NAMES or any(marker in lowered for marker in AGENT_PATH_MARKERS):
        return True
    names = [os.path.basename(a).lower() for a in argv]
    if names and names[0] in AGENT_NAMES:
        return True
    if len(argv) >= 2 and (names[0].startswith(INTERPRETERS) or os.path.basename(lowered).startswith(INTERPRETERS)):
        script = names[1]
        for suffix in (".js", ".mjs", ".cjs", ".py"):
            if script.endswith(suffix):
                script = script[: -len(suffix)]
        if script in AGENT_NAMES or any(marker in argv[1].lower() for marker in AGENT_PATH_MARKERS):
            return True
    return False


class OwnerClientPolicy:
    """对一条已接受的连接做身份判定；通过返回对端 pid，否则抛 IdentityError（固定码）。"""

    def __init__(self, clients: list[str], *, platform: str | None = None) -> None:
        if not clients:
            raise IdentityError("owner_client_invalid")
        self.clients = {c.path: c for c in (register_client(path) for path in clients)}
        self.platform = platform or sys.platform
        self._darwin = _Darwin() if self.platform == "darwin" else None

    def check(self, connection: socket.socket) -> int:
        if self._darwin is None:
            raise IdentityError("identity_unsupported_platform")
        darwin = self._darwin
        try:
            cred = connection.getsockopt(SOL_LOCAL, LOCAL_PEERCRED, XUCRED_SIZE)
            pid = struct.unpack("<i", connection.getsockopt(SOL_LOCAL, LOCAL_PEERPID, 4))[0]
            token = struct.unpack("<8I", connection.getsockopt(SOL_LOCAL, LOCAL_PEERTOKEN, AUDIT_TOKEN_SIZE))
        except (OSError, struct.error) as exc:
            raise IdentityError("identity_unavailable") from exc
        version, uid = struct.unpack_from("<II", cred, 0)
        euid = os.geteuid()
        if version != 0 or uid != euid or token[1] != euid:
            raise IdentityError("caller_uid_mismatch")
        if pid <= 1 or token[5] != pid:
            raise IdentityError("identity_unavailable")
        bound_version = token[7]

        def same() -> None:
            if darwin.pid_version(pid) != bound_version:
                raise IdentityError("identity_unavailable")

        same()
        executable = os.path.realpath(darwin.path(pid))
        client = self.clients.get(executable)
        if client is None:
            raise IdentityError("caller_not_owner_client")
        try:
            info = os.stat(client.path)
        except OSError as exc:
            raise IdentityError("owner_client_changed") from exc
        if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != (client.dev, client.ino, client.size, client.mtime_ns):
            raise IdentityError("owner_client_changed")
        self._check_ancestry(pid)
        same()
        return pid

    def _check_ancestry(self, pid: int) -> None:
        darwin = self._darwin
        assert darwin is not None
        seen = set()
        current = darwin.parent(pid)
        while current > 1:
            if current in seen or len(seen) > 256:
                raise IdentityError("identity_unavailable")
            seen.add(current)
            try:
                path = darwin.path(current)
                parent = darwin.parent(current)
            except IdentityError:
                break  # 祖先恰好退出：链到此为止（对端自身的生命周期另有绑定核对）
            if is_agent_process(path, darwin.argv(current)):
                raise IdentityError("owner_client_under_agent")
            current = parent


CheckFunction = Callable[[socket.socket], int]
