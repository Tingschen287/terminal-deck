"""写回入口的调用者身份判定（n077 attempt 2）：只有 owner 界面能经写回 socket 回答决定。

判定以 macOS 代码签名为准（owner 2026-11-02 选 A，「R3 按你建议」）：

1. socket 权限：写回 socket 在 0700 目录里、本身 0600，只有本用户能连。
2. 对端凭据：LOCAL_PEERCRED 的 uid、LOCAL_PEERTOKEN 审计令牌里的 euid 都必须是本用户；令牌里的 pid 与 LOCAL_PEERPID 一致。
3. **主判定——代码签名**：用连接的审计令牌（建立连接那一刻的 pid + pidversion）向 Security 框架取对端的
   SecCode（``SecCodeCopyGuestWithAttributes`` + ``kSecGuestAttributeAudit``），再用 ``SecCodeCheckValidity`` 校验：
   签名有效（运行中的代码与磁盘上的签名都没被改）且满足登记的 designated requirement。
   - 登记方式：服务启动时 ``--owner-client <已签名的 owner 界面程序>``，从程序本身取 designated requirement
     （``SecStaticCodeCreateWithPath`` + ``SecCodeCopyDesignatedRequirement``）并先做一次静态校验；
     也可用 ``--owner-requirement '<requirement 字符串>'`` 直接给。
   - ad-hoc 签名（本机 ``codesign -s - <程序>``）的 designated requirement 是 ``cdhash H"…"``，钉死这一份二进制：
     程序被改任何一个字节，或改完重新 ad-hoc 签名，cdhash 都变，一律被拒；换新版要重启服务重新登记。
     用本地证书签名时 requirement 是「identifier + 证书」，同一证书签的新版可用，别人（含 ad-hoc 重签）不行。
   - 不再看可执行文件路径、inode、mtime：这些同用户可改可恢复（attempt 1 评审 R3）。
   - pid 复用或对端 exec：审计令牌带 pidversion，Security 框架按它取进程，对不上即失败。
4. 辅助判定——进程链：从对端沿父进程向上到 launchd，任何一层是已知 Agent CLI 就拒绝。
   它挡「主管或分身把 owner 界面程序当子进程拉起」；双 fork 能绕过它，所以只是辅助。

剩余边界（与 deckd 相同）：防的是模型越权与误操作。同一系统用户下能 ptrace / 注入 owner 界面进程、
或改掉服务启动参数后重启服务的恶意代码防不住。owner 界面程序本身不得提供无界面的答复入口
（命令行参数、URL scheme、AppleScript），否则签名对也没用——这一条交 G4 合同。非 macOS 一律拒绝。
"""

from __future__ import annotations

import ctypes
import os
import socket
import struct
import sys
from typing import Callable


SOL_LOCAL = 0
LOCAL_PEERCRED = 0x001
LOCAL_PEERPID = 0x002
LOCAL_PEERTOKEN = 0x006
XUCRED_SIZE = 76
AUDIT_TOKEN_SIZE = 32
PROC_PIDTBSDINFO = 3
PROC_PIDTBSDINFO_SIZE = 136
PROC_PIDPATHINFO_MAXSIZE = 4096
CTL_KERN = 1
KERN_PROCARGS2 = 49
K_CF_STRING_ENCODING_UTF8 = 0x08000100

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


class _Security:
    """CoreFoundation / Security 框架的最小 ctypes 绑定。"""

    def __init__(self) -> None:
        vp = ctypes.c_void_p
        self.cf = cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        self.sec = sec = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
        cf.CFDataCreate.restype = vp
        cf.CFDataCreate.argtypes = [vp, ctypes.c_char_p, ctypes.c_long]
        cf.CFStringCreateWithCString.restype = vp
        cf.CFStringCreateWithCString.argtypes = [vp, ctypes.c_char_p, ctypes.c_uint32]
        cf.CFURLCreateFromFileSystemRepresentation.restype = vp
        cf.CFURLCreateFromFileSystemRepresentation.argtypes = [vp, ctypes.c_char_p, ctypes.c_long, ctypes.c_bool]
        cf.CFDictionaryCreate.restype = vp
        cf.CFDictionaryCreate.argtypes = [vp, ctypes.POINTER(vp), ctypes.POINTER(vp), ctypes.c_long, vp, vp]
        cf.CFRelease.argtypes = [vp]
        cf.CFStringGetCString.restype = ctypes.c_bool
        cf.CFStringGetCString.argtypes = [vp, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
        self.key_callbacks = ctypes.c_void_p.in_dll(cf, "kCFTypeDictionaryKeyCallBacks")
        self.value_callbacks = ctypes.c_void_p.in_dll(cf, "kCFTypeDictionaryValueCallBacks")
        self.audit_key = vp.in_dll(sec, "kSecGuestAttributeAudit")
        sec.SecCodeCopyGuestWithAttributes.argtypes = [vp, vp, ctypes.c_uint32, ctypes.POINTER(vp)]
        sec.SecCodeCopyGuestWithAttributes.restype = ctypes.c_int32
        sec.SecRequirementCreateWithString.argtypes = [vp, ctypes.c_uint32, ctypes.POINTER(vp)]
        sec.SecRequirementCreateWithString.restype = ctypes.c_int32
        sec.SecRequirementCopyString.argtypes = [vp, ctypes.c_uint32, ctypes.POINTER(vp)]
        sec.SecRequirementCopyString.restype = ctypes.c_int32
        sec.SecCodeCheckValidity.argtypes = [vp, ctypes.c_uint32, vp]
        sec.SecCodeCheckValidity.restype = ctypes.c_int32
        sec.SecStaticCodeCreateWithPath.argtypes = [vp, ctypes.c_uint32, ctypes.POINTER(vp)]
        sec.SecStaticCodeCreateWithPath.restype = ctypes.c_int32
        sec.SecStaticCodeCheckValidity.argtypes = [vp, ctypes.c_uint32, vp]
        sec.SecStaticCodeCheckValidity.restype = ctypes.c_int32
        sec.SecCodeCopyDesignatedRequirement.argtypes = [vp, ctypes.c_uint32, ctypes.POINTER(vp)]
        sec.SecCodeCopyDesignatedRequirement.restype = ctypes.c_int32

    def release(self, *refs: ctypes.c_void_p) -> None:
        for ref in refs:
            if ref:
                self.cf.CFRelease(ref)

    def requirement_from_string(self, text: str) -> ctypes.c_void_p:
        string = self.cf.CFStringCreateWithCString(None, text.encode("utf-8"), K_CF_STRING_ENCODING_UTF8)
        requirement = ctypes.c_void_p()
        status = self.sec.SecRequirementCreateWithString(string, 0, ctypes.byref(requirement))
        self.release(string)
        if status != 0 or not requirement:
            raise IdentityError("owner_client_invalid")
        return requirement

    def requirement_string(self, requirement: ctypes.c_void_p) -> str:
        string = ctypes.c_void_p()
        if self.sec.SecRequirementCopyString(requirement, 0, ctypes.byref(string)) != 0:
            raise IdentityError("owner_client_invalid")
        buffer = ctypes.create_string_buffer(4096)
        ok = self.cf.CFStringGetCString(string, buffer, len(buffer), K_CF_STRING_ENCODING_UTF8)
        self.release(string)
        if not ok:
            raise IdentityError("owner_client_invalid")
        return buffer.value.decode("utf-8")

    def designated_requirement(self, path: str) -> ctypes.c_void_p:
        """取已签名程序的 designated requirement，并先静态校验程序满足它（未签名、签名坏的拒绝）。"""
        raw = os.fsencode(path)
        url = self.cf.CFURLCreateFromFileSystemRepresentation(None, raw, len(raw), False)
        static = ctypes.c_void_p()
        requirement = ctypes.c_void_p()
        try:
            if not url or self.sec.SecStaticCodeCreateWithPath(url, 0, ctypes.byref(static)) != 0:
                raise IdentityError("owner_client_invalid")
            if self.sec.SecCodeCopyDesignatedRequirement(static, 0, ctypes.byref(requirement)) != 0 or not requirement:
                raise IdentityError("owner_client_invalid")
            if self.sec.SecStaticCodeCheckValidity(static, 0, requirement) != 0:
                self.release(requirement)
                raise IdentityError("owner_client_invalid")
            return requirement
        finally:
            self.release(url, static)

    def check_peer(self, token: bytes, requirement: ctypes.c_void_p) -> int:
        """按审计令牌取对端代码并校验；返回 OSStatus（0 为通过）。"""
        data = self.cf.CFDataCreate(None, token, len(token))
        keys = (ctypes.c_void_p * 1)(self.audit_key.value)
        values = (ctypes.c_void_p * 1)(data)
        attributes = self.cf.CFDictionaryCreate(None, keys, values, 1, ctypes.addressof(self.key_callbacks),
                                                ctypes.addressof(self.value_callbacks))
        code = ctypes.c_void_p()
        try:
            status = self.sec.SecCodeCopyGuestWithAttributes(None, attributes, 0, ctypes.byref(code))
            if status != 0 or not code:
                return status or -1
            return self.sec.SecCodeCheckValidity(code, 0, requirement)
        finally:
            self.release(code, attributes, data)


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

    def __init__(self, clients: list[str] | None = None, *, requirements: list[str] | None = None,
                 platform: str | None = None) -> None:
        clients = list(clients or [])
        requirements = list(requirements or [])
        if not clients and not requirements:
            raise IdentityError("owner_client_invalid")
        for path in clients:
            if not os.path.isabs(path):
                raise IdentityError("owner_client_invalid")
        self.platform = platform or sys.platform
        self.requirement_strings: list[str] = []
        self._requirements: list[ctypes.c_void_p] = []
        if self.platform != "darwin":
            self._security = None
            self._darwin = None
            return
        self._security = _Security()
        self._darwin = _Darwin()
        for path in clients:
            requirement = self._security.designated_requirement(path)
            self._requirements.append(requirement)
            self.requirement_strings.append(self._security.requirement_string(requirement))
        for text in requirements:
            requirement = self._security.requirement_from_string(text)
            self._requirements.append(requirement)
            self.requirement_strings.append(self._security.requirement_string(requirement))

    def check(self, connection: socket.socket) -> int:
        if self._darwin is None or self._security is None:
            raise IdentityError("identity_unsupported_platform")
        try:
            cred = connection.getsockopt(SOL_LOCAL, LOCAL_PEERCRED, XUCRED_SIZE)
            pid = struct.unpack("<i", connection.getsockopt(SOL_LOCAL, LOCAL_PEERPID, 4))[0]
            token = connection.getsockopt(SOL_LOCAL, LOCAL_PEERTOKEN, AUDIT_TOKEN_SIZE)
            fields = struct.unpack("<8I", token)
        except (OSError, struct.error) as exc:
            raise IdentityError("identity_unavailable") from exc
        version, uid = struct.unpack_from("<II", cred, 0)
        euid = os.geteuid()
        if version != 0 or uid != euid or fields[1] != euid:
            raise IdentityError("caller_uid_mismatch")
        if pid <= 1 or fields[5] != pid:
            raise IdentityError("identity_unavailable")
        if not any(self._security.check_peer(token, requirement) == 0 for requirement in self._requirements):
            raise IdentityError("caller_not_owner_client")
        self._check_ancestry(pid)
        # 进程链读完后再校验一次签名（同一审计令牌）：期间对端退出、pid 被复用都会失败。
        if not any(self._security.check_peer(token, requirement) == 0 for requirement in self._requirements):
            raise IdentityError("identity_unavailable")
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
                break  # 祖先恰好退出：链到此为止（对端自身由审计令牌绑定）
            if is_agent_process(path, darwin.argv(current)):
                raise IdentityError("owner_client_under_agent")
            current = parent


CheckFunction = Callable[[socket.socket], int]
