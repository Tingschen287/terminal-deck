"""n077 收件箱解析、落点与往返一致的测试（纯计算，不碰磁盘上的真实收件箱）。

夹具 testdata/inbox/structure.md 照真实 owner-inbox.md 的结构合成（标题、说明、信任档位表、待你定的格式行与
连续空行、带勾选框/不带勾选框/不带主会话的条目、粗体与反引号、相邻与空行分隔），正文全部是合成文字，
每段人写文字都带记号 ZQX，用来检查任何结构化结果里都不出现正文。
"""

from __future__ import annotations

import json
from pathlib import Path
import unittest

import inbox
from inbox import InboxError


TESTDATA = Path(__file__).resolve().parent / "testdata" / "inbox"
CANARY = "ZQX"


def load(name: str) -> bytes:
    return (TESTDATA / name).read_bytes()


def entry_at(parsed: dict, line: int) -> dict:
    for section in ("pending", "decided"):
        for entry in parsed[section]:
            if entry["line_start"] == line:
                return entry
    raise AssertionError(f"no entry at line {line}")


def iter_refs(value):
    if isinstance(value, dict):
        if {"sha256", "byte_start", "byte_end", "field"} <= set(value):
            yield value
        for item in value.values():
            yield from iter_refs(item)
    elif isinstance(value, list):
        for item in value:
            yield from iter_refs(item)


def leaks(result: dict, data: bytes) -> list[str]:
    """结构化结果里出现的正文：记号 ZQX，或任何一段被引用文字的原文（≥2 字）。"""
    dumped = json.dumps(result, ensure_ascii=False)
    found = [CANARY] if CANARY in dumped else []
    for ref in iter_refs(result):
        text = data[ref["byte_start"] : ref["byte_end"]].decode("utf-8")
        if len(text) >= 2 and text in dumped:
            found.append(text)
    return found


def apply(data: bytes, line: int, value: str) -> bytes:
    parsed = inbox.parse_inbox(data)
    position, inserted = inbox.plan_answer(data, entry_at(parsed, line), value)
    return data[:position] + inserted + data[position:]


class ParseTests(unittest.TestCase):
    def test_structure_fixture_reads_like_real_file(self) -> None:
        data = load("structure.md")
        parsed = inbox.parse_inbox(data)
        self.assertEqual(parsed["counts"]["pending"]["total"], 4)
        self.assertEqual(parsed["counts"]["pending"]["parsed"], 4)
        self.assertEqual(parsed["counts"]["pending"]["answerable"], 4)
        self.assertEqual(parsed["counts"]["decided"]["total"], 8)
        self.assertEqual(parsed["counts"]["decided"]["unparsed"], 0)
        self.assertEqual([r["tier"] for r in parsed["trust"]["rows"]], ["L1", "L0", "L2", "L1", None])
        self.assertEqual(parsed["trust"]["rows"][4]["next_review"], None)
        decided = parsed["decided"]
        self.assertEqual([e["checked"] for e in decided], [True, True, True, None, None, None, None, None])
        self.assertEqual([e["supervisor"] is not None for e in decided], [True] * 6 + [False, False])
        for entry in parsed["pending"]:
            self.assertEqual(entry["format"], "legacy")
            self.assertIsNone(entry["answer"])

    def test_every_nonblank_line_is_accounted_for(self) -> None:
        for name in ("structure.md", "variants.md"):
            data = load(name)
            parsed = inbox.parse_inbox(data)
            covered = {h["line"] for h in parsed["headings"]}
            for block in parsed["notes"] + parsed["other"] + parsed["pending"] + parsed["decided"]:
                covered.update(range(block["line_start"], block["line_end"] + 1))
            if parsed["trust"].get("line_start"):
                covered.update(range(parsed["trust"]["line_start"], parsed["trust"]["line_end"] + 1))
            nonblank = {line.number for line in inbox.split_lines(data) if line.text.strip()}
            self.assertEqual(covered, nonblank, name)

    def test_no_text_in_structured_result(self) -> None:
        for name in ("structure.md", "variants.md"):
            data = load(name)
            parsed = inbox.parse_inbox(data)
            self.assertEqual(leaks(parsed, data), [], name)

    def test_leak_checker_positive_control(self) -> None:
        data = load("structure.md")
        parsed = inbox.parse_inbox(data)
        title = parsed["pending"][0]["title"]
        parsed["pending"][0]["title_text"] = data[title["byte_start"] : title["byte_end"]].decode("utf-8")
        self.assertTrue(leaks(parsed, data))

    def test_refs_slice_back_to_original_text(self) -> None:
        data = load("variants.md")
        parsed = inbox.parse_inbox(data)
        refs = list(iter_refs(parsed))
        self.assertGreater(len(refs), 30)
        for ref in refs:
            raw = data[ref["byte_start"] : ref["byte_end"]]
            self.assertEqual(inbox.sha256_hex(raw), ref["sha256"])
            self.assertEqual(len(raw.decode("utf-8")), ref["chars"])

    def test_v2_fields(self) -> None:
        data = load("variants.md")
        parsed = inbox.parse_inbox(data)
        first = entry_at(parsed, 11)
        self.assertEqual(first["format"], "v2")
        self.assertEqual([o["label"] for o in first["options"]], ["A", "B", "C"])
        self.assertEqual(first["recommendation"]["label"], "A")
        self.assertEqual(first["default"]["day"], "2026-01-13")
        self.assertEqual(first["action"], {"kind": "graph", "project": "demo-project", "node": "n003_demo"})

        def text(ref: dict) -> str:
            return data[ref["byte_start"] : ref["byte_end"]].decode("utf-8")

        self.assertEqual(text(first["title"]), "夹具 ZQX 新格式标题一")
        self.assertEqual(text(first["background"]), "夹具 ZQX 背景一句话")
        self.assertEqual(text(first["basis"]), "`demo/4.eval/reports/ZQX.md`")
        self.assertEqual(text(first["options"][1]["text"]), "夹具 ZQX 先修")
        self.assertEqual(text(first["default"]["text"]), "不回复按 A 办")
        self.assertEqual(text(first["supervisor"]), "主管甲")
        self.assertEqual(entry_at(parsed, 18)["recommendation"]["label"], "行")
        self.assertEqual(entry_at(parsed, 18)["action"], {"kind": "none"})
        self.assertEqual(entry_at(parsed, 22)["action"], {"kind": "staff", "definition": "reviewer-def"})
        self.assertEqual(entry_at(parsed, 25)["action"]["kind"], "text")

    def test_v2_default_without_day(self) -> None:
        """默认可以没有默认日（如对外消息「不回复就不发」，真实文件里已有这种写法）；像日期但不合法的仍拒。"""
        head = "## 待你定\n\n- [ ] 2026-01-10 · 主管甲 · 夹具 ZQX 对外消息\n  - 选项：A 发；B 不发\n"
        good = inbox.parse_inbox((head + "  - 默认：夹具 ZQX 不回复就不发\n").encode("utf-8"))["pending"][0]
        self.assertEqual((good["status"], good["default"]["day"]), ("parsed", None))
        for bad in ("2026-13-40 按 A 办", "2026-1-5 按 A 办"):
            entry = inbox.parse_inbox((head + f"  - 默认：{bad}\n").encode("utf-8"))["pending"][0]
            self.assertEqual((entry["status"], entry["reason"]), ("unparsed", "v2_default_invalid"), bad)

    def test_unparsed_entries_are_kept_not_guessed(self) -> None:
        parsed = inbox.parse_inbox(load("variants.md"))
        expected = {
            41: "v2_key_unknown",
            44: "v2_default_missing",
            46: "v2_recommendation_invalid",
            50: "header_unrecognized",
            51: "header_unrecognized",
            52: "v2_supervisor_missing",
        }
        unparsed = {e["line_start"]: e["reason"] for e in parsed["pending"] if e["status"] == "unparsed"}
        self.assertEqual(unparsed, expected)
        for line in expected:
            entry = entry_at(parsed, line)
            self.assertFalse(entry["answerable"])
            self.assertNotIn("title", entry)  # 不猜字段，只给原文范围
            self.assertIn("sha256", entry)
            self.assertLess(entry["byte_start"], entry["byte_end"])

    def test_owner_hand_written_answers(self) -> None:
        parsed = inbox.parse_inbox(load("variants.md"))
        cases = {28: ("yes", "bare"), 29: ("no", "bare"), 30: ("change", "marker"), 31: ("change", "own_line"),
                 35: ("no", "marker")}
        for line, (kind, form) in cases.items():
            answer = entry_at(parsed, line)["answer"]
            self.assertEqual((answer["kind"], answer["form"]), (kind, form), line)
            self.assertEqual(entry_at(parsed, line)["unanswerable_reason"], "entry_already_answered")
        # 行尾裸写「改成……」不当作答复，只标「可能已答」并拒绝写回
        self.assertIsNone(entry_at(parsed, 33)["answer"])
        self.assertTrue(entry_at(parsed, 33)["answer_possible"])
        self.assertEqual(entry_at(parsed, 33)["unanswerable_reason"], "entry_possibly_answered")
        # 正文里的「把 A 改成 B」不是答复
        self.assertIsNone(entry_at(parsed, 34)["answer"])
        self.assertTrue(entry_at(parsed, 34)["answerable"])
        # 新格式字段值以「不」结尾不是答复
        self.assertIsNone(entry_at(parsed, 38)["answer"])

    def test_answer_text_excluded_from_title(self) -> None:
        data = load("variants.md")
        parsed = inbox.parse_inbox(data)
        for line in (28, 29, 30, 31):
            entry = entry_at(parsed, line)
            title = data[entry["title"]["byte_start"] : entry["title"]["byte_end"]].decode("utf-8")
            self.assertTrue(title.endswith("按默认办。"), (line, title))

    def test_bad_encoding_and_size(self) -> None:
        with self.assertRaises(InboxError) as raised:
            inbox.parse_inbox(b"# x\n\xff\xfe\n")
        self.assertEqual(raised.exception.code, "inbox_invalid_encoding")
        with self.assertRaises(InboxError) as raised:
            inbox.parse_inbox(b"a" * (inbox.MAX_INBOX_BYTES + 1))
        self.assertEqual(raised.exception.code, "inbox_too_large")


class AnswerPlanTests(unittest.TestCase):
    def test_normalize_answer(self) -> None:
        self.assertEqual(inbox.normalize_answer("yes", None), "行")
        self.assertEqual(inbox.normalize_answer("no", ""), "不")
        self.assertEqual(inbox.normalize_answer("change", "B，先做 X"), "改成B，先做 X")
        self.assertEqual(inbox.normalize_answer("change", "改成 B"), "改成 B")
        bad = [("yes", "行"), ("maybe", None), ("change", ""), ("change", " B"), ("change", "B\n行"),
               ("change", "B\r"), ("change", "B‮"), ("change", "B\x00"), ("change", "改成"),
               ("change", "x" * 501), ("change", 3)]
        for kind, text in bad:
            with self.assertRaises(InboxError, msg=(kind, text)) as raised:
                inbox.normalize_answer(kind, text)
            self.assertEqual(raised.exception.code, "answer_invalid")
        with self.assertRaises(InboxError) as raised:
            inbox.normalize_answer("change", "用 sk-ABCDEFGHIJKLMNOPQRSTUV 试")
        self.assertEqual(raised.exception.code, "answer_secret_like")

    def test_legacy_append_is_pure_suffix_on_entry_line(self) -> None:
        data = load("structure.md")
        parsed = inbox.parse_inbox(data)
        entry = entry_at(parsed, 29)
        position, inserted = inbox.plan_answer(data, entry, "行")
        self.assertEqual(position, entry["byte_end"])
        self.assertEqual(inserted, " 你答：行".encode("utf-8"))
        self.assertEqual(data[position : position + 1], b"\n")

    def test_v2_append_is_new_last_line(self) -> None:
        data = load("variants.md")
        parsed = inbox.parse_inbox(data)
        entry = entry_at(parsed, 11)
        position, inserted = inbox.plan_answer(data, entry, "改成 B")
        self.assertEqual(inserted, "  - 你答：改成 B\n".encode("utf-8"))
        self.assertEqual(position, entry["byte_end"] + 1)

    def test_crlf_and_missing_final_newline(self) -> None:
        data = load("variants.md").replace(b"\n", b"\r\n")
        new = apply(data, 11, "行")
        self.assertIn("  - 动作：graph demo-project/n003_demo\r\n  - 你答：行\r\n".encode("utf-8"), new)
        legacy = apply(data, 34, "不")
        self.assertIn("的说明不是答复 你答：不\r\n".encode("utf-8"), legacy)
        tail = b"## \xe5\xbe\x85\xe4\xbd\xa0\xe5\xae\x9a\n\n- [ ] 2026-01-10 \xc2\xb7 A \xc2\xb7 T\n  - " + \
            "默认：2026-01-13 按 A 办".encode("utf-8")
        new_tail = apply(tail, 3, "行")
        self.assertEqual(new_tail, tail + "\n  - 你答：行".encode("utf-8"))

    def test_refuses_non_answerable(self) -> None:
        data = load("variants.md")
        parsed = inbox.parse_inbox(data)
        cases = {28: "entry_already_answered", 33: "entry_possibly_answered", 41: "entry_unparsed",
                 57: "entry_not_pending"}
        for line, code in cases.items():
            with self.assertRaises(InboxError, msg=line) as raised:
                inbox.plan_answer(data, entry_at(parsed, line), "行")
            self.assertEqual(raised.exception.code, code)
        structure = load("structure.md")
        with self.assertRaises(InboxError) as raised:
            inbox.plan_answer(structure, entry_at(inbox.parse_inbox(structure), 40), "行")
        self.assertEqual(raised.exception.code, "entry_not_pending")

    def test_find_entry_requires_matching_hash(self) -> None:
        parsed = inbox.parse_inbox(load("structure.md"))
        entry = entry_at(parsed, 29)
        self.assertIs(inbox.find_entry(parsed, 29, entry["sha256"]), entry)
        for line, sha in ((29, "0" * 64), (28, entry["sha256"])):
            with self.assertRaises(InboxError) as raised:
                inbox.find_entry(parsed, line, sha)
            self.assertEqual(raised.exception.code, "entry_not_found")


class RoundTripTests(unittest.TestCase):
    ANSWERS = (("行", "yes"), ("不", "no"), ("改成 B，夹具理由", "change"))

    def test_roundtrip_every_answerable_entry(self) -> None:
        checked = 0
        for name in ("structure.md", "variants.md"):
            data = load(name)
            before = inbox.parse_inbox(data)
            for entry in before["pending"]:
                if not entry["answerable"]:
                    continue
                for value, kind in self.ANSWERS:
                    position, inserted = inbox.plan_answer(data, entry, value)
                    new = data[:position] + inserted + data[position:]
                    self.assertEqual(new[:position], data[:position])
                    self.assertEqual(new[position + len(inserted) :], data[position:])
                    after = inbox.parse_inbox(new)
                    target = entry_at(after, entry["line_start"])
                    self.assertEqual(target["answer"]["kind"], kind)
                    self.assertEqual(target["answer"]["form"], "marker")
                    if kind == "change":
                        ref = target["answer"]["text"]
                        self.assertEqual(new[ref["byte_start"] : ref["byte_end"]].decode("utf-8"), value)
                    for field in ("format", "checked", "date", "section"):
                        self.assertEqual(target[field], entry[field])
                    for field in ("title", "supervisor", "background", "basis", "default"):
                        old_ref, new_ref = entry.get(field), target.get(field)
                        if isinstance(old_ref, dict) and "text" in old_ref:
                            old_ref, new_ref = old_ref["text"], new_ref["text"]
                        self.assertEqual((old_ref or {}).get("sha256"), (new_ref or {}).get("sha256"), field)
                    self.assertFalse(target["answerable"])
                    shift = 1 if entry["format"] == "v2" else 0
                    for section in ("pending", "decided"):
                        old_others = [e for e in before[section] if e is not entry]
                        new_others = [e for e in after[section] if e["line_start"] != entry["line_start"]]
                        self.assertEqual([e["sha256"] for e in old_others], [e["sha256"] for e in new_others])
                        self.assertEqual(
                            [e["line_start"] + (shift if e["line_start"] > entry["line_end"] else 0) for e in old_others],
                            [e["line_start"] for e in new_others],
                        )
                    self.assertEqual(after["trust"]["rows"][0]["tier"], before["trust"]["rows"][0]["tier"])
                    checked += 1
        self.assertEqual(checked, 3 * (4 + 6))


class TitleLintTests(unittest.TestCase):
    """owner 2026-11-02：标题涉及项目或节点时写 `[项目简称·节点简称 编号]`；裸节点 id 只给警告，不拦解析与写回。"""

    def test_bare_node_id_warns_canonical_does_not(self) -> None:
        parsed = inbox.parse_inbox(load("lint.md"))
        expected = {5: [], 6: ["title_bare_node_id"], 7: ["title_bare_node_id"], 8: ["title_bare_node_id"],
                    9: ["title_bare_node_id"], 12: []}
        self.assertEqual({e["line_start"]: e["warnings"] for e in parsed["pending"]}, expected)
        self.assertEqual(parsed["counts"]["pending"]["warned"], 4)
        # 只查标题：新格式背景里的裸编号不算；反引号里的路径不算
        self.assertEqual(entry_at(parsed, 12)["warnings"], [])
        self.assertEqual(entry_at(parsed, 5)["warnings"], [])
        # 已定区旧条目也只是警告，状态不变
        decided = parsed["decided"][0]
        self.assertEqual((decided["status"], decided["warnings"]), ("parsed", ["title_bare_node_id"]))

    def test_warning_does_not_change_parse_or_writeback(self) -> None:
        data = load("lint.md")
        parsed = inbox.parse_inbox(data)
        for line in (6, 7, 8, 9):
            entry = entry_at(parsed, line)
            self.assertEqual(entry["status"], "parsed")
            self.assertTrue(entry["answerable"], line)
            new = apply(data, line, "行")
            target = entry_at(inbox.parse_inbox(new), line)
            self.assertEqual(target["answer"]["kind"], "yes")
            self.assertEqual(target["warnings"], ["title_bare_node_id"])  # 答复不进标题，警告照旧

    def test_title_warnings_unit(self) -> None:
        cases = {
            "[安排·读取接口 n075] 第 6 轮": [],
            "[只读查库·查询取消调查 n005a] 额度": [],
            "依据 `infra/4.eval/reports/n003-sql.md`": [],
            "n075 第 6 轮": ["title_bare_node_id"],
            "[安排] n075_read_interface 收口": ["title_bare_node_id"],
            "拦截库n003 紧挨中文": ["title_bare_node_id"],
            "[只读查库·SQL 拦截库 n003] 与 n004 一起": ["title_bare_node_id"],
            "mv2_contract 第 3 轮": [],
            "evt_000123 与 an075x 不算": [],
        }
        for text, expected in cases.items():
            self.assertEqual(inbox.title_warnings(text), expected, text)

    def test_existing_fixtures_have_no_warnings(self) -> None:
        for name in ("structure.md", "variants.md"):
            parsed = inbox.parse_inbox(load(name))
            self.assertEqual(parsed["counts"]["pending"]["warned"], 0, name)
            self.assertEqual(parsed["counts"]["decided"]["warned"], 0, name)


def supervisor_moves_to_decided(data: bytes, line: int) -> bytes:
    """模拟主管照 playbook 办完：把条目从「待你定」挪到「已定」并打勾（主管手工做的事，写回服务不做）。"""
    lines = data.decode("utf-8").split("\n")
    parsed = inbox.parse_inbox(data)
    entry = entry_at(parsed, line)
    block = lines[entry["line_start"] - 1 : entry["line_end"]]
    del lines[entry["line_start"] - 1 : entry["line_end"]]
    block[0] = block[0].replace("- [ ] ", "- [x] ", 1)
    decided_at = lines.index("## 已定")
    lines[decided_at + 1 : decided_at + 1] = block
    return "\n".join(lines).encode("utf-8")


class OwnerHabitContrastTests(unittest.TestCase):
    """对照：owner 直接在文件里写答复 vs 经写回入口写答复，对解析与主管照办是一样的。"""

    def test_hand_written_and_writeback_answers_are_equivalent(self) -> None:
        data = load("structure.md")
        entry = entry_at(inbox.parse_inbox(data), 29)
        end = entry["byte_end"]
        suffixes = [
            ("yes", " 行", "行"),  # 行尾空格后裸写
            ("yes", "行", "行"),  # 紧跟句末标点裸写
            ("no", " 不", "不"),
            ("no", "\n不", "不"),  # 另起一行
            ("change", " 你答：改成 B", "改成 B"),
            ("change", "\n改成 B", "改成 B"),
            ("change", "\n你答：改成 B", "改成 B"),
        ]
        for kind, suffix, value in suffixes:
            hand = data[:end] + suffix.encode("utf-8") + data[end:]
            gui = apply(data, 29, value)
            hand_entry = entry_at(inbox.parse_inbox(hand), 29)
            gui_entry = entry_at(inbox.parse_inbox(gui), 29)
            self.assertEqual(hand_entry["answer"]["kind"], kind)
            self.assertEqual(gui_entry["answer"]["kind"], kind)
            self.assertEqual(hand_entry["title"]["sha256"], gui_entry["title"]["sha256"], suffix)
            self.assertEqual(hand_entry["title"]["sha256"], entry["title"]["sha256"], suffix)
            if kind == "change":
                self.assertEqual(hand_entry["answer"]["text"]["sha256"], gui_entry["answer"]["text"]["sha256"])
            # 两种答法下，条目都还在「待你定」、勾选框不变；写回服务不挪条目
            for version in (hand, gui):
                parsed = inbox.parse_inbox(version)
                self.assertEqual(entry_at(parsed, 29)["section"], "pending")
                self.assertIs(entry_at(parsed, 29)["checked"], False)
                self.assertEqual(parsed["counts"]["pending"]["total"], 4)
            # 主管照办（挪到已定、打勾）对两种答法一样可行
            for version in (hand, gui):
                moved = inbox.parse_inbox(supervisor_moves_to_decided(version, 29))
                self.assertEqual(moved["counts"]["pending"]["total"], 3)
                self.assertEqual(moved["counts"]["decided"]["total"], 9)
                self.assertEqual(moved["decided"][0]["answer"]["kind"], kind)
                self.assertIs(moved["decided"][0]["checked"], True)

    def test_hand_answer_blocks_second_answer_via_writeback(self) -> None:
        data = load("structure.md")
        entry = entry_at(inbox.parse_inbox(data), 30)
        hand = data[: entry["byte_end"]] + " 不".encode("utf-8") + data[entry["byte_end"] :]
        with self.assertRaises(InboxError) as raised:
            apply(hand, 30, "行")
        self.assertEqual(raised.exception.code, "entry_already_answered")

    def test_writeback_text_reads_naturally(self) -> None:
        data = load("structure.md")
        new = apply(data, 31, "行").decode("utf-8").split("\n")[30]
        self.assertTrue(new.endswith("不回复就 01-08 按默认办。 你答：行"))


if __name__ == "__main__":
    unittest.main()
