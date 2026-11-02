"""n077 写回入口的测试：身份判定（只有 owner 界面能答）、读写入口分开、socket 端到端。

身份用例用真实进程与真实 socket（macOS）：
- owner 界面程序由 testdata/owner_client_fixture.c 现编出来，登记为 --owner-client；
- 「owner 自己打开」= 脱离测试进程链（双 fork，父进程成为 launchd）后运行登记的程序；
- 主管/分身 = 本测试进程（Python，未登记）直接连；或把登记的程序当子进程拉起的 `claude`。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import inbox
from inbox_writer import InboxWriter
from inbox_write_service import WriteServer
import owner_identity
from owner_identity import IdentityError, OwnerClientPolicy
from read_service import ProjectCtlClient, ReadServer


SVC = Path(__file__).resolve().parent
TESTDATA = SVC / "testdata"
CANARY = "ZQX"

# 双 fork 后等到父进程变成 launchd（pid 1）再 exec：模拟 owner 从 Dock/Finder 打开，进程链上没有测试进程与 Agent。
DETACH = r"""
import os, sys, time
if os.fork() == 0:
    os.setsid()
    if os.fork() == 0:
        deadline = time.time() + 5
        while os.getppid() != 1 and time.time() < deadline:
            time.sleep(0.01)
        os.execv(sys.argv[1], sys.argv[1:])
    os._exit(0)
os.wait()
"""


def entry_at(parsed: dict, line: int) -> dict:
    for section in ("pending", "decided"):
        for entry in parsed[section]:
            if entry["line_start"] == line:
                return entry
    raise AssertionError(line)


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("cc"), "需要 macOS 与 cc")
class OwnerIdentityEndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.build = tempfile.TemporaryDirectory()
        build = Path(cls.build.name)
        cls.owner_client = build / "owner-gui"
        subprocess.run(["cc", "-O0", "-o", str(cls.owner_client), str(TESTDATA / "owner_client_fixture.c")], check=True)
        cls.unregistered = build / "other" / "owner-gui"
        cls.unregistered.parent.mkdir()
        shutil.copy2(cls.owner_client, cls.unregistered)
        cls.agent = build / "agentbin" / "claude"
        cls.agent.parent.mkdir()
        shutil.copy2(cls.owner_client, cls.agent)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.build.cleanup()

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir="/tmp")
        self.root = Path(self.temporary.name)
        self.inbox = self.root / "projects" / "owner-inbox.md"
        self.inbox.parent.mkdir()
        shutil.copyfile(TESTDATA / "inbox" / "structure.md", self.inbox)
        self.state = self.root / "state"
        self.socket_path = self.root / "w.sock"
        self.policy = OwnerClientPolicy([str(self.owner_client)])
        self.server = WriteServer(str(self.socket_path), InboxWriter(str(self.inbox), str(self.state)), self.policy.check)
        os.chmod(self.socket_path, 0o600)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        self.counter = 0

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temporary.cleanup()

    def answer_request(self, line: int = 29, kind: str = "yes", text: str | None = None) -> dict:
        data = self.inbox.read_bytes()
        entry = entry_at(inbox.parse_inbox(data), line)
        self.counter += 1
        return {"op": "answer", "request_id": f"req-e2e-{self.counter:04d}", "expected_sha256": inbox.sha256_hex(data),
                "entry": {"line_start": line, "sha256": entry["sha256"]}, "answer": {"kind": kind, "text": text}}

    def python_client(self, request: dict) -> dict:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(5)
            connection.connect(str(self.socket_path))
            connection.sendall(json.dumps(request).encode("utf-8") + b"\n")
            return json.loads(connection.makefile("rb").readline())

    def run_binary(self, argv_tail: list[str], request: dict, *, detached: bool, launcher: Path | None = None) -> dict:
        self.counter += 1
        request_file = self.root / f"req-{self.counter}.json"
        out_file = self.root / f"out-{self.counter}.json"
        request_file.write_bytes(json.dumps(request).encode("utf-8") + b"\n")
        args = argv_tail + [str(self.socket_path), str(request_file), str(out_file)]
        if detached:
            subprocess.run([sys.executable, "-c", DETACH, *args], check=True, timeout=10)
        else:
            subprocess.run(args, check=False, timeout=10)
        deadline = time.time() + 10
        while not out_file.exists():
            if time.time() > deadline:
                self.fail("fixture produced no response")
            time.sleep(0.02)
        return json.loads(out_file.read_bytes())

    # ---- 阳性对照 ----

    def test_owner_client_opened_by_owner_can_answer(self) -> None:
        before = self.inbox.read_bytes()
        response = self.run_binary([str(self.owner_client)], self.answer_request(), detached=True)
        self.assertTrue(response["ok"], response)
        self.assertFalse(response["result"]["replayed"])
        after = self.inbox.read_bytes()
        self.assertEqual(entry_at(inbox.parse_inbox(after), 29)["answer"]["kind"], "yes")
        self.assertEqual(response["result"]["sha256"], inbox.sha256_hex(after))
        self.assertNotEqual(before, after)

    # ---- 拒绝用例 ----

    def test_supervisor_or_worker_process_is_refused(self) -> None:
        before = self.inbox.read_bytes()
        response = self.python_client(self.answer_request())
        self.assertEqual(response["error"], "caller_not_owner_client")
        self.assertEqual(self.inbox.read_bytes(), before)

    def test_unregistered_copy_of_owner_client_is_refused(self) -> None:
        before = self.inbox.read_bytes()
        response = self.run_binary([str(self.unregistered)], self.answer_request(), detached=True)
        self.assertEqual(response["error"], "caller_not_owner_client")
        self.assertEqual(self.inbox.read_bytes(), before)

    def test_owner_client_launched_by_agent_is_refused(self) -> None:
        before = self.inbox.read_bytes()
        response = self.run_binary([str(self.agent), "spawn", str(self.owner_client)], self.answer_request(), detached=True)
        self.assertEqual(response["error"], "owner_client_under_agent")
        self.assertEqual(self.inbox.read_bytes(), before)

    def test_owner_client_run_from_this_session_follows_process_chain(self) -> None:
        """不脱离进程链直接运行登记的程序：本测试若在 Agent 会话里跑（如 Claude Code），必须被拒。"""
        try:
            self.policy._check_ancestry(os.getpid())
            under_agent = False
        except IdentityError as exc:
            under_agent = exc.code == "owner_client_under_agent"
        before = self.inbox.read_bytes()
        response = self.run_binary([str(self.owner_client)], self.answer_request(), detached=False)
        if under_agent:
            self.assertEqual(response.get("error"), "owner_client_under_agent")
            self.assertEqual(self.inbox.read_bytes(), before)
        else:
            self.assertTrue(response["ok"], response)

    def test_owner_client_replaced_after_start_is_refused(self) -> None:
        before = self.inbox.read_bytes()
        original = self.owner_client.read_bytes()
        try:
            self.owner_client.write_bytes(original)  # 同内容重写：inode/mtime 变了也算被换
            os.utime(self.owner_client, ns=(time.time_ns(), time.time_ns()))
            response = self.run_binary([str(self.owner_client)], self.answer_request(), detached=True)
            self.assertEqual(response["error"], "owner_client_changed")
            self.assertEqual(self.inbox.read_bytes(), before)
        finally:
            self.owner_client.write_bytes(original)

    def test_refusal_does_not_echo_request(self) -> None:
        request = self.answer_request(kind="change", text="B 夹具 ZQX 机密文字")
        response = self.python_client(request)
        self.assertNotIn(CANARY, json.dumps(response, ensure_ascii=False))
        self.assertEqual(set(response), {"ok", "error", "message"})

    def test_write_socket_serves_no_reads(self) -> None:
        response = self.run_binary([str(self.owner_client)], {"op": "inbox"}, detached=True)
        self.assertEqual(response["error"], "unsupported_operation")

    def test_inbox_changed_reports_current_hash_only(self) -> None:
        request = self.answer_request()
        changed = self.inbox.read_bytes() + "- 2026-01-09 · 主管甲 · 夹具 ZQX 期间写入。\n".encode("utf-8")
        self.inbox.write_bytes(changed)
        response = self.run_binary([str(self.owner_client)], request, detached=True)
        self.assertEqual(response["error"], "inbox_changed")
        self.assertEqual(response["current_sha256"], inbox.sha256_hex(changed))
        self.assertEqual(self.inbox.read_bytes(), changed)


class PolicyUnitTests(unittest.TestCase):
    def test_agent_process_detection(self) -> None:
        cases = [
            ("/Users/u/.local/share/claude/versions/2.1.316", [], True),
            ("/usr/local/bin/codex", ["codex"], True),
            ("/opt/homebrew/bin/node", ["node", "/x/node_modules/@anthropic-ai/claude-code/cli.js"], True),
            ("/usr/bin/python3", ["python3", "/x/bin/kimi"], True),
            ("/usr/local/bin/opencode", [], True),
            ("/Applications/iTerm.app/Contents/MacOS/iTerm2", ["iTerm2"], False),
            ("/bin/zsh", ["-zsh"], False),
            ("/usr/bin/python3", ["python3", "/x/tool.py"], False),
            ("/sbin/launchd", [], False),
        ]
        for path, argv, expected in cases:
            self.assertEqual(owner_identity.is_agent_process(path, argv), expected, (path, argv))

    def test_unsupported_platform_fails_closed(self) -> None:
        policy = OwnerClientPolicy([sys.executable], platform="linux")
        left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        with left, right:
            with self.assertRaises(IdentityError) as raised:
                policy.check(left)
        self.assertEqual(raised.exception.code, "identity_unsupported_platform")

    def test_registration_rules(self) -> None:
        with self.assertRaises(IdentityError):
            OwnerClientPolicy([])
        with self.assertRaises(IdentityError):
            OwnerClientPolicy(["relative/path"])
        with tempfile.TemporaryDirectory() as folder:
            writable = Path(folder) / "gui"
            writable.write_bytes(b"#!/bin/sh\n")
            os.chmod(writable, 0o777)
            with self.assertRaises(IdentityError):
                OwnerClientPolicy([str(writable)])

    @unittest.skipUnless(sys.platform == "darwin", "需要 macOS")
    def test_python_peer_is_not_owner_client(self) -> None:
        policy = OwnerClientPolicy(["/bin/sh"])
        left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        with left, right:
            with self.assertRaises(IdentityError) as raised:
                policy.check(left)
        self.assertEqual(raised.exception.code, "caller_not_owner_client")


class ReadWriteSeparationTests(unittest.TestCase):
    """只读接口（read_service 的 op inbox）与写回入口（inbox_write_service）分开。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir="/tmp")
        self.root = Path(self.temporary.name)
        self.inbox = self.root / "projects" / "owner-inbox.md"
        self.inbox.parent.mkdir()
        shutil.copyfile(TESTDATA / "inbox" / "variants.md", self.inbox)
        fake = self.root / "projectctl.py"
        fake.write_text("print('{}')\n", encoding="utf-8")
        self.socket_path = self.root / "r.sock"
        self.server = ReadServer(str(self.socket_path), ProjectCtlClient(str(fake)), inbox_path=str(self.inbox))
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temporary.cleanup()

    def request(self, value: dict) -> dict:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(5)
            connection.connect(str(self.socket_path))
            connection.sendall(json.dumps(value).encode("utf-8") + b"\n")
            return json.loads(connection.makefile("rb").readline())

    def test_read_service_returns_structure_without_text(self) -> None:
        response = self.request({"op": "inbox"})
        self.assertTrue(response["ok"])
        result = response["result"]
        data = self.inbox.read_bytes()
        self.assertEqual(result["file"]["sha256"], inbox.sha256_hex(data))
        self.assertEqual(result["file"]["path"], str(self.inbox))
        self.assertEqual(result["counts"]["pending"]["total"], 18)
        self.assertEqual(result["trust"]["rows"][0]["tier"], "L1")
        self.assertEqual(len(result["decided"]), 1)
        dumped = json.dumps(response, ensure_ascii=False)
        self.assertNotIn(CANARY, dumped)
        # GUI 侧：按结果里的字节范围在本机切片，能还原原文并核对 sha256（阳性对照：原文确实含记号）
        title = result["pending"][0]["title"]
        raw = data[title["byte_start"] : title["byte_end"]]
        self.assertIn(CANARY, raw.decode("utf-8"))
        self.assertEqual(inbox.sha256_hex(raw), title["sha256"])

    def test_read_service_returns_title_warnings(self) -> None:
        shutil.copyfile(TESTDATA / "inbox" / "lint.md", self.inbox)
        response = self.request({"op": "inbox"})
        self.assertTrue(response["ok"])
        warnings = {e["line_start"]: e["warnings"] for e in response["result"]["pending"]}
        self.assertEqual(warnings[5], [])
        self.assertEqual(warnings[6], ["title_bare_node_id"])
        self.assertEqual(response["result"]["counts"]["pending"]["warned"], 4)
        self.assertNotIn(CANARY, json.dumps(response, ensure_ascii=False))

    def test_read_service_cannot_write(self) -> None:
        before = self.inbox.read_bytes()
        stat_before = os.stat(self.inbox)
        entry = entry_at(inbox.parse_inbox(before), 11)
        for op in ("answer", "inbox_answer", "write"):
            response = self.request({"op": op, "request_id": "req-00000001", "expected_sha256": inbox.sha256_hex(before),
                                     "entry": {"line_start": 11, "sha256": entry["sha256"]},
                                     "answer": {"kind": "yes"}, "project": str(self.root)})
            self.assertFalse(response["ok"])
        for _ in range(20):
            self.assertTrue(self.request({"op": "inbox"})["ok"])
        self.assertEqual(self.inbox.read_bytes(), before)
        self.assertEqual(os.stat(self.inbox).st_mtime_ns, stat_before.st_mtime_ns)
        self.assertEqual(sorted(os.listdir(self.inbox.parent)), ["owner-inbox.md"])

    def test_inbox_path_cannot_come_from_request(self) -> None:
        response = self.request({"op": "inbox", "path": "/etc/passwd", "project": "/etc"})
        self.assertTrue(response["ok"])
        self.assertEqual(response["result"]["file"]["path"], str(self.inbox))

    def test_inbox_errors_are_fixed_codes(self) -> None:
        os.replace(self.inbox, self.root / "real.md")
        os.symlink(self.root / "real.md", self.inbox)
        self.assertEqual(self.request({"op": "inbox"})["error"], "inbox_unsafe")
        os.unlink(self.inbox)
        self.assertEqual(self.request({"op": "inbox"})["error"], "inbox_unreadable")
        self.inbox.write_bytes(b"\xff\xfe broken")
        self.assertEqual(self.request({"op": "inbox"})["error"], "inbox_invalid_encoding")

    def test_inbox_not_configured(self) -> None:
        fake = self.root / "projectctl.py"
        path = self.root / "r2.sock"
        server = ReadServer(str(path), ProjectCtlClient(str(fake)))
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.connect(str(path))
                connection.sendall(b'{"op":"inbox"}\n')
                response = json.loads(connection.makefile("rb").readline())
            self.assertEqual(response["error"], "inbox_not_configured")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
