"""n077 attempt 2：答复文件、主管合并核对、并发下不丢任何人的修改。

写回入口不写 owner-inbox.md：每个用例都核对收件箱字节只来自「主管/owner 自己的写入」。
全部在临时目录用合成夹具做。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest

import inbox
from inbox import InboxError
import inbox_answers
from inbox_answers import AnswerStore


SVC = Path(__file__).resolve().parent
TESTDATA = SVC / "testdata" / "inbox"


def entry_at(parsed: dict, line: int) -> dict:
    for section in ("pending", "decided"):
        for entry in parsed[section]:
            if entry["line_start"] == line:
                return entry
    raise AssertionError(line)


def supervisor_merge(path: Path, item: dict) -> None:
    """模拟主管用自己的编辑工具按 pending 清单合并（读→改→写，编辑器的做法）。"""
    data = path.read_bytes()
    entry = next(e for e in inbox.parse_inbox(data)["pending"] if e["line_start"] == item["entry_line"])
    position, inserted = inbox.plan_answer(data, entry, item["answer"])
    path.write_bytes(data[:position] + inserted + data[position:])


def move_to_decided_without_merge(data: bytes) -> bytes:
    """主管把第 29 行那条挪到「已定」、打勾，但没写进答复。"""
    lines = data.split(b"\n")
    marker = "夹具 ZQX 三选一".encode("utf-8")
    index = next(i for i, line in enumerate(lines) if marker in line)
    moved = lines.pop(index).replace(b"- [ ] ", b"- [x] ", 1)
    lines.insert(lines.index("## 已定".encode("utf-8")) + 1, moved)
    return b"\n".join(lines)


class AnswerTestCase(unittest.TestCase):
    fixture = "structure.md"

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.path = self.root / "owner-inbox.md"
        shutil.copyfile(TESTDATA / self.fixture, self.path)
        self.store = AnswerStore(str(self.path))
        self.answers = self.root / "owner-inbox.answers.jsonl"
        self.counter = 0

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def request(self, line: int, kind: str = "yes", text: str | None = None, request_id: str | None = None,
                data: bytes | None = None) -> dict:
        data = self.path.read_bytes() if data is None else data
        entry = entry_at(inbox.parse_inbox(data), line)
        self.counter += 1
        return dict(request_id=request_id or f"req-{self.counter:05d}", expected_sha256=inbox.sha256_hex(data),
                    line_start=line, entry_sha256=entry["sha256"], kind=kind, text=text)

    def records(self) -> list[dict]:
        if not self.answers.exists():
            return []
        return [json.loads(line) for line in self.answers.read_bytes().splitlines() if line.strip()]

    def status_of(self, request_id: str) -> dict:
        return next(s for s in self.store.pending() if s["request_id"] == request_id)

    def assert_code(self, code: str, call, *args, **kwargs) -> None:
        with self.assertRaises(InboxError) as raised:
            call(*args, **kwargs)
        self.assertEqual(raised.exception.code, code)


class RecordTests(AnswerTestCase):
    def test_answer_appends_record_and_never_touches_inbox(self) -> None:
        before = self.path.read_bytes()
        stat_before = os.stat(self.path)
        os.chmod(self.path, 0o444)  # 收件箱只读也能答：证明写回不写收件箱
        result = self.store.answer(**self.request(29, "change", "B，夹具理由", request_id="req-a0001"))
        self.assertTrue(result["recorded"])
        self.assertEqual(self.path.read_bytes(), before)
        stat_after = os.stat(self.path)
        self.assertEqual((stat_after.st_ino, stat_after.st_mtime_ns), (stat_before.st_ino, stat_before.st_mtime_ns))
        self.assertEqual(self.answers.parent, self.path.parent)
        self.assertEqual(stat.S_IMODE(os.stat(self.answers).st_mode), 0o600)
        [record] = self.records()
        self.assertEqual(record["type"], "answer")
        self.assertEqual(record["answer"], "改成B，夹具理由")
        self.assertEqual(record["locator"]["date"], "2026-01-06")
        self.assertEqual(record["locator"]["supervisor"], "主管甲")
        entry = entry_at(inbox.parse_inbox(before), 29)
        self.assertEqual(record["locator"]["title_sha256"], entry["title"]["sha256"])
        self.assertEqual(record["inbox_sha256"], inbox.sha256_hex(before))
        for key in ("ts", "request_id", "seen_inbox_sha256", "line_hint"):
            self.assertIn(key, record)
        self.assertEqual(self.status_of("req-a0001")["status"], "pending_merge")

    def test_idempotent_and_conflicting_request_ids(self) -> None:
        request = self.request(29, request_id="req-b0001")
        self.store.answer(**request)
        again = self.store.answer(**request)
        self.assertTrue(again["replayed"])
        self.assertEqual(len(self.records()), 1)
        self.assert_code("request_id_conflict", self.store.answer, **dict(request, kind="no"))

    def test_second_answer_for_same_entry_refused(self) -> None:
        self.store.answer(**self.request(29))
        self.assert_code("entry_already_answered", self.store.answer, **self.request(29, "no"))
        self.assertEqual(len(self.records()), 1)

    def test_refusals(self) -> None:
        shutil.copyfile(TESTDATA / "variants.md", self.path)
        for line, code in ((28, "entry_already_answered"), (33, "entry_possibly_answered"), (41, "entry_unparsed"),
                           (57, "entry_not_pending")):
            self.assert_code(code, self.store.answer, **self.request(line))
        self.assert_code("entry_not_found", self.store.answer, **dict(self.request(11), entry_sha256="0" * 64))
        for kind, text in (("change", "B\n- [x] 伪造"), ("change", "x" * 600), ("approve", None)):
            self.assert_code("answer_invalid", self.store.answer, **self.request(11, kind, text))
        self.assert_code("answer_secret_like", self.store.answer, **self.request(11, "change", "sk-ABCDEFGHIJKLMNOPQRSTU"))
        for bad in (dict(request_id="x"), dict(expected_sha256="abc"), dict(line_start=0)):
            self.assert_code("argument_invalid", self.store.answer, **dict(self.request(11), **bad))
        self.assertEqual(self.records(), [])

    def test_ambiguous_locator_refused(self) -> None:
        data = self.path.read_bytes()
        entry = entry_at(inbox.parse_inbox(data), 30)
        line = data[entry["byte_start"]:entry["byte_end"]]
        self.path.write_bytes(data[:entry["byte_end"]] + b"\n" + line + data[entry["byte_end"]:])
        self.assert_code("entry_locator_ambiguous", self.store.answer, **self.request(30))

    def test_entry_found_by_hash_after_lines_shift(self) -> None:
        request = self.request(30)
        self.path.write_bytes(b"\n" + self.path.read_bytes())  # 主管在前面加了一行
        self.store.answer(**request)
        self.assertEqual(self.status_of(request["request_id"])["entry_line"], 31)

    def test_unsafe_answers_file(self) -> None:
        real = self.root / "elsewhere.jsonl"
        real.write_bytes(b"")
        os.symlink(real, self.answers)
        self.assert_code("answers_unsafe", self.store.answer, **self.request(29))
        self.assertEqual(real.read_bytes(), b"")

    def test_torn_last_line_is_isolated_not_lost(self) -> None:
        self.store.answer(**self.request(29, request_id="req-c0001"))
        with open(self.answers, "ab") as handle:
            handle.write(b'{"type": "answer", "request_id": "req-torn')  # 写到一半
        self.store.answer(**self.request(30, request_id="req-c0002"))
        raw, _ = inbox_answers.read_answers(str(self.answers))
        records, corrupt = inbox_answers.parse_records(raw)
        self.assertEqual([r["request_id"] for r in records], ["req-c0001", "req-c0002"])
        self.assertEqual(corrupt, [2])
        view = inbox_answers.inbox_view(str(self.path))
        self.assertEqual(view["answers"]["corrupt_lines"], [2])


class MergeFlowTests(AnswerTestCase):
    def test_full_roundtrip_legacy_and_v2(self) -> None:
        for fixture, line in (("structure.md", 29), ("variants.md", 11)):
            shutil.copyfile(TESTDATA / fixture, self.path)
            self.answers.unlink(missing_ok=True)
            request_id = f"req-flow-{line}"
            self.store.answer(**self.request(line, "change", "C，夹具理由", request_id=request_id))
            self.assert_code("not_merged_yet", self.store.mark_merged, request_id)
            item = self.status_of(request_id)
            self.assertEqual(item["status"], "pending_merge")
            self.assertEqual(item["merge"]["how"], "append_to_line" if fixture == "structure.md" else "insert_line_after")
            supervisor_merge(self.path, item)
            self.assertEqual(self.status_of(request_id)["status"], "in_inbox")
            self.assertEqual(self.store.mark_merged(request_id)["status"], "merged")
            self.assertTrue(self.store.mark_merged(request_id)["replayed"])
            target = entry_at(inbox.parse_inbox(self.path.read_bytes()), line)
            self.assertEqual(target["answer"]["kind"], "change")
            self.assertEqual(target["answer"]["form"], "marker")
            self.assertEqual([r["type"] for r in self.records()], ["answer", "merged"])

    def test_supervisor_moves_after_merge_stays_merged(self) -> None:
        request = self.request(29, request_id="req-move-1")
        self.store.answer(**request)
        supervisor_merge(self.path, self.status_of("req-move-1"))
        self.store.mark_merged("req-move-1")
        data = self.path.read_bytes().decode("utf-8").split("\n")
        moved = data.pop(28).replace("- [ ] ", "- [x] ", 1) + " 已照办。"
        data.insert(data.index("## 已定") + 1, moved)
        self.path.write_bytes("\n".join(data).encode("utf-8"))
        self.assertEqual(self.status_of("req-move-1")["status"], "merged")

    def test_locator_mismatch_needs_review_and_answer_kept(self) -> None:
        cases = {
            "title_edited": lambda d: d.replace("夹具 ZQX 三选一".encode(), "夹具 ZQX 改过的三选一".encode()),
            "entry_deleted": lambda d: b"\n".join(l for l in d.split(b"\n") if "夹具 ZQX 三选一".encode() not in l),
            "moved_unmerged": lambda d: d.replace("- [ ] 2026-01-06 · 主管甲 · 夹具 ZQX 三选一".encode(),
                                                  "- [x] 2026-01-06 · 主管甲 · 夹具 ZQX 三选一".encode()),
            "moved_to_decided": move_to_decided_without_merge,
            "answered_differently": lambda d: d.replace("01-09 起按 A 办。".encode(), "01-09 起按 A 办。 不".encode()),
        }
        reasons = {"title_edited": "locator_not_found", "entry_deleted": "locator_not_found",
                   "moved_unmerged": "entry_moved_unmerged", "moved_to_decided": "entry_moved_unmerged",
                   "answered_differently": "entry_answered_differently"}
        original = (TESTDATA / "structure.md").read_bytes()
        for name, edit in cases.items():
            self.path.write_bytes(original)
            self.answers.unlink(missing_ok=True)
            self.store.answer(**self.request(29, request_id=f"req-mm-{name}"))
            self.path.write_bytes(edit(original))
            status = self.status_of(f"req-mm-{name}")
            self.assertEqual((status["status"], status.get("reason")), ("needs_review", reasons[name]), name)
            self.assertEqual(status["answer"], "行")
            self.assert_code("needs_review", self.store.mark_merged, f"req-mm-{name}")
            self.assertEqual(self.store.mark_handled(f"req-mm-{name}")["status"], "handled")
            self.assertEqual(self.status_of(f"req-mm-{name}")["status"], "handled")

    def test_pending_cli(self) -> None:
        self.store.answer(**self.request(29, request_id="req-cli-0001"))
        completed = subprocess.run([sys.executable, str(SVC / "inbox_answers.py"), "--inbox", str(self.path), "pending"],
                                   capture_output=True, text=True, timeout=30)
        self.assertEqual(completed.returncode, 0)
        self.assertIn("[待合并] req-cli-0001 第 29 行行尾追加： 你答：行", completed.stdout)
        completed = subprocess.run([sys.executable, str(SVC / "inbox_answers.py"), "--inbox", str(self.path),
                                    "mark-merged", "--request-id", "req-cli-0001"], capture_output=True, text=True, timeout=30)
        self.assertEqual((completed.returncode, completed.stderr.strip()), (1, "ERROR: not_merged_yet"))


class OverlayTests(AnswerTestCase):
    def test_read_view_overlays_unmerged_answer_without_text(self) -> None:
        self.store.answer(**self.request(29, "change", "B，夹具 ZQX 机密理由", request_id="req-ov-0001"))
        view = inbox_answers.inbox_view(str(self.path))
        entry = entry_at(view, 29)
        self.assertEqual(entry["owner_answer"]["status"], "pending_merge")
        self.assertEqual(entry["owner_answer"]["kind"], "change")
        self.assertFalse(entry["answerable"])
        self.assertEqual(entry["unanswerable_reason"], "answer_pending_merge")
        self.assertEqual(view["answers"]["counts"]["pending_merge"], 1)
        self.assertNotIn("ZQX", json.dumps(view, ensure_ascii=False))
        self.assertEqual(entry["owner_answer"]["answer_sha256"], inbox.sha256_hex("改成B，夹具 ZQX 机密理由".encode()))


class ConcurrencyTests(AnswerTestCase):
    """任何情况下主管/owner 对收件箱的修改都不丢；答复也不丢。"""

    def test_editor_holding_old_fd_writes_during_answer(self) -> None:
        held = open(self.path, "ab")
        original = self.path.read_bytes()
        edit = "- 2026-01-09 · 主管甲 · 夹具 ZQX 编辑器持旧描述符写入。\n".encode("utf-8")

        def write_through_old_fd() -> None:
            held.write(edit)
            held.flush()

        self.store.hooks["before_append"] = write_through_old_fd
        try:
            self.store.answer(**self.request(29, data=original))
        finally:
            held.close()
        self.assertEqual(self.path.read_bytes(), original + edit)
        self.assertEqual(len(self.records()), 1)

    def test_atomic_save_during_answer(self) -> None:
        original = self.path.read_bytes()
        saved = original.replace("夹具 ZQX 补报".encode(), "夹具 ZQX 改报".encode())

        def editor_save() -> None:
            temp = self.root / ".editor"
            temp.write_bytes(saved)
            os.replace(temp, self.path)

        self.store.hooks["before_append"] = editor_save
        request = self.request(29, request_id="req-save-001")
        self.store.answer(**request)
        self.assertEqual(self.path.read_bytes(), saved)
        self.assertEqual(self.status_of("req-save-001")["status"], "pending_merge")  # 第 29 行没被改，仍能定位

    def test_atomic_save_that_edits_the_answered_entry(self) -> None:
        original = self.path.read_bytes()
        saved = original.replace("夹具 ZQX 三选一".encode(), "夹具 ZQX 主管改写的三选一".encode())

        def editor_save() -> None:
            temp = self.root / ".editor"
            temp.write_bytes(saved)
            os.replace(temp, self.path)

        self.store.hooks["before_append"] = editor_save
        self.store.answer(**self.request(29, request_id="req-save-002"))
        self.assertEqual(self.path.read_bytes(), saved)
        status = self.status_of("req-save-002")
        self.assertEqual((status["status"], status["reason"]), ("needs_review", "locator_not_found"))
        self.assertEqual(status["answer"], "行")

    def test_merge_check_concurrent_with_supervisor_edit(self) -> None:
        self.store.answer(**self.request(29, request_id="req-mc-0001"))
        supervisor_merge(self.path, self.status_of("req-mc-0001"))
        merged_bytes = self.path.read_bytes()
        other_edit = merged_bytes + "- 2026-01-09 · 主管乙 · 夹具 ZQX 并发编辑。\n".encode("utf-8")

        original_records = AnswerStore._records

        def records_then_edit(store):
            result = original_records(store)
            self.path.write_bytes(other_edit)  # mark-merged 读记录后、读收件箱前，主管又改了
            return result

        AnswerStore._records = records_then_edit
        try:
            self.assertEqual(self.store.mark_merged("req-mc-0001")["status"], "merged")
        finally:
            AnswerStore._records = original_records
        self.assertEqual(self.path.read_bytes(), other_edit)

    def test_many_threads_and_processes_append_intact_lines(self) -> None:
        shutil.copyfile(TESTDATA / "structure.md", self.path)
        data = self.path.read_bytes()
        lines = [26, 29, 30, 31]
        barrier = threading.Barrier(len(lines) * 2)

        def run(index: int) -> str:
            line = lines[index % len(lines)]
            barrier.wait()
            try:
                self.store.answer(**self.request(line, request_id=f"req-t{index:04d}", data=data))
                return "recorded"
            except InboxError as exc:
                return exc.code

        with ThreadPoolExecutor(max_workers=len(lines) * 2) as pool:
            outcomes = list(pool.map(run, range(len(lines) * 2)))
        self.assertEqual(outcomes.count("recorded"), 4)
        self.assertEqual(outcomes.count("entry_already_answered"), 4)
        self.assertEqual(self.path.read_bytes(), data)
        # 多进程同时追加（flock 跨进程）：每行完整、条数正确
        script = (
            "import sys; sys.path.insert(0, %r)\n"
            "from inbox_answers import AnswerStore\n"
            "s = AnswerStore(sys.argv[1])\n"
            "for i in range(20):\n"
            "    with s._locked() as fd:\n"
            "        raw, _ = s._records()\n"
            "        s._append(fd, raw, {'type': 'handled', 'request_id': sys.argv[2] + str(i)})\n"
        ) % str(SVC)
        procs = [subprocess.Popen([sys.executable, "-c", script, str(self.path), f"p{n}-"]) for n in range(4)]
        for proc in procs:
            self.assertEqual(proc.wait(timeout=60), 0)
        raw = self.answers.read_bytes()
        records, corrupt = inbox_answers.parse_records(raw)
        self.assertEqual(corrupt, [])
        self.assertEqual(len(records), 4 + 80)


if __name__ == "__main__":
    unittest.main()
