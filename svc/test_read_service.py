from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import contextlib
import io
import json
import os
from pathlib import Path
import socket
import stat
import tempfile
import threading
import time
import unittest

import read_service
from read_service import ProjectCtlClient, ReadServer, ReadServiceError


# attempt 7 曾有的调试开关名；attempt 8 起已删除，设置它也不得改变 stderr。
READ_DEBUG_ENV = "PROJECTCTL_READ_DEBUG"


class ReadServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        # 先去掉旧调试开关；个别测试显式设置它，验证仍然零命中。
        previous_debug = os.environ.pop(READ_DEBUG_ENV, None)
        if previous_debug is not None:
            self.addCleanup(os.environ.__setitem__, READ_DEBUG_ENV, previous_debug)
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.log = self.root / "calls.jsonl"
        self.projectctl = self.root / "fake_projectctl.py"
        self.projectctl.write_text(
            """import json
import sys
from pathlib import Path

log = Path(%r)
with log.open('a', encoding='utf-8') as handle:
    handle.write(json.dumps(sys.argv[1:]) + '\\n')
if 'read-file' in sys.argv and 'bad' in sys.argv:
    print(json.dumps({'ok': False, 'error': 'synthetic read failure'}))
    raise SystemExit(1)
if 'read-file' in sys.argv and 'coded' in sys.argv:
    print('raw detail SYNTH_SECRET_9f3a', file=sys.stderr)
    print(json.dumps({'ok': False, 'error': 'node_not_found: leaked SYNTH_SECRET_9f3a'}))
    raise SystemExit(1)
if 'read-file' in sys.argv and 'garbled' in sys.argv:
    print('raw detail SYNTH_SECRET_9f3a', file=sys.stderr)
    print('not json SYNTH_SECRET_9f3a')
    raise SystemExit(1)
if 'read-file' in sys.argv and 'uncoded' in sys.argv:
    print(json.dumps({'ok': False, 'error': 'SYNTH_SECRET_9f3a: not a code we know'}))
    raise SystemExit(1)
print(json.dumps({'ok': True, 'result': {'argv': sys.argv[1:]}}))
"""
            % str(self.log),
            encoding="utf-8",
        )
        self.client = ProjectCtlClient(str(self.projectctl), timeout=5)
        self.socket_path = self.root / "read.sock"
        self.server = ReadServer(str(self.socket_path), self.client)
        os.chmod(self.socket_path, 0o600)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
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
            response = connection.makefile("rb").readline()
        return json.loads(response)

    def calls(self) -> list:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def test_health_and_socket_are_local_and_owner_only(self) -> None:
        response = self.request({"op": "health"})
        self.assertTrue(response["ok"])
        self.assertEqual(response["result"]["status"], "ok")
        self.assertEqual(self.server.socket.family, socket.AF_UNIX)
        self.assertEqual(stat.S_IMODE(self.socket_path.stat().st_mode), 0o600)
        self.assertEqual(self.calls(), [])

    def test_four_read_operations_forward_only_to_read_subcommands(self) -> None:
        project = "/synthetic/project"
        requests = [
            {"op": "project", "project": project},
            {"op": "node", "project": project, "node_id": "n001"},
            {"op": "events", "project": project, "cursor": "evt_1", "limit": 2},
            {"op": "file", "project": project, "path": "evidence/note.txt"},
        ]
        responses = [self.request(request) for request in requests]
        self.assertTrue(all(response["ok"] for response in responses))
        commands = [call[call.index("--format") + 2] for call in self.calls()]
        self.assertEqual(commands, ["read-project", "read-node", "read-events", "read-file"])
        self.assertIn("--cursor", self.calls()[2])
        self.assertEqual(self.calls()[3][-2:], ["read-file", "evidence/note.txt"])

    def test_write_and_unknown_operations_are_rejected_without_calling_projectctl(self) -> None:
        for operation in ("start", "submit", "review", "write", "text", "amend", None):
            response = self.request({"op": operation, "project": "/synthetic/project"})
            self.assertEqual(
                response,
                {"ok": False, "error": "unsupported_operation", "message": "unsupported read operation"},
            )
        self.assertEqual(self.calls(), [])

    def test_projectctl_error_is_returned_clearly(self) -> None:
        response = self.request({"op": "file", "project": "/synthetic/project", "path": "bad"})
        self.assertEqual(
            response, {"ok": False, "error": "projectctl_failed", "message": "projectctl read failed"}
        )

    def test_errors_carry_only_fixed_codes_never_input_or_projectctl_text(self) -> None:
        marker = "SYNTH_SECRET_9f3a"
        coded = self.request({"op": "file", "project": "/synthetic/project", "path": "coded"})
        self.assertEqual(coded["error"], "node_not_found")
        uncoded = self.request({"op": "file", "project": "/synthetic/project", "path": "uncoded"})
        self.assertEqual(uncoded["error"], "projectctl_failed")
        responses = [coded, uncoded]
        for request in (
            {"op": marker, "project": marker},
            {"op": "node", "project": marker, "node_id": 7},
            {"op": "events", "project": marker, "limit": marker},
        ):
            responses.append(self.request(request))
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(5)
            connection.connect(str(self.socket_path))
            connection.sendall(b"{not json " + marker.encode() + b"\n")
            responses.append(json.loads(connection.makefile("rb").readline()))
        for response in responses:
            self.assertFalse(response["ok"])
            self.assertEqual(set(response), {"ok", "error", "message"})
            self.assertNotIn(marker, json.dumps(response))

    def _error_requests_with_marker(self, marker: str) -> list[dict]:
        responses = []
        for path in ("coded", "uncoded", "garbled", "bad"):
            responses.append(self.request({"op": "file", "project": "/synthetic/" + marker, "path": path}))
        for request in (
            {"op": marker, "project": marker},
            {"op": "node", "project": marker, "node_id": 7},
            {"op": "events", "project": marker, "limit": marker},
        ):
            responses.append(self.request(request))
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(5)
            connection.connect(str(self.socket_path))
            connection.sendall(b"{not json " + marker.encode() + b"\n")
            responses.append(json.loads(connection.makefile("rb").readline()))
        return responses

    def test_service_stderr_carries_only_fixed_codes_by_default(self) -> None:
        """R15：服务 stderr 默认只写固定错误码，不转写 projectctl stderr、异常文本或请求内容。"""
        marker = "SYNTH_SECRET_9f3a"
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            responses = self._error_requests_with_marker(marker)
            # 处理线程里逃出 handle() 的异常（socketserver.handle_error）也只写固定码。
            try:
                raise RuntimeError(marker)
            except RuntimeError:
                self.server.handle_error(None, None)
            read_service.log_code(marker)
        for response in responses:
            self.assertFalse(response["ok"])
            self.assertNotIn(marker, json.dumps(response))
        captured = stderr.getvalue()
        self.assertNotIn(marker, captured)
        self.assertNotIn("/synthetic", captured)
        lines = captured.splitlines()
        self.assertTrue(lines)
        for line in lines:
            self.assertRegex(line, r"^read service error: [a-z_]+$")
        self.assertIn("read service error: node_not_found", lines)
        self.assertIn("read service error: projectctl_invalid_response", lines)

    def test_service_stderr_stays_fixed_codes_with_old_debug_switch_set(self) -> None:
        """R15（attempt 8）：设置旧调试开关 PROJECTCTL_READ_DEBUG=1 后，同一组请求的服务
        stderr 仍零命中、每行都是固定码；服务不收集 projectctl 的 stderr。"""
        marker = "SYNTH_SECRET_9f3a"
        os.environ[READ_DEBUG_ENV] = "1"
        self.addCleanup(os.environ.pop, READ_DEBUG_ENV, None)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            responses = self._error_requests_with_marker(marker)
            try:
                raise RuntimeError(marker)
            except RuntimeError:
                self.server.handle_error(None, None)
        for response in responses:
            self.assertNotIn(marker, json.dumps(response))
        captured = stderr.getvalue()
        self.assertNotIn(marker, captured)
        self.assertNotIn("/synthetic", captured)
        for line in captured.splitlines():
            self.assertRegex(line, r"^read service error: [a-z_]+$")
        source = Path(read_service.__file__).read_text(encoding="utf-8")
        self.assertNotIn(READ_DEBUG_ENV, source)
        self.assertNotIn("subprocess.PIPE if", source)

    def test_invalid_limits_and_cursor_combination_are_rejected(self) -> None:
        requests = [
            {"op": "events", "project": "/synthetic/project", "limit": 0},
            {"op": "events", "project": "/synthetic/project", "limit": True},
            {"op": "events", "project": "/synthetic/project", "cursor": "a", "before": "b"},
            {"op": "file", "project": "/synthetic/project", "path": ""},
            {"op": "file", "project": "/synthetic/project", "path": 7},
        ]
        for request in requests:
            self.assertFalse(self.request(request)["ok"])
        self.assertEqual(self.calls(), [])

    def test_burst_connections_have_bounded_workers_and_large_backlog(self) -> None:
        self.assertGreaterEqual(self.server.request_queue_size, 32)
        with ThreadPoolExecutor(max_workers=32) as executor:
            responses = list(executor.map(lambda _: self.request({"op": "health"}), range(32)))
        self.assertTrue(all(response["ok"] for response in responses))
        self.assertEqual(self.calls(), [])

    def test_slow_connection_times_out_and_releases_capacity(self) -> None:
        socket_path = self.root / "limited.sock"
        server = ReadServer(
            str(socket_path), self.client, max_connections=1, connection_read_timeout=0.2
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        slow = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            slow.settimeout(2)
            slow.connect(str(socket_path))
            slow.sendall(b'{"op":"he')
            time.sleep(0.05)
            # 容量拒绝在 accept 时立即回一行错误并关闭连接。客户端先读、不先写：
            # 先写会和服务端关闭竞争（S7：BrokenPipeError），读则确定性拿到这一行。
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as extra:
                extra.settimeout(2)
                extra.connect(str(socket_path))
                busy = json.loads(extra.makefile("rb").readline())
            self.assertFalse(busy["ok"])
            self.assertEqual(busy["error"], "capacity_reached")
            timeout = json.loads(slow.makefile("rb").readline())
            self.assertEqual(
                timeout, {"ok": False, "error": "request_timeout", "message": "request read timed out"}
            )
            healthy = self.request({"op": "health"})
            self.assertTrue(healthy["ok"])
        finally:
            slow.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_missing_projectctl_fails_before_start(self) -> None:
        with self.assertRaises(ReadServiceError):
            ProjectCtlClient(str(self.root / "missing.py"))


if __name__ == "__main__":
    unittest.main()
