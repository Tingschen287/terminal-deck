"""owner 答复写回收件箱（n077）。只在条目末尾追加答复，不改别的字节，不覆盖主管的改动。

一次写回的步骤（全部在写回锁内，同一时刻只有一个写回）：

1. 规范答复（行 / 不 / 改成……），查只追加记录里这个 request_id 有没有做过（幂等）。
2. 读文件（拒绝软链、非普通文件、多硬链接），整份 sha256 必须等于请求带来的 expected_sha256；
   不等说明 GUI 读过之后文件被改过，拒写，让 GUI 重读。
3. 解析、定位条目（行号 + 条目 sha256），算出纯插入的落点与字节，在内存里再解析一遍核对往返一致。
4. 记录 prepared；把新内容写到同目录的临时文件并 fsync。
5. 换文件前再读一次：inode 与内容都必须还是第 2 步那份。
6. 用原子交换（macOS renamex_np RENAME_SWAP，Linux renameat2 RENAME_EXCHANGE）把临时文件与收件箱对调。
   对调后，被换下来的那份就是交换瞬间的收件箱：它的 inode 与 sha256 必须仍是第 2 步那份，
   否则说明第 5 步到第 6 步之间主管改过，立刻再对调一次换回主管的版本，拒写。
   不支持原子交换的文件系统直接拒写（swap_unavailable），不退化成会覆盖的普通改名。
7. 删除换下来的旧文件，fsync 目录，记录 applied（时间、条目、答复、前后哈希）。

不能消除的窗口：主管的编辑器在第 6 步之后仍拿着旧 inode 原地写，写进的是已删除的旧文件。见 inbox-format.md「已知边界」。
"""

from __future__ import annotations

import contextlib
import ctypes
import datetime as _dt
import errno
import fcntl
import hashlib
import json
import os
import re
import sys
import time
from typing import Any, Callable, Iterator

import inbox
from inbox import InboxError
from inbox_io import Snapshot, read_snapshot


LOG_NAME = "inbox-writeback.jsonl"
LOCK_NAME = "inbox-writeback.lock"
LOCK_TIMEOUT = 5.0
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")

RENAME_SWAP = 0x2  # macOS <stdio.h>
RENAME_EXCHANGE = 0x2  # Linux <linux/fs.h>
AT_FDCWD = -100


def _libc() -> ctypes.CDLL:
    return ctypes.CDLL(None, use_errno=True)


def atomic_swap(first: str, second: str) -> str:
    """原子对调两个路径上的文件；返回所用机制名。不支持就抛 swap_unavailable。"""
    a, b = os.fsencode(first), os.fsencode(second)
    if sys.platform == "darwin":
        function = getattr(_libc(), "renamex_np", None)
        if function is None:
            raise InboxError("swap_unavailable")
        function.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        function.restype = ctypes.c_int
        if function(a, b, RENAME_SWAP) != 0:
            code = ctypes.get_errno()
            raise InboxError("swap_unavailable" if code in (errno.ENOTSUP, errno.EINVAL, errno.ENOSYS) else "write_failed")
        return "renamex_np"
    if sys.platform.startswith("linux"):
        function = getattr(_libc(), "renameat2", None)
        if function is None:
            raise InboxError("swap_unavailable")
        function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        function.restype = ctypes.c_int
        if function(AT_FDCWD, a, AT_FDCWD, b, RENAME_EXCHANGE) != 0:
            code = ctypes.get_errno()
            raise InboxError("swap_unavailable" if code in (errno.ENOTSUP, errno.EINVAL, errno.ENOSYS) else "write_failed")
        return "renameat2"
    raise InboxError("swap_unavailable")


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _fsync_dir(path: str) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class WriteRejected(InboxError):
    def __init__(self, code: str, current_sha256: str | None = None) -> None:
        super().__init__(code)
        self.current_sha256 = current_sha256


class InboxWriter:
    """写回入口的执行部分。身份判定不在这里，由 inbox_write_service 在调用前完成。"""

    def __init__(
        self,
        inbox_path: str,
        state_dir: str,
        *,
        hooks: dict[str, Callable[[], None]] | None = None,
        swap: Callable[[str, str], str] = atomic_swap,
    ) -> None:
        self.inbox_path = os.path.abspath(inbox_path)
        self.directory = os.path.dirname(self.inbox_path)
        self.state_dir = os.path.abspath(state_dir)
        os.makedirs(self.state_dir, mode=0o700, exist_ok=True)
        os.chmod(self.state_dir, 0o700)
        self.log_path = os.path.join(self.state_dir, LOG_NAME)
        self.lock_path = os.path.join(self.state_dir, LOCK_NAME)
        self.hooks = hooks or {}
        self.swap = swap

    # ---- 记录 ----

    def _append_log(self, record: dict[str, Any]) -> None:
        line = json.dumps({"ts": _now(), **record}, ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n"
        try:
            fd = os.open(self.log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        except OSError as exc:
            raise InboxError("log_failed") from exc
        try:
            written = os.write(fd, line)
            if written != len(line):
                raise InboxError("log_failed")
            os.fsync(fd)
        except OSError as exc:
            raise InboxError("log_failed") from exc
        finally:
            os.close(fd)

    def records(self, request_id: str | None = None) -> list[dict[str, Any]]:
        try:
            with open(self.log_path, "rb") as handle:
                raw_lines = handle.read().splitlines()
        except FileNotFoundError:
            return []
        result = []
        for raw in raw_lines:
            try:
                record = json.loads(raw)
            except json.JSONDecodeError as exc:
                # 记录被截断或改坏：不猜，拒绝继续写回。
                raise InboxError("log_failed") from exc
            if request_id is None or record.get("request_id") == request_id:
                result.append(record)
        return result

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            deadline = time.monotonic() + LOCK_TIMEOUT
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() > deadline:
                        raise InboxError("writer_busy")
                    time.sleep(0.02)
            yield
        finally:
            os.close(fd)

    def _hook(self, name: str) -> None:
        hook = self.hooks.get(name)
        if hook:
            hook()

    # ---- 写回 ----

    def answer(
        self,
        *,
        request_id: Any,
        expected_sha256: Any,
        line_start: Any,
        entry_sha256: Any,
        kind: Any,
        text: Any = None,
    ) -> dict[str, Any]:
        if not isinstance(request_id, str) or not _REQUEST_ID_RE.match(request_id):
            raise InboxError("argument_invalid")
        for value in (expected_sha256, entry_sha256):
            if not isinstance(value, str) or not _SHA_RE.match(value):
                raise InboxError("argument_invalid")
        if isinstance(line_start, bool) or not isinstance(line_start, int) or line_start < 1:
            raise InboxError("argument_invalid")
        answer_value = inbox.normalize_answer(kind, text)
        fingerprint = hashlib.sha256(
            json.dumps([expected_sha256, line_start, entry_sha256, answer_value], ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        base = {
            "request_id": request_id,
            "fingerprint": fingerprint,
            "entry": {"line_start": line_start, "sha256": entry_sha256},
            "answer": answer_value,
            "pre_sha256": expected_sha256,
        }
        with self._locked():
            replay = self._replay(request_id, fingerprint, base)
            if replay is not None:
                return replay
            try:
                return self._apply(base, expected_sha256, line_start, entry_sha256, answer_value)
            except WriteRejected as exc:
                self._append_log({**base, "phase": "rejected", "code": exc.code,
                                  "current_sha256": exc.current_sha256})
                raise
            except InboxError as exc:
                if exc.code not in ("log_failed", "request_uncertain"):
                    self._append_log({**base, "phase": "rejected", "code": exc.code})
                raise

    def _replay(self, request_id: str, fingerprint: str, base: dict[str, Any]) -> dict[str, Any] | None:
        history = self.records(request_id)
        if not history:
            return None
        if any(record.get("fingerprint") != fingerprint for record in history):
            raise InboxError("request_id_conflict")
        applied = [r for r in history if r.get("phase") in ("applied", "recovered")]
        if applied:
            record = applied[-1]
            return {"applied": True, "replayed": True, "previous_sha256": record["pre_sha256"],
                    "sha256": record["post_sha256"], "entry": record.get("result_entry")}
        prepared = [r for r in history if r.get("phase") == "prepared"]
        if not prepared or history[-1].get("phase") == "rejected":
            return None  # 只被拒过：按新的一次重来（仍要过哈希比对）
        record = prepared[-1]
        current = read_snapshot(self.inbox_path)
        if current.sha256 == record["post_sha256"]:
            # 上次已换好文件、没来得及记 applied（例如进程被杀）：补记，不再写。
            # 换下来的旧文件若还在且正是写前那份，删掉；不是就留给人看。
            leftover = self._temporary_path(request_id)
            with contextlib.suppress(InboxError):
                if read_snapshot(leftover).sha256 == record["pre_sha256"]:
                    os.unlink(leftover)
            self._append_log({**base, "phase": "recovered", "post_sha256": record["post_sha256"],
                              "result_entry": record.get("result_entry")})
            return {"applied": True, "replayed": True, "previous_sha256": record["pre_sha256"],
                    "sha256": record["post_sha256"], "entry": record.get("result_entry")}
        if current.sha256 == record["pre_sha256"]:
            return None  # 上次没换成：文件仍是原样，可以重来
        raise InboxError("request_uncertain")

    def _apply(self, base: dict[str, Any], expected: str, line_start: int, entry_sha: str, answer_value: str) -> dict[str, Any]:
        before = read_snapshot(self.inbox_path)
        if before.sha256 != expected:
            raise WriteRejected("inbox_changed", before.sha256)
        parsed = inbox.parse_inbox(before.data)
        entry = inbox.find_entry(parsed, line_start, entry_sha)
        position, inserted = inbox.plan_answer(before.data, entry, answer_value)
        new_data = before.data[:position] + inserted + before.data[position:]
        result_entry = self._verify_roundtrip(parsed, inbox.parse_inbox(new_data), entry, answer_value, len(inserted))
        post_sha = inbox.sha256_hex(new_data)

        self._append_log({**base, "phase": "prepared", "post_sha256": post_sha, "result_entry": result_entry,
                          "insert": {"byte": position, "bytes": len(inserted)}})
        temporary = self._temporary_path(base["request_id"])
        if os.path.lexists(temporary):
            # 同一 request_id 上次没换成留下的临时文件：内容正是这次要写的才删，否则不动它、拒写。
            try:
                leftover = read_snapshot(temporary)
            except InboxError as exc:
                raise InboxError("write_failed") from exc
            if leftover.sha256 != post_sha:
                raise InboxError("write_failed")
            os.unlink(temporary)
        try:
            self._write_temporary(temporary, new_data, before.mode)
        except FileExistsError as exc:
            raise InboxError("write_failed") from exc
        swapped = False
        try:
            self._hook("before_precheck")
            current = read_snapshot(self.inbox_path)
            if (current.dev, current.ino) != (before.dev, before.ino) or current.sha256 != expected:
                raise WriteRejected("inbox_changed", current.sha256)
            self._hook("before_swap")
            mechanism = self.swap(temporary, self.inbox_path)
            swapped = True
            displaced = read_snapshot(temporary)
            if (displaced.dev, displaced.ino) != (before.dev, before.ino) or displaced.sha256 != expected:
                # 交换前一瞬间收件箱被改了：换回主管的版本。
                try:
                    self.swap(temporary, self.inbox_path)
                except InboxError as exc:
                    self._append_log({**base, "phase": "uncertain", "code": "restore_failed", "kept": temporary})
                    raise InboxError("request_uncertain") from exc
                swapped = False
                restored = read_snapshot(self.inbox_path)
                if restored.sha256 != displaced.sha256:
                    self._append_log({**base, "phase": "uncertain", "code": "restore_mismatch",
                                      "current_sha256": restored.sha256, "kept": temporary})
                    temporary = ""  # 两份都留着给人看，不删
                    raise InboxError("request_uncertain")
                raise WriteRejected("inbox_changed", restored.sha256)
            self._hook("after_swap")
        except BaseException:
            if temporary and not swapped:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(temporary)
            raise
        # 换成功：temporary 现在是旧文件。
        os.unlink(temporary)
        _fsync_dir(self.directory)
        after = read_snapshot(self.inbox_path)
        self._append_log({**base, "phase": "applied", "post_sha256": post_sha, "result_entry": result_entry,
                          "swap": mechanism, "observed_sha256": after.sha256})
        return {"applied": True, "replayed": False, "previous_sha256": expected, "sha256": post_sha,
                "entry": result_entry}

    def _temporary_path(self, request_id: str) -> str:
        return os.path.join(self.directory, "." + os.path.basename(self.inbox_path) + ".anpai-" + request_id + ".tmp")

    @staticmethod
    def _write_temporary(path: str, data: bytes, mode: int) -> None:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fchmod(fd, mode)
            os.fsync(fd)
        except BaseException:
            os.close(fd)
            with contextlib.suppress(FileNotFoundError):
                os.unlink(path)
            raise
        os.close(fd)

    @staticmethod
    def _verify_roundtrip(old: dict[str, Any], new: dict[str, Any], entry: dict[str, Any], answer_value: str,
                          inserted_bytes: int) -> dict[str, Any]:
        """写之前在内存里核对：只有目标条目变了，且它被同一解析器认回成这条答复。"""
        target = next((e for e in new["pending"] if e["line_start"] == entry["line_start"]), None)
        expected_kind = {"行": "yes", "不": "no"}.get(answer_value, "change")
        if not target or target.get("status") != "parsed" or not target.get("answer"):
            raise InboxError("roundtrip_failed")
        if target["answer"]["kind"] != expected_kind or target["format"] != entry["format"]:
            raise InboxError("roundtrip_failed")
        for field in ("checked", "date", "section"):
            if target.get(field) != entry.get(field):
                raise InboxError("roundtrip_failed")
        for field in ("supervisor", "title"):
            if (target.get(field) or {}).get("sha256") != (entry.get(field) or {}).get("sha256"):
                raise InboxError("roundtrip_failed")
        added_lines = 0 if entry["format"] == "legacy" else 1
        for section in ("pending", "decided"):
            others_old = [e for e in old[section] if e["line_start"] != entry["line_start"] or section != "pending"]
            others_new = [e for e in new[section] if e["line_start"] != entry["line_start"] or section != "pending"]
            if [e["sha256"] for e in others_old] != [e["sha256"] for e in others_new]:
                raise InboxError("roundtrip_failed")
            for a, b in zip(others_old, others_new):
                shift = added_lines if a["line_start"] > entry["line_end"] else 0
                if b["line_start"] != a["line_start"] + shift or a.get("status") != b.get("status"):
                    raise InboxError("roundtrip_failed")
        if [r.get("tier") for r in old["trust"]["rows"]] != [r.get("tier") for r in new["trust"]["rows"]]:
            raise InboxError("roundtrip_failed")
        return {"entry_id": target["entry_id"], "line_start": target["line_start"],
                "line_end": target["line_end"], "sha256": target["sha256"]}
