"""owner 收件箱（00.projects/owner-inbox.md）的结构化解析与答复落点计算（n077）。

只做纯计算：输入文件字节，输出结构；不读写磁盘、不联网。文件 IO 在 inbox_writer.py。

输出遵守 n075 的读取约束：人写文本一律不出现在结果里，只给
``{field, chars, bytes, sha256, byte_start, byte_end}`` 引用，GUI 拿结果里的文件路径在本机自己打开、
按字节切片并核对 sha256。结果里只出现格式受控的值：日期、枚举、选项字母、Graph 标识、行号与字节偏移。

条目格式见 n077 交付物 inbox-format.md：
- 旧格式（legacy）：``- [ ] 日期 · 主会话 · 事项。默认：……；不回复就在某日按默认办``，一条一行；
  更早的条目没有勾选框或没有主会话字段，也照读。
- 新格式（v2）：标题行同旧格式，下面每个字段一行 ``  - 键：值``，键只认 KEYS 里的几个。
解析不了的条目原样保留为 ``status: unparsed``，带字节范围与原因码，不丢、不猜。
"""

from __future__ import annotations

from dataclasses import dataclass
import datetime as _dt
import hashlib
import re
from typing import Any


SCHEMA_VERSION = 1
MAX_INBOX_BYTES = 4 * 1024 * 1024

SECTION_TRUST = "信任档位"
SECTION_PENDING = "待你定"
SECTION_DECIDED = "已定"
SECTION_KEYS = {SECTION_TRUST: "trust", SECTION_PENDING: "pending", SECTION_DECIDED: "decided"}

# 新格式字段键（顺序即推荐书写顺序；解析不强制顺序，但每个键最多一次）。
V2_FIELD_KEYS = {
    "背景": "background",
    "依据": "basis",
    "选项": "options",
    "推荐": "recommendation",
    "默认": "default",
    "动作": "action",
}
ANSWER_KEYS = ("你答", "答")
TRUST_HEADER = ["领域", "档位", "起始", "下次评估"]

# 写回时追加的答复标记（与 owner 手写「你答：……」相同）。
ANSWER_MARKER = "你答："
MAX_CHANGE_TEXT_CHARS = 500

_HEADER_RE = re.compile(r"^- (?:\[(?P<box>[ xX])\] )?(?P<date>\d{4}-\d{2}-\d{2}) · (?P<rest>.*\S)\s*$")
_SUPERVISOR_FORBIDDEN = set("，。；：:、,.;!?！？「」“”\"'`*[]()（）【】<>《》/\\|")
_V2_LINE_RE = re.compile(r"^ {2,4}- (?P<key>[^：:\s]{1,4})[：:][ \t　]*(?P<value>.*?)[ \t　]*$")
_LIST_START_RE = re.compile(r"^(?:[-*+] |\d+[.)] )")
_HEADING_RE = re.compile(r"^(?P<hashes>#{1,6})[ \t]+(?P<title>.*?)[ \t#]*$")
_TABLE_RE = re.compile(r"^\|.*\|[ \t]*$")
_TABLE_SEPARATOR_RE = re.compile(r"^\|(?:[ \t]*:?-{3,}:?[ \t]*\|)+[ \t]*$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_ANSWER_VALUE = r"(?P<answer>行|不|改成[ \t　]*\S.*?)"
# 自成一行的答复：「你答：行」「- 你答：不」「行」「改成 B」。
_OWN_LINE_ANSWER_RE = re.compile(
    r"^[ \t]*(?:[-*][ \t]+)?(?:(?P<marker>你答|答)[：:][ \t　]*)?" + _ANSWER_VALUE + r"[ \t　]*$"
)
# 行尾带标记的答复：「……办。 你答：改成 B」。标记前必须是空白或句末标点，避免吃掉正文里的「某某答：」。
_INLINE_MARKER_RE = re.compile(
    r"(?:(?<=[\s　。；;！？!?，,）)\]】」』\"”*`])|^)(?P<marker>你答|答)[：:][ \t　]*" + _ANSWER_VALUE + r"[ \t　]*$"
)
# 行尾裸写的「行」「不」：前面必须是空白或句末标点。
_BARE_SHORT_RE = re.compile(r"(?<=[\s　。；;！？!?）)\]】」』\"”*`])(?P<answer>行|不)[ \t　]*$")
# 行尾裸写的「改成……」：前面是句末标点（可再跟空白），且这段里没有再出现「改成」。只用于「可能已答」。
_BARE_CHANGE_RE = re.compile(r"(?<=[。；;！？!?）)\]】」』\"”*`])[ \t　]*(?P<answer>改成(?:(?!改成).)+?)[ \t　]*$")

_OPTION_RE = re.compile(r"^(?P<label>[A-Z])[ ．.、：:]?[ \t　]*(?P<text>\S.*)$")
_RECOMMEND_LABEL_RE = re.compile(r"^(?P<label>[A-Z])(?:$|[ ，,。；;：:、（(])")
_RECOMMEND_BINARY_RE = re.compile(r"^(?P<label>行|不)(?:$|[ ，,。；;：:、（(])")
_DEFAULT_RE = re.compile(r"^(?P<day>\d{4}-\d{2}-\d{2})[ \t　]+(?P<text>\S.*)$")
_ACTION_GRAPH_RE = re.compile(r"^graph[ \t]+(?P<project>[a-z0-9][a-z0-9-]{0,63})/(?P<node>[A-Za-z0-9_]{1,64})$")
_ACTION_STAFF_RE = re.compile(r"^staff[ \t]+(?P<definition>[a-z0-9][a-z0-9_-]{0,63})$")

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f​-‏‪-‮⁦-⁩﻿]")
_SECRET_LIKE_RE = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|glpat-[A-Za-z0-9_-]{16,}|AKIA[0-9A-Z]{16}"
    r"|xox[abpr]-[A-Za-z0-9-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.)"
)


class InboxError(Exception):
    """解析或落点计算失败；code 是固定错误码。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Line:
    number: int  # 1 起
    start: int  # 行首字节偏移
    end: int  # 行尾（不含换行）字节偏移
    eol: bytes  # b"\n"、b"\r\n" 或 b""（文件末行无换行）
    text: str


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def split_lines(data: bytes) -> list[Line]:
    if len(data) > MAX_INBOX_BYTES:
        raise InboxError("inbox_too_large")
    try:
        data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise InboxError("inbox_invalid_encoding") from exc
    lines: list[Line] = []
    position = 0
    number = 0
    if data.startswith(b"\xef\xbb\xbf"):
        position = 3
    while position < len(data):
        newline = data.find(b"\n", position)
        if newline < 0:
            end, eol, following = len(data), b"", len(data)
        else:
            end, eol, following = newline, b"\n", newline + 1
            if end > position and data[end - 1 : end] == b"\r":
                end -= 1
                eol = b"\r\n"
        number += 1
        lines.append(Line(number, position, end, eol, data[position:end].decode("utf-8")))
        position = following
    return lines


def _char_to_byte(line: Line, char_index: int) -> int:
    return line.start + len(line.text[:char_index].encode("utf-8"))


def text_ref(data: bytes, field: str, start: int, end: int) -> dict[str, Any]:
    raw = data[start:end]
    return {
        "field": field,
        "chars": len(raw.decode("utf-8")),
        "bytes": len(raw),
        "sha256": sha256_hex(raw),
        "byte_start": start,
        "byte_end": end,
    }


def _line_ref(data: bytes, field: str, line: Line, start_char: int, end_char: int | None = None) -> dict[str, Any]:
    end_char = len(line.text) if end_char is None else end_char
    return text_ref(data, field, _char_to_byte(line, start_char), _char_to_byte(line, end_char))


def _valid_date(value: str) -> bool:
    if not _DATE_RE.match(value):
        return False
    try:
        _dt.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _span(lines: list[Line]) -> dict[str, int]:
    return {
        "line_start": lines[0].number,
        "line_end": lines[-1].number,
        "byte_start": lines[0].start,
        "byte_end": lines[-1].end,
    }


def _block(data: bytes, kind: str, lines: list[Line], reason: str | None = None) -> dict[str, Any]:
    block = {"kind": kind, **_span(lines), "sha256": sha256_hex(data[lines[0].start : lines[-1].end])}
    if reason:
        block["reason"] = reason
    return block


@dataclass(frozen=True)
class Answer:
    kind: str  # yes / no / change
    form: str  # marker / bare / own_line
    line: Line
    start_char: int  # 答复（含标记）在该行的起点，之前的部分仍属正文
    value_start_char: int  # 「行」「不」「改成……」本身的起点


def _answer_kind(value: str) -> str:
    if value == "行":
        return "yes"
    if value == "不":
        return "no"
    return "change"


def detect_answer(line: Line, *, allow_own_line: bool, allow_bare: bool) -> Answer | None:
    """在条目最后一行找 owner 的答复。找不到返回 None；只认文档写明的几种写法。

    allow_own_line：多行条目的最后一行可以整行是答复（「你答：行」「- 你答：不」「行」「改成 B」）。
    allow_bare：旧格式行尾可以裸写「行」「不」；新格式不认行尾裸写（字段值里可能本来就以「不」结尾）。
    行尾裸写的「改成……」不认作答复：真实文件里主管正文也会在句末标点后以「改成」起句，认了就是猜。
    这种写法由 possibly_answered() 检出，只用来拒绝写回，不当作已答。
    """
    text = line.text
    if allow_own_line:
        match = _OWN_LINE_ANSWER_RE.match(text)
        if match:
            form = "marker" if match.group("marker") else "own_line"
            return Answer(_answer_kind(match.group("answer")), form, line, 0, match.start("answer"))
    patterns = [(_INLINE_MARKER_RE, "marker")]
    if allow_bare:
        patterns.append((_BARE_SHORT_RE, "bare"))
    for pattern, form in patterns:
        match = pattern.search(text)
        if match:
            start = match.start("marker") if "marker" in pattern.groupindex else match.start("answer")
            return Answer(_answer_kind(match.group("answer")), form, line, start, match.start("answer"))
    return None


def possibly_answered(line: Line) -> bool:
    """行尾像是手写答复但不能确定（裸写的「改成……」，或新格式字段行尾的「行」「不」）。只用于拒绝写回。"""
    return bool(_BARE_CHANGE_RE.search(line.text) or _BARE_SHORT_RE.search(line.text))


def _answer_dict(data: bytes, answer: Answer) -> dict[str, Any]:
    result: dict[str, Any] = {"kind": answer.kind, "form": answer.form, "line": answer.line.number}
    if answer.kind == "change":
        result["text"] = _line_ref(data, "answer", answer.line, answer.value_start_char)
    return result


def _strip_answer_end(line: Line, answer: Answer | None) -> int:
    """正文在这一行的结束字符位置（去掉答复与其前面的空白）。"""
    if answer is None or answer.line.number != line.number:
        return len(line.text)
    return len(line.text[: answer.start_char].rstrip(" \t　"))


def _parse_entry(data: bytes, section: str, lines: list[Line]) -> dict[str, Any]:
    raw = data[lines[0].start : lines[-1].end]
    entry: dict[str, Any] = {
        "entry_id": f"L{lines[0].number}-{sha256_hex(raw)[:16]}",
        "section": section,
        **_span(lines),
        "sha256": sha256_hex(raw),
    }

    def unparsed(reason: str) -> dict[str, Any]:
        return {**entry, "status": "unparsed", "reason": reason, "answerable": False,
                "unanswerable_reason": "entry_unparsed"}

    header = lines[0]
    match = _HEADER_RE.match(header.text)
    if not match or not _valid_date(match.group("date")):
        return unparsed("header_unrecognized")
    box = match.group("box")
    rest_start = match.start("rest")
    rest = match.group("rest")
    supervisor: tuple[int, int] | None = None
    title_start = rest_start
    if " · " in rest:
        candidate = rest.split(" · ", 1)[0]
        if 1 <= len(candidate) <= 20 and not (set(candidate) & _SUPERVISOR_FORBIDDEN) and candidate.strip() == candidate:
            supervisor = (rest_start, rest_start + len(candidate))
            title_start = rest_start + len(candidate) + 3

    def v2_key(line: Line) -> str | None:
        found = _V2_LINE_RE.match(line.text)
        return found.group("key") if found else None

    is_v2 = any(v2_key(line) in V2_FIELD_KEYS for line in lines[1:])
    answer = detect_answer(lines[-1], allow_own_line=len(lines) > 1, allow_bare=not is_v2)
    entry["checked"] = None if box is None else box != " "
    entry["date"] = match.group("date")
    entry["supervisor"] = _line_ref(data, "supervisor", header, *supervisor) if supervisor else None
    if is_v2:
        if supervisor is None:
            return unparsed("v2_supervisor_missing")
        parsed = _parse_v2(data, lines, header, title_start, answer)
        if isinstance(parsed, str):
            return unparsed(parsed)
        entry["format"] = "v2"
        entry.update(parsed)
    else:
        own_line = answer is not None and len(lines) > 1 and answer.start_char == 0
        body_lines = lines[:-1] if own_line else lines
        end_line = body_lines[-1]
        end_char = _strip_answer_end(end_line, answer)
        title_byte_start = _char_to_byte(header, title_start)
        title_byte_end = _char_to_byte(end_line, end_char)
        if title_byte_end <= title_byte_start:
            return unparsed("title_empty")
        entry.update(
            {
                "format": "legacy",
                "title": text_ref(data, "title", title_byte_start, title_byte_end),
                "background": None,
                "basis": None,
                "options": None,
                "recommendation": None,
                "default": _legacy_default(data, header, title_start, end_line, end_char),
                "action": None,
            }
        )
    entry["status"] = "parsed"
    entry["answer"] = _answer_dict(data, answer) if answer else None
    entry["answer_possible"] = answer is None and possibly_answered(lines[-1])
    if section != "pending":
        reason = "entry_not_pending"
    elif box is None:
        reason = "entry_no_checkbox"
    elif box != " ":
        reason = "entry_not_pending"
    elif answer is not None:
        reason = "entry_already_answered"
    elif possibly_answered(lines[-1]):
        reason = "entry_possibly_answered"
    else:
        reason = None
    entry["answerable"] = reason is None
    if reason:
        entry["unanswerable_reason"] = reason
    return entry


def _legacy_default(data: bytes, header: Line, title_start: int, end_line: Line, end_char: int) -> dict[str, Any] | None:
    """旧格式只在正文里恰好出现一次「默认：」时取出默认建议的字节范围；默认日不推断。"""
    if end_line.number != header.number:
        return None
    body = header.text[title_start:end_char]
    if body.count("默认：") != 1:
        return None
    start = title_start + body.index("默认：") + len("默认：")
    end = end_char
    for stop in ("；", "。"):
        position = header.text.find(stop, start, end_char)
        if position >= 0:
            end = min(end, position)
    if end <= start:
        return None
    return {"day": None, "text": _line_ref(data, "default", header, start, end)}


def _parse_v2(data: bytes, lines: list[Line], header: Line, title_start: int, answer: Answer | None) -> dict[str, Any] | str:
    title_end = len(header.text)
    if title_end <= title_start:
        return "title_empty"
    fields: dict[str, tuple[Line, int, int]] = {}
    for index, line in enumerate(lines[1:], start=1):
        match = _V2_LINE_RE.match(line.text)
        if not match:
            if index == len(lines) - 1 and answer is not None and answer.line is line and answer.start_char == 0:
                continue  # 最后一行整行是答复（「行」「改成 B」）
            return "v2_line_unrecognized"
        key = match.group("key")
        if key in ANSWER_KEYS:
            if index != len(lines) - 1 or answer is None or answer.line is not line:
                return "v2_answer_not_last"
            continue
        if key not in V2_FIELD_KEYS:
            return "v2_key_unknown"
        name = V2_FIELD_KEYS[key]
        if name in fields:
            return "v2_key_duplicate"
        value_start = match.start("value")
        value_end = match.end("value")
        if answer is not None and answer.line is line:
            value_end = _strip_answer_end(line, answer)
        if value_end <= value_start:
            return "v2_value_empty"
        fields[name] = (line, value_start, value_end)
    if "default" not in fields:
        return "v2_default_missing"

    def ref(name: str) -> dict[str, Any] | None:
        if name not in fields:
            return None
        line, start, end = fields[name]
        return _line_ref(data, name, line, start, end)

    def value(name: str) -> str:
        line, start, end = fields[name]
        return line.text[start:end]

    options = None
    labels: list[str] = []
    if "options" in fields:
        line, start, end = fields["options"]
        options = []
        cursor = start
        for segment in line.text[start:end].split("；"):
            seg_start = cursor
            cursor += len(segment) + 1
            stripped_left = len(segment) - len(segment.lstrip(" \t　"))
            trimmed = segment.strip(" \t　")
            match = _OPTION_RE.match(trimmed)
            if not match or match.group("label") in labels:
                return "v2_options_invalid"
            labels.append(match.group("label"))
            text_start = seg_start + stripped_left + match.start("text")
            text_end = seg_start + stripped_left + len(trimmed)
            options.append({"label": match.group("label"), "text": _line_ref(data, "option", line, text_start, text_end)})
        if len(options) < 2:
            return "v2_options_invalid"

    recommendation = None
    if "recommendation" in fields:
        text = value("recommendation")
        match = (_RECOMMEND_LABEL_RE if labels else _RECOMMEND_BINARY_RE).match(text)
        if not match or (labels and match.group("label") not in labels):
            return "v2_recommendation_invalid"
        recommendation = {"label": match.group("label"), "text": ref("recommendation")}

    match = _DEFAULT_RE.match(value("default"))
    if not match or not _valid_date(match.group("day")):
        return "v2_default_invalid"
    line, start, _ = fields["default"]
    default = {
        "day": match.group("day"),
        "text": _line_ref(data, "default", line, start + match.start("text"), fields["default"][2]),
    }

    action = None
    if "action" in fields:
        text = value("action")
        if text == "无":
            action = {"kind": "none"}
        elif (graph := _ACTION_GRAPH_RE.match(text)):
            action = {"kind": "graph", "project": graph.group("project"), "node": graph.group("node")}
        elif (staff := _ACTION_STAFF_RE.match(text)):
            action = {"kind": "staff", "definition": staff.group("definition")}
        else:
            action = {"kind": "text", "text": ref("action")}

    return {
        "title": _line_ref(data, "title", header, title_start, title_end),
        "background": ref("background"),
        "basis": ref("basis"),
        "options": options,
        "recommendation": recommendation,
        "default": default,
        "action": action,
    }


def _parse_trust(data: bytes, lines: list[Line]) -> tuple[dict[str, Any], list[list[Line]]]:
    """信任档位表。返回 (表结构, 表外的说明块)。"""
    notes: list[list[Line]] = []
    table: list[Line] = []
    current: list[Line] = []
    for line in lines:
        if not line.text.strip():
            if current:
                notes.append(current)
                current = []
            continue
        if _TABLE_RE.match(line.text):
            if current:
                notes.append(current)
                current = []
            table.append(line)
        else:
            current.append(line)
    if current:
        notes.append(current)
    if not table:
        return {"status": "missing", "rows": []}, notes

    def cells(line: Line) -> list[tuple[int, int]] | None:
        text = line.text.rstrip(" \t")
        if not (text.startswith("|") and text.endswith("|")):
            return None
        spans = []
        position = 1
        for raw in text[1:-1].split("|"):
            left = len(raw) - len(raw.lstrip(" \t"))
            right = len(raw.rstrip(" \t"))
            spans.append((position + left, position + max(left, right)))
            position += len(raw) + 1
        return spans

    header_cells = cells(table[0])
    header_text = [table[0].text[a:b] for a, b in header_cells] if header_cells else None
    if header_text != TRUST_HEADER or len(table) < 2 or not _TABLE_SEPARATOR_RE.match(table[1].text):
        return {"status": "unparsed", "reason": "trust_header_unrecognized", **_span(table),
                "sha256": sha256_hex(data[table[0].start : table[-1].end]), "rows": []}, notes
    rows = []
    for line in table[2:]:
        spans = cells(line)
        if spans is None or len(spans) != 4:
            rows.append({"status": "unparsed", "reason": "trust_row_shape", "line": line.number,
                         "byte_start": line.start, "byte_end": line.end})
            continue
        values = [line.text[a:b] for a, b in spans]
        since, nxt = values[2], values[3]
        if not _valid_date(since) or not (_valid_date(nxt) or nxt in ("—", "-", "")):
            rows.append({"status": "unparsed", "reason": "trust_row_date", "line": line.number,
                         "byte_start": line.start, "byte_end": line.end})
            continue
        tier = re.match(r"^(L[0-3])(?:$|[ \t（(])", values[1])
        rows.append(
            {
                "status": "parsed",
                "line": line.number,
                "domain": _line_ref(data, "domain", line, *spans[0]),
                "tier": tier.group(1) if tier else None,
                "tier_text": _line_ref(data, "tier_text", line, *spans[1]),
                "since": since,
                "next_review": nxt if _valid_date(nxt) else None,
            }
        )
    return {"status": "parsed", **_span(table), "rows": rows}, notes


def parse_inbox(data: bytes) -> dict[str, Any]:
    """解析整份收件箱。每个非空行都恰好落在一个块里（标题、说明、表、条目、未解析），不丢行。"""
    lines = split_lines(data)
    preamble: list[Line] = []
    sections: list[tuple[str, Line | None, list[Line]]] = []
    current_name = "preamble"
    current_heading: Line | None = None
    current: list[Line] = []
    for line in lines:
        heading = _HEADING_RE.match(line.text)
        if heading and len(heading.group("hashes")) <= 2:
            if current_name == "preamble":
                preamble = current
            else:
                sections.append((current_name, current_heading, current))
            title = heading.group("title").strip()
            current_name = SECTION_KEYS.get(title, "other") if len(heading.group("hashes")) == 2 else "title"
            current_heading = line
            current = []
        else:
            current.append(line)
    if current_name == "preamble":
        preamble = current
    else:
        sections.append((current_name, current_heading, current))

    result: dict[str, Any] = {
        "kind": "owner_inbox",
        "schema": SCHEMA_VERSION,
        "trust": {"status": "missing", "rows": []},
        "pending": [],
        "decided": [],
        "notes": [],
        "other": [],
        "headings": [],
    }
    seen_sections: dict[str, int] = {}

    def add_blocks(kind: str, groups: list[list[Line]], target: str) -> None:
        for group in groups:
            result[target].append(_block(data, kind, group))

    add_blocks("preamble", _paragraphs(preamble), "notes")
    for name, heading, body in sections:
        if heading is not None:
            result["headings"].append({"line": heading.number, "section": name,
                                       "byte_start": heading.start, "byte_end": heading.end})
        seen_sections[name] = seen_sections.get(name, 0) + 1
        if name in ("pending", "decided", "trust") and seen_sections[name] > 1:
            add_blocks("duplicate_section", _paragraphs(body), "other")
            continue
        if name == "trust":
            trust, notes = _parse_trust(data, body)
            result["trust"] = trust
            add_blocks("trust_note", notes, "notes")
        elif name in ("pending", "decided"):
            entries, notes = _split_entries(body)
            for group in entries:
                result[name].append(_parse_entry(data, name, group))
            add_blocks(f"{name}_note", notes, "notes")
        else:
            add_blocks(name, _paragraphs(body), "other")
    for name in ("pending", "decided"):
        if name not in seen_sections:
            result.setdefault("missing_sections", []).append(name)
    counts = {}
    for name in ("pending", "decided"):
        entries = result[name]
        counts[name] = {
            "total": len(entries),
            "parsed": sum(1 for e in entries if e["status"] == "parsed"),
            "unparsed": sum(1 for e in entries if e["status"] == "unparsed"),
            "legacy": sum(1 for e in entries if e.get("format") == "legacy"),
            "v2": sum(1 for e in entries if e.get("format") == "v2"),
            "answered": sum(1 for e in entries if e.get("answer")),
            "possibly_answered": sum(1 for e in entries if e.get("answer_possible")),
            "answerable": sum(1 for e in entries if e.get("answerable")),
        }
    result["counts"] = counts
    return result


def _paragraphs(lines: list[Line]) -> list[list[Line]]:
    groups: list[list[Line]] = []
    current: list[Line] = []
    for line in lines:
        if line.text.strip():
            current.append(line)
        elif current:
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups


def _split_entries(lines: list[Line]) -> tuple[list[list[Line]], list[list[Line]]]:
    """把一个分节切成条目与说明块。条目 = 「- 」开头的行 + 紧随的缩进行或懒续行（直到空行、下一个列表项）。"""
    entries: list[list[Line]] = []
    notes: list[list[Line]] = []
    current: list[Line] | None = None
    note: list[Line] = []
    for line in lines:
        text = line.text
        if not text.strip():
            if current:
                entries.append(current)
                current = None
            if note:
                notes.append(note)
                note = []
            continue
        if text.startswith("- "):
            if current:
                entries.append(current)
            if note:
                notes.append(note)
                note = []
            current = [line]
            continue
        if current is not None and (text[:1] in (" ", "\t") or not (_LIST_START_RE.match(text) or _TABLE_RE.match(text))):
            current.append(line)
            continue
        if current:
            entries.append(current)
            current = None
        note.append(line)
    if current:
        entries.append(current)
    if note:
        notes.append(note)
    return entries, notes


def find_entry(parsed: dict[str, Any], line_start: int, entry_sha256: str) -> dict[str, Any]:
    for section in ("pending", "decided"):
        for entry in parsed[section]:
            if entry["line_start"] == line_start:
                if entry["sha256"] != entry_sha256:
                    raise InboxError("entry_not_found")
                return entry
    raise InboxError("entry_not_found")


def normalize_answer(kind: Any, text: Any) -> str:
    """把写回请求的答复规范成要写进文件的字符串（不含标记）。不合法就抛固定错误码。"""
    if kind == "yes" and text in (None, ""):
        return "行"
    if kind == "no" and text in (None, ""):
        return "不"
    if kind != "change" or not isinstance(text, str):
        raise InboxError("answer_invalid")
    if _CONTROL_RE.search(text):
        raise InboxError("answer_invalid")
    stripped = text.strip(" 　")
    if not stripped or stripped != text or len(text) > MAX_CHANGE_TEXT_CHARS:
        raise InboxError("answer_invalid")
    if _SECRET_LIKE_RE.search(text):
        raise InboxError("answer_secret_like")
    value = text if text.startswith("改成") else "改成" + text
    # 写进去后必须能被同一解析器原样认回（例如不能以「改成」结尾却没有内容）。
    if not re.fullmatch(r"改成[ \t　]*\S.*", value):
        raise InboxError("answer_invalid")
    return value


def plan_answer(data: bytes, entry: dict[str, Any], answer_value: str) -> tuple[int, bytes]:
    """答复写回的落点与要插入的字节：纯插入，插入点前后的字节一个不改。

    - 旧格式（一条一行）：追加在条目最后一行行尾（换行之前）：`` 你答：行``。
    - 新格式：在条目最后一行之后插入一行 ``  - 你答：行``，换行符沿用该行的。
    """
    if entry.get("status") != "parsed":
        raise InboxError("entry_unparsed")
    if not entry.get("answerable"):
        raise InboxError(entry.get("unanswerable_reason") or "entry_not_pending")
    lines = split_lines(data)
    last = next((line for line in lines if line.number == entry["line_end"]), None)
    if last is None or last.end != entry["byte_end"]:
        raise InboxError("entry_not_found")
    marker = ANSWER_MARKER + answer_value
    if entry["format"] == "legacy":
        return last.end, (" " + marker).encode("utf-8")
    eol = last.eol or (b"\r\n" if any(line.eol == b"\r\n" for line in lines) else b"\n")
    inserted = ("  - " + marker).encode("utf-8")
    if last.eol:
        return last.end + len(last.eol), inserted + last.eol
    return last.end, eol + inserted
