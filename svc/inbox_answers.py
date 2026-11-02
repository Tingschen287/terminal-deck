#!/usr/bin/env python3
"""owner 答复文件（n077 attempt 2，owner 2026-11-02 选 A）。

GUI 不修改 owner-inbox.md。owner 在 GUI 里的答复只追加写进同目录的答复文件
``owner-inbox.answers.jsonl``（只增不改，一行一条 JSON），再由主管（主会话）合并回收件箱。

记录类型：
- ``answer``：时间、request_id、条目定位（日期 + 主管 + 标题 sha256）、行号提示、答复、
  GUI 读取时看到的收件箱 sha256、写入时的收件箱 sha256。
- ``merged``：主管已把这条答复写进收件箱（由 ``mark-merged`` 核对后记）。
- ``handled``：「待人工核对」的答复已由人处理（``mark-handled``）。

合并方式（两种里选了更不可能丢数据的一种）：**本模块不写 owner-inbox.md**。``pending`` 只列出待合并清单
（第几行、怎么追加），主管用自己的编辑工具在条目末尾写 ``你答：……``——这和主管现在改收件箱是同一个写入者、
同一种方式，不新增任何写入者，因此不会比现状多出任何覆盖窗口。另一种做法（工具自己「读→改→比对→写」）
无论用改名替换还是原地重写，都有「别的编辑器拿着旧文件描述符或在比对后保存」的窗口（attempt 1 评审 R1、R2），
那是新增的丢数据可能。写完后 ``mark-merged`` 只读核对收件箱里确实有这条答复，才记 ``merged``。

条目定位失配（标题改了、条目删了、挪到已定却没合并、两条一模一样、收件箱里已有不同答复）时，
答复保留，状态为 ``needs_review``（待人工核对）并给原因码，不猜。
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
import time
from typing import Any, Callable, Iterator

import inbox
from inbox import InboxError
from inbox_io import read_snapshot


ANSWERS_SUFFIX = ".answers.jsonl"
SCHEMA = 1
MAX_ANSWERS_BYTES = 8 * 1024 * 1024
LOCK_TIMEOUT = 5.0
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def answers_path_for(inbox_path: str) -> str:
    """答复文件与收件箱同目录：owner-inbox.md → owner-inbox.answers.jsonl。"""
    directory, name = os.path.split(os.path.abspath(inbox_path))
    stem = name[:-3] if name.endswith(".md") else name
    return os.path.join(directory, stem + ANSWERS_SUFFIX)


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def locator_of(data: bytes, entry: dict[str, Any]) -> dict[str, Any]:
    """条目定位：日期 + 主管（原文，没有为 null）+ 标题 sha256。答复不算进标题，所以合并答复后定位不变。"""
    supervisor = entry.get("supervisor")
    return {
        "date": entry["date"],
        "supervisor": data[supervisor["byte_start"] : supervisor["byte_end"]].decode("utf-8") if supervisor else None,
        "title_sha256": entry["title"]["sha256"],
    }


def _answer_value_of(data: bytes, entry: dict[str, Any]) -> str | None:
    """收件箱里条目已有答复的规范值（行 / 不 / 改成……）。"""
    answer = entry.get("answer")
    if not answer:
        return None
    if answer["kind"] == "yes":
        return "行"
    if answer["kind"] == "no":
        return "不"
    text = answer["text"]
    return data[text["byte_start"] : text["byte_end"]].decode("utf-8")


def _same_answer(a: str | None, b: str | None) -> bool:
    """答复是否相同：只去掉首尾空白、统一换行符，其余逐字节比较（内部空白、全半角、标点都不归一）。

    attempt 2 曾删除全部空白再比，「改成目录 `/tmp/a b`」与「`/tmp/ab`」被判相同（codex 评审 2 R4）。
    """
    if a is None or b is None:
        return False
    norm = lambda value: value.replace("\r\n", "\n").replace("\r", "\n").strip().encode("utf-8")
    return norm(a) == norm(b)


# ---------------------------------------------------------------- 读取（不写盘）


def parse_records(raw: bytes) -> tuple[list[dict[str, Any]], list[int]]:
    """解析答复文件。坏行（写一半、被手改坏）跳过并报行号，不丢其余记录。"""
    records: list[dict[str, Any]] = []
    corrupt: list[int] = []
    for number, line in enumerate(raw.split(b"\n"), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            corrupt.append(number)
            continue
        if not isinstance(record, dict) or record.get("type") not in ("answer", "merged", "handled") \
                or not isinstance(record.get("request_id"), str):
            corrupt.append(number)
            continue
        record["_line"] = number
        records.append(record)
    return records, corrupt


def read_answers(path: str) -> tuple[bytes, str | None]:
    """只读取答复文件；不存在返回空。软链、非普通文件、多硬链接拒绝。"""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
    except FileNotFoundError:
        return b"", None
    except OSError as exc:
        raise InboxError("answers_unsafe" if os.path.islink(path) else "answers_unreadable") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise InboxError("answers_unsafe")
        if info.st_size > MAX_ANSWERS_BYTES:
            raise InboxError("answers_too_large")
        chunks = []
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                break
            chunks.append(chunk)
        raw = b"".join(chunks)
    finally:
        os.close(fd)
    return raw, inbox.sha256_hex(raw)


def statuses(inbox_data: bytes, parsed: dict[str, Any], records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """每条 owner 答复的当前状态。

    pending_merge：定位到「待你定」里唯一一条、收件箱里还没有答复的条目；
    in_inbox：收件箱里那条已有同样的答复（主管已写入，未 mark-merged）；
    merged / handled：已记合并或已人工处理；
    needs_review：其余一律待人工核对（reason 给原因码）。
    """
    answers = [r for r in records if r["type"] == "answer"]
    merged = {r["request_id"] for r in records if r["type"] == "merged"}
    handled = {r["request_id"] for r in records if r["type"] == "handled"}
    entries = [(section, e) for section in ("pending", "decided") for e in parsed[section] if e.get("status") == "parsed"]
    keyed: dict[tuple, list[tuple[str, dict]]] = {}
    for section, entry in entries:
        locator = locator_of(inbox_data, entry)
        keyed.setdefault((locator["date"], locator["supervisor"], locator["title_sha256"]), []).append((section, entry))
    result = []
    claimed: set[int] = set()
    seen_request: set[str] = set()
    for record in answers:
        request_id = record["request_id"]
        if request_id in seen_request:
            continue  # 同一 request_id 只认第一条（写入端保证不重复）
        seen_request.add(request_id)
        status: dict[str, Any] = {"request_id": request_id, "ts": record.get("ts"), "answer": record.get("answer"),
                                  "kind": {"行": "yes", "不": "no"}.get(record.get("answer"), "change"),
                                  "record_line": record["_line"]}
        locator = record.get("locator") or {}
        matches = keyed.get((locator.get("date"), locator.get("supervisor"), locator.get("title_sha256")), [])
        if request_id in handled:
            status["status"] = "handled"
        elif request_id in merged:
            status["status"] = "merged"
        elif not matches:
            status.update(status="needs_review", reason="locator_not_found")
        elif len(matches) > 1:
            status.update(status="needs_review", reason="locator_ambiguous")
        else:
            section, entry = matches[0]
            status.update(entry_line=entry["line_start"], entry_id=entry["entry_id"], section=section)
            inbox_answer = _answer_value_of(inbox_data, entry)
            if inbox_answer is not None:
                if _same_answer(inbox_answer, record.get("answer")):
                    status["status"] = "in_inbox"
                else:
                    status.update(status="needs_review", reason="entry_answered_differently")
            elif section != "pending" or entry.get("checked") is not False:
                # 挪到已定、或在待你定里打了勾，却没有合并这条答复
                status.update(status="needs_review", reason="entry_moved_unmerged")
            elif entry.get("answer_possible"):
                status.update(status="needs_review", reason="entry_possibly_answered")
            elif id(entry) in claimed:
                status.update(status="needs_review", reason="duplicate_answer")
            else:
                status["status"] = "pending_merge"
            if status["status"] in ("pending_merge", "in_inbox"):
                claimed.add(id(entry))
        result.append(status)
    return result


def overlay(parsed: dict[str, Any], answer_statuses: list[dict[str, Any]], answers_path: str,
            answers_sha256: str | None, corrupt: list[int]) -> dict[str, Any]:
    """把未合并的答复叠加到读取结果上（只给元数据，不给答复原文：GUI 按 request_id 在本机读答复文件）。"""
    by_entry = {s["entry_id"]: s for s in answer_statuses if s.get("status") in ("pending_merge", "in_inbox")}
    for section in ("pending", "decided"):
        for entry in parsed[section]:
            status = by_entry.get(entry.get("entry_id"))
            if not status:
                continue
            entry["owner_answer"] = {
                "request_id": status["request_id"],
                "status": status["status"],
                "kind": status["kind"],
                "ts": status["ts"],
                "answer_sha256": inbox.sha256_hex((status["answer"] or "").encode("utf-8")),
            }
            if entry.get("answerable"):
                entry["answerable"] = False
                entry["unanswerable_reason"] = "answer_pending_merge"
    parsed["answers"] = {
        "path": answers_path,
        "sha256": answers_sha256,
        "corrupt_lines": corrupt,
        "counts": {name: sum(1 for s in answer_statuses if s["status"] == name)
                   for name in ("pending_merge", "in_inbox", "merged", "handled", "needs_review")},
        "needs_review": [{"request_id": s["request_id"], "reason": s["reason"], "kind": s["kind"], "ts": s["ts"]}
                         for s in answer_statuses if s["status"] == "needs_review"],
    }
    return parsed


def inbox_view(inbox_path: str) -> dict[str, Any]:
    """读取服务用：解析收件箱并叠加答复文件。全程只读。"""
    snapshot = read_snapshot(inbox_path)
    parsed = inbox.parse_inbox(snapshot.data)
    path = answers_path_for(inbox_path)
    raw, answers_sha = read_answers(path)
    records, corrupt = parse_records(raw)
    overlay(parsed, statuses(snapshot.data, parsed, records), path, answers_sha, corrupt)
    parsed["file"] = {"path": os.path.abspath(inbox_path), "sha256": snapshot.sha256, "size": snapshot.size,
                      "mtime_ns": snapshot.mtime_ns}
    return parsed


# ---------------------------------------------------------------- 追加（只写答复文件）


class AnswerRejected(InboxError):
    def __init__(self, code: str, current_sha256: str | None = None) -> None:
        super().__init__(code)
        self.current_sha256 = current_sha256


class AnswerStore:
    """答复文件的唯一写入口。对收件箱只读；答复文件只追加，持 flock 串行。"""

    def __init__(self, inbox_path: str, *, answers_path: str | None = None,
                 hooks: dict[str, Callable[[], None]] | None = None) -> None:
        self.inbox_path = os.path.abspath(inbox_path)
        self.answers_path = answers_path or answers_path_for(self.inbox_path)
        self.hooks = hooks or {}

    def _hook(self, name: str) -> None:
        if self.hooks.get(name):
            self.hooks[name]()

    @contextlib.contextmanager
    def _locked(self) -> Iterator[int]:
        try:
            fd = os.open(self.answers_path, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), 0o600)
        except OSError as exc:
            raise InboxError("answers_unsafe" if os.path.islink(self.answers_path) else "answers_unreadable") from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise InboxError("answers_unsafe")
            deadline = time.monotonic() + LOCK_TIMEOUT
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() > deadline:
                        raise InboxError("writer_busy")
                    time.sleep(0.01)
            yield fd
        finally:
            os.close(fd)

    def _records(self) -> tuple[bytes, list[dict[str, Any]]]:
        raw, _ = read_answers(self.answers_path)
        records, _corrupt = parse_records(raw)
        return raw, records

    @staticmethod
    def _append(fd: int, raw_before: bytes, record: dict[str, Any]) -> None:
        line = json.dumps({"schema": SCHEMA, "ts": _now(), **record}, ensure_ascii=False, sort_keys=True).encode("utf-8")
        # 上一条写到一半（文件不以换行结尾）：先补换行把它隔开，不改它。
        prefix = b"\n" if raw_before and not raw_before.endswith(b"\n") else b""
        payload = prefix + line + b"\n"
        try:
            written = os.write(fd, payload)
            if written != len(payload):
                raise InboxError("answers_write_failed")
            os.fsync(fd)
        except OSError as exc:
            raise InboxError("answers_write_failed") from exc

    def answer(self, *, request_id: Any, expected_sha256: Any, line_start: Any, entry_sha256: Any, kind: Any,
               text: Any = None) -> dict[str, Any]:
        if not isinstance(request_id, str) or not _REQUEST_ID_RE.match(request_id):
            raise InboxError("argument_invalid")
        for value in (expected_sha256, entry_sha256):
            if not isinstance(value, str) or not _SHA_RE.match(value):
                raise InboxError("argument_invalid")
        if isinstance(line_start, bool) or not isinstance(line_start, int) or line_start < 1:
            raise InboxError("argument_invalid")
        answer_value = inbox.normalize_answer(kind, text)
        fingerprint = hashlib.sha256(json.dumps([entry_sha256, answer_value], ensure_ascii=False).encode("utf-8")).hexdigest()
        with self._locked() as fd:
            raw, records = self._records()
            previous = [r for r in records if r.get("request_id") == request_id and r["type"] == "answer"]
            if previous:
                if previous[0].get("fingerprint") != fingerprint:
                    raise InboxError("request_id_conflict")
                return {"recorded": True, "replayed": True, "request_id": request_id,
                        "locator": previous[0].get("locator"), "inbox_sha256": previous[0].get("inbox_sha256")}
            snapshot = read_snapshot(self.inbox_path)
            parsed = inbox.parse_inbox(snapshot.data)
            entry = self._locate(parsed, line_start, entry_sha256, snapshot.sha256)
            if not entry.get("answerable"):
                raise AnswerRejected(entry.get("unanswerable_reason") or "entry_not_pending")
            locator = locator_of(snapshot.data, entry)
            same_locator = [e for section in ("pending", "decided") for e in parsed[section]
                            if e.get("status") == "parsed" and locator_of(snapshot.data, e) == locator]
            if len(same_locator) != 1:
                raise AnswerRejected("entry_locator_ambiguous")
            for status in statuses(snapshot.data, parsed, records):
                if status.get("entry_id") == entry["entry_id"] and status["status"] in ("pending_merge", "in_inbox", "needs_review"):
                    raise AnswerRejected("entry_already_answered")
            self._hook("before_append")
            record = {
                "type": "answer",
                "request_id": request_id,
                "fingerprint": fingerprint,
                "locator": locator,
                "line_hint": entry["line_start"],
                "format": entry["format"],
                "answer": answer_value,
                "seen_inbox_sha256": expected_sha256,
                "inbox_sha256": snapshot.sha256,
            }
            self._append(fd, raw, record)
        return {"recorded": True, "replayed": False, "request_id": request_id, "locator": locator,
                "inbox_sha256": snapshot.sha256}

    @staticmethod
    def _locate(parsed: dict[str, Any], line_start: int, entry_sha256: str, current_sha: str) -> dict[str, Any]:
        """先按行号找，行号对不上就在「待你定」里按条目 sha256 找唯一一条（主管在别处改过文件也能答）。"""
        for entry in parsed["pending"] + parsed["decided"]:
            if entry["line_start"] == line_start and entry["sha256"] == entry_sha256:
                return entry
        same = [e for e in parsed["pending"] if e["sha256"] == entry_sha256]
        if len(same) == 1:
            return same[0]
        raise AnswerRejected("entry_not_found", current_sha)

    # ---- 主管用（合并回收件箱由主管自己的编辑工具做；这里只核对与记账）----

    def pending(self) -> list[dict[str, Any]]:
        snapshot = read_snapshot(self.inbox_path)
        parsed = inbox.parse_inbox(snapshot.data)
        _raw, records = self._records()
        result = []
        for status in statuses(snapshot.data, parsed, records):
            item = dict(status)
            if status["status"] == "pending_merge":
                entry = next(e for e in parsed["pending"] if e["entry_id"] == status["entry_id"])
                position, inserted = inbox.plan_answer(snapshot.data, entry, status["answer"])
                item["merge"] = {
                    "how": "append_to_line" if entry["format"] == "legacy" else "insert_line_after",
                    "line": entry["line_end"],
                    "text": inserted.decode("utf-8").rstrip("\r\n"),
                }
            result.append(item)
        return result

    def mark_merged(self, request_id: str) -> dict[str, Any]:
        with self._locked() as fd:
            raw, records = self._records()
            snapshot = read_snapshot(self.inbox_path)
            parsed = inbox.parse_inbox(snapshot.data)
            status = next((s for s in statuses(snapshot.data, parsed, records) if s["request_id"] == request_id), None)
            if status is None:
                raise InboxError("request_not_found")
            if status["status"] in ("merged", "handled"):
                return {"request_id": request_id, "status": status["status"], "replayed": True}
            if status["status"] != "in_inbox":
                raise InboxError("not_merged_yet" if status["status"] == "pending_merge" else "needs_review")
            self._append(fd, raw, {"type": "merged", "request_id": request_id, "inbox_sha256": snapshot.sha256,
                                   "entry_line": status["entry_line"]})
        return {"request_id": request_id, "status": "merged", "replayed": False}

    def mark_handled(self, request_id: str) -> dict[str, Any]:
        with self._locked() as fd:
            raw, records = self._records()
            snapshot = read_snapshot(self.inbox_path)
            parsed = inbox.parse_inbox(snapshot.data)
            status = next((s for s in statuses(snapshot.data, parsed, records) if s["request_id"] == request_id), None)
            if status is None:
                raise InboxError("request_not_found")
            if status["status"] != "needs_review":
                raise InboxError("not_needs_review")
            self._append(fd, raw, {"type": "handled", "request_id": request_id, "reason": status["reason"],
                                   "inbox_sha256": snapshot.sha256})
        return {"request_id": request_id, "status": "handled"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="owner 答复文件：主管查看待合并清单与记账（不写收件箱）")
    parser.add_argument("--inbox", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    pending = sub.add_parser("pending", help="列出答复与待合并清单")
    pending.add_argument("--json", action="store_true")
    for name in ("mark-merged", "mark-handled"):
        sub.add_parser(name).add_argument("--request-id", required=True)
    args = parser.parse_args(argv)
    store = AnswerStore(args.inbox)
    try:
        if args.command == "pending":
            items = store.pending()
            if args.json:
                print(json.dumps(items, ensure_ascii=False, indent=2))
                return 0
            for item in items:
                if item["status"] == "pending_merge":
                    merge = item["merge"]
                    where = "行尾追加" if merge["how"] == "append_to_line" else "之后插入一行"
                    print(f"[待合并] {item['request_id']} 第 {merge['line']} 行{where}：{merge['text']}")
                elif item["status"] == "in_inbox":
                    print(f"[已写入未记账] {item['request_id']} 第 {item['entry_line']} 行；核对后运行 mark-merged")
                elif item["status"] == "needs_review":
                    print(f"[待人工核对] {item['request_id']} 原因 {item['reason']}，答复：{item['answer']}")
            return 0
        result = store.mark_merged(args.request_id) if args.command == "mark-merged" else store.mark_handled(args.request_id)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except InboxError as exc:
        print(f"ERROR: {exc.code}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
