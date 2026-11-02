"""n077 答复写回的文件层测试：哈希比对、拒绝覆盖、临时文件 + 原子交换、只追加记录、幂等、并发。

全部在临时目录里用合成夹具做，不碰真实收件箱。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import threading
import unittest

import inbox
from inbox import InboxError
from inbox_writer import InboxWriter, WriteRejected, atomic_swap


TESTDATA = Path(__file__).resolve().parent / "testdata" / "inbox"


def entry_at(parsed: dict, line: int) -> dict:
    for section in ("pending", "decided"):
        for entry in parsed[section]:
            if entry["line_start"] == line:
                return entry
    raise AssertionError(line)


class WriterTestCase(unittest.TestCase):
    fixture = "structure.md"

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.inbox_dir = self.root / "projects"
        self.inbox_dir.mkdir()
        self.path = self.inbox_dir / "owner-inbox.md"
        shutil.copyfile(TESTDATA / self.fixture, self.path)
        os.chmod(self.path, 0o640)
        self.state = self.root / "state"
        self.writer = InboxWriter(str(self.path), str(self.state))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def data(self) -> bytes:
        return self.path.read_bytes()

    def request(self, line: int, kind: str = "yes", text: str | None = None, request_id: str = "req-0001",
                expected: str | None = None, data: bytes | None = None) -> dict:
        data = self.data() if data is None else data
        entry = entry_at(inbox.parse_inbox(data), line)
        return dict(request_id=request_id, expected_sha256=expected or inbox.sha256_hex(data), line_start=line,
                    entry_sha256=entry["sha256"], kind=kind, text=text)

    def log(self) -> list[dict]:
        path = self.state / "inbox-writeback.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def leftovers(self) -> list[str]:
        return sorted(name for name in os.listdir(self.inbox_dir) if name != "owner-inbox.md")

    def assert_rejected(self, code: str, call, *args, **kwargs) -> InboxError:
        with self.assertRaises(InboxError) as raised:
            call(*args, **kwargs)
        self.assertEqual(raised.exception.code, code)
        return raised.exception


class ApplyTests(WriterTestCase):
    def test_apply_legacy_is_pure_insertion(self) -> None:
        before = self.data()
        result = self.writer.answer(**self.request(29))
        after = self.data()
        entry = entry_at(inbox.parse_inbox(before), 29)
        inserted = " 你答：行".encode("utf-8")
        self.assertEqual(after, before[: entry["byte_end"]] + inserted + before[entry["byte_end"] :])
        self.assertEqual(result["sha256"], inbox.sha256_hex(after))
        self.assertEqual(result["previous_sha256"], inbox.sha256_hex(before))
        self.assertFalse(result["replayed"])
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o640)
        self.assertEqual(self.leftovers(), [])
        records = self.log()
        self.assertEqual([r["phase"] for r in records], ["prepared", "applied"])
        applied = records[-1]
        for key in ("ts", "request_id", "entry", "answer", "pre_sha256", "post_sha256"):
            self.assertIn(key, applied)
        self.assertEqual(applied["answer"], "行")
        self.assertEqual(applied["entry"]["line_start"], 29)
        self.assertEqual(applied["pre_sha256"], inbox.sha256_hex(before))
        self.assertEqual(applied["post_sha256"], inbox.sha256_hex(after))
        self.assertEqual(applied["observed_sha256"], applied["post_sha256"])
        self.assertEqual(stat.S_IMODE(os.stat(self.state / "inbox-writeback.jsonl").st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.state).st_mode), 0o700)
        target = entry_at(inbox.parse_inbox(after), 29)
        self.assertEqual(target["answer"]["kind"], "yes")
        self.assertEqual(result["entry"]["sha256"], target["sha256"])

    def test_change_answer_and_log_record(self) -> None:
        self.writer.answer(**self.request(30, "change", "B，夹具理由"))
        target = entry_at(inbox.parse_inbox(self.data()), 30)
        self.assertEqual(target["answer"]["kind"], "change")
        self.assertEqual(self.log()[-1]["answer"], "改成B，夹具理由")

    def test_stale_expected_hash_is_rejected_and_file_untouched(self) -> None:
        request = self.request(29)
        supervisor = self.data() + "- 2026-01-09 · 主管甲 · 夹具 ZQX 主管新写。\n".encode("utf-8")
        self.path.write_bytes(supervisor)
        error = self.assert_rejected("inbox_changed", self.writer.answer, **request)
        self.assertEqual(error.current_sha256, inbox.sha256_hex(supervisor))
        self.assertEqual(self.data(), supervisor)
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(self.log()[-1]["phase"], "rejected")
        self.assertEqual(self.log()[-1]["code"], "inbox_changed")

    def test_supervisor_edit_before_precheck_is_not_overwritten(self) -> None:
        supervisor = {}

        def edit() -> None:
            with open(self.path, "ab") as handle:  # 主管原地追加
                handle.write("- 2026-01-09 · 主管甲 · 夹具 ZQX 期间写入。\n".encode("utf-8"))
            supervisor["data"] = self.data()

        self.writer.hooks["before_precheck"] = edit
        self.assert_rejected("inbox_changed", self.writer.answer, **self.request(29))
        self.assertEqual(self.data(), supervisor["data"])
        self.assertEqual(self.leftovers(), [])

    def counted_writer(self) -> tuple[InboxWriter, list[str]]:
        calls: list[str] = []

        def swap(a: str, b: str) -> str:
            calls.append("swap")
            return atomic_swap(a, b)

        return InboxWriter(str(self.path), str(self.state), swap=swap), calls

    def test_supervisor_inplace_edit_right_before_swap_is_restored(self) -> None:
        supervisor = {}
        self.writer, swaps = self.counted_writer()

        def edit() -> None:
            with open(self.path, "r+b") as handle:  # 原地改写同一个 inode
                content = handle.read().replace("夹具 ZQX 补报".encode("utf-8"), "夹具 ZQX 改报".encode("utf-8"))
                handle.seek(0)
                handle.write(content)
                handle.truncate()
            supervisor["data"] = self.data()
            supervisor["ino"] = os.stat(self.path).st_ino

        self.writer.hooks["before_swap"] = edit
        self.assert_rejected("inbox_changed", self.writer.answer, **self.request(29))
        self.assertEqual(self.data(), supervisor["data"])
        self.assertEqual(os.stat(self.path).st_ino, supervisor["ino"])
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(swaps, ["swap", "swap"])  # 换上去、发现被改、换回来

    def test_supervisor_atomic_save_right_before_swap_is_restored(self) -> None:
        supervisor = {}
        self.writer, swaps = self.counted_writer()

        def editor_save() -> None:  # 编辑器式保存：写新文件再改名替换
            content = self.data() + "- 2026-01-09 · 主管乙 · 夹具 ZQX 编辑器保存。\n".encode("utf-8")
            tmp = self.inbox_dir / ".editor-save"
            tmp.write_bytes(content)
            os.replace(tmp, self.path)
            supervisor["data"] = content

        self.writer.hooks["before_swap"] = editor_save
        self.assert_rejected("inbox_changed", self.writer.answer, **self.request(29))
        self.assertEqual(self.data(), supervisor["data"])
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(swaps, ["swap", "swap"])

    def test_swap_unavailable_fails_closed(self) -> None:
        def no_swap(_a: str, _b: str) -> str:
            raise InboxError("swap_unavailable")

        writer = InboxWriter(str(self.path), str(self.state), swap=no_swap)
        before = self.data()
        self.assert_rejected("swap_unavailable", writer.answer, **self.request(29))
        self.assertEqual(self.data(), before)
        self.assertEqual(self.leftovers(), [])

    def test_atomic_swap_exchanges(self) -> None:
        a, b = self.root / "a", self.root / "b"
        a.write_bytes(b"A")
        b.write_bytes(b"B")
        self.assertIn(atomic_swap(str(a), str(b)), ("renamex_np", "renameat2"))
        self.assertEqual((a.read_bytes(), b.read_bytes()), (b"B", b"A"))


class IdempotencyTests(WriterTestCase):
    def test_same_request_replays_without_writing(self) -> None:
        request = self.request(29)
        first = self.writer.answer(**request)
        after_first = self.data()
        second = self.writer.answer(**request)
        self.assertTrue(second["replayed"])
        self.assertEqual(second["sha256"], first["sha256"])
        self.assertEqual(self.data(), after_first)
        self.assertEqual([r["phase"] for r in self.log()], ["prepared", "applied"])

    def test_request_id_reused_for_other_answer_conflicts(self) -> None:
        request = self.request(29)
        self.writer.answer(**request)
        other = dict(request, kind="no")
        self.assert_rejected("request_id_conflict", self.writer.answer, **other)

    def test_second_answer_to_same_entry_is_refused(self) -> None:
        self.writer.answer(**self.request(29))
        self.assert_rejected("entry_already_answered", self.writer.answer, **self.request(29, "no", request_id="req-0002"))

    def test_crash_after_swap_recovers_on_retry(self) -> None:
        request = self.request(29)
        before = self.data()

        def crash() -> None:
            raise RuntimeError("simulated crash after swap")

        self.writer.hooks["after_swap"] = crash
        with self.assertRaises(RuntimeError):
            self.writer.answer(**request)
        self.assertEqual([r["phase"] for r in self.log()], ["prepared"])
        self.assertEqual(len(self.leftovers()), 1)  # 换下来的旧文件还在
        self.writer.hooks.clear()
        result = self.writer.answer(**request)
        self.assertTrue(result["replayed"])
        self.assertEqual([r["phase"] for r in self.log()], ["prepared", "recovered"])
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(entry_at(inbox.parse_inbox(self.data()), 29)["answer"]["kind"], "yes")
        self.assertNotEqual(self.data(), before)

    def test_crash_before_swap_retries_cleanly(self) -> None:
        request = self.request(29)
        before = self.data()

        def crash() -> None:
            raise RuntimeError("simulated crash before swap")

        self.writer.hooks["before_swap"] = crash
        with self.assertRaises(RuntimeError):
            self.writer.answer(**request)
        self.assertEqual(self.data(), before)
        self.assertEqual(self.leftovers(), [])
        self.writer.hooks.clear()
        result = self.writer.answer(**request)
        self.assertFalse(result["replayed"])
        self.assertEqual([r["phase"] for r in self.log()], ["prepared", "prepared", "applied"])

    def test_prepared_but_file_changed_is_uncertain(self) -> None:
        request = self.request(29)

        def crash() -> None:
            raise RuntimeError("crash")

        self.writer.hooks["before_swap"] = crash
        with self.assertRaises(RuntimeError):
            self.writer.answer(**request)
        self.writer.hooks.clear()
        self.path.write_bytes(self.data() + b"\n")
        self.assert_rejected("request_uncertain", self.writer.answer, **request)

    def test_log_is_append_only(self) -> None:
        self.writer.answer(**self.request(29))
        snapshot = (self.state / "inbox-writeback.jsonl").read_bytes()
        self.assert_rejected("entry_already_answered", self.writer.answer, **self.request(29, request_id="req-0003"))
        self.writer.answer(**self.request(30, "no", request_id="req-0004"))
        grown = (self.state / "inbox-writeback.jsonl").read_bytes()
        self.assertTrue(grown.startswith(snapshot))
        self.assertGreater(len(grown), len(snapshot))

    def test_corrupt_log_stops_writes(self) -> None:
        self.state.mkdir(exist_ok=True)
        (self.state / "inbox-writeback.jsonl").write_bytes(b'{"phase": "prepared"\n')
        before = self.data()
        self.assert_rejected("log_failed", self.writer.answer, **self.request(29))
        self.assertEqual(self.data(), before)


class ConcurrencyTests(WriterTestCase):
    def test_two_writers_same_snapshot_only_one_applies(self) -> None:
        data = self.data()
        requests = [self.request(29, request_id="req-a001", data=data), self.request(30, request_id="req-b001", data=data)]
        barrier = threading.Barrier(2)

        def run(request: dict) -> str:
            barrier.wait()
            try:
                self.writer.answer(**request)
                return "applied"
            except InboxError as exc:
                return exc.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = sorted(pool.map(run, requests))
        self.assertEqual(outcomes, ["applied", "inbox_changed"])
        parsed = inbox.parse_inbox(self.data())
        self.assertEqual(parsed["counts"]["pending"]["answered"], 1)
        self.assertEqual(self.leftovers(), [])


class RefusalTests(WriterTestCase):
    fixture = "variants.md"

    def test_refusals_leave_file_untouched(self) -> None:
        before = self.data()
        cases = {28: "entry_already_answered", 33: "entry_possibly_answered", 41: "entry_unparsed",
                 57: "entry_not_pending"}
        for index, (line, code) in enumerate(cases.items()):
            self.assert_rejected(code, self.writer.answer, **self.request(line, request_id=f"req-r{index:03d}"))
        self.assert_rejected("entry_not_found", self.writer.answer,
                             **dict(self.request(11, request_id="req-r100"), entry_sha256="0" * 64))
        for kind, text in (("change", "B\n- [x] 伪造"), ("change", "x" * 600), ("change", "sk-ABCDEFGHIJKLMNOPQRSTU"),
                           ("approve", None)):
            with self.assertRaises(InboxError):
                self.writer.answer(**self.request(11, kind, text, request_id="req-r200"))
        for bad in (dict(request_id="x"), dict(expected_sha256="abc"), dict(line_start=0), dict(line_start=True)):
            self.assert_rejected("argument_invalid", self.writer.answer, **dict(self.request(11, request_id="req-r300"), **bad))
        self.assertEqual(self.data(), before)
        self.assertEqual(self.leftovers(), [])

    def test_v2_writeback_roundtrip_on_disk(self) -> None:
        self.writer.answer(**self.request(11, "change", "C，夹具理由"))
        target = entry_at(inbox.parse_inbox(self.data()), 11)
        self.assertEqual(target["answer"]["kind"], "change")
        self.assertEqual(target["line_end"], 18)
        self.assertEqual(target["options"][0]["label"], "A")


class UnsafeFileTests(WriterTestCase):
    def test_symlink_and_hardlink_are_refused(self) -> None:
        real = self.inbox_dir / "real.md"
        os.replace(self.path, real)
        os.symlink(real, self.path)
        request = self.request(29, data=real.read_bytes())
        self.assert_rejected("inbox_unsafe", self.writer.answer, **request)
        self.assertTrue(os.path.islink(self.path))
        os.unlink(self.path)
        os.link(real, self.path)
        self.assert_rejected("inbox_unsafe", self.writer.answer, **dict(request, request_id="req-0009"))
        self.assertEqual(real.read_bytes(), (TESTDATA / "structure.md").read_bytes())


if __name__ == "__main__":
    unittest.main()
