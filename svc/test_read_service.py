from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import socket
import stat
import tempfile
import threading
import time
import unittest

from read_service import ProjectCtlClient, ReadServer, ReadServiceError


class ReadServiceTests(unittest.TestCase):
    def setUp(self) -> None:
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
if 'read-text' in sys.argv and 'bad' in sys.argv:
    print(json.dumps({'ok': False, 'error': 'synthetic read failure'}))
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
            {"op": "text", "project": project, "path": "evidence/note.txt", "max_bytes": 128},
        ]
        responses = [self.request(request) for request in requests]
        self.assertTrue(all(response["ok"] for response in responses))
        commands = [call[call.index("--format") + 2] for call in self.calls()]
        self.assertEqual(commands, ["read-project", "read-node", "read-events", "read-text"])
        self.assertIn("--cursor", self.calls()[2])
        self.assertIn("--max-bytes", self.calls()[3])

    def test_write_and_unknown_operations_are_rejected_without_calling_projectctl(self) -> None:
        for operation in ("start", "submit", "review", "write", None):
            response = self.request({"op": operation, "project": "/synthetic/project"})
            self.assertFalse(response["ok"])
            self.assertIn("unsupported read operation", response["error"])
        self.assertEqual(self.calls(), [])

    def test_projectctl_error_is_returned_clearly(self) -> None:
        response = self.request({"op": "text", "project": "/synthetic/project", "path": "bad"})
        self.assertEqual(response, {"ok": False, "error": "synthetic read failure"})

    def test_invalid_limits_and_cursor_combination_are_rejected(self) -> None:
        requests = [
            {"op": "events", "project": "/synthetic/project", "limit": 0},
            {"op": "events", "project": "/synthetic/project", "limit": True},
            {"op": "events", "project": "/synthetic/project", "cursor": "a", "before": "b"},
            {"op": "text", "project": "/synthetic/project", "path": "x", "max_bytes": 0},
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
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as extra:
                extra.settimeout(2)
                extra.connect(str(socket_path))
                extra.sendall(b'{"op":"health"}\n')
                busy = json.loads(extra.makefile("rb").readline())
            self.assertFalse(busy["ok"])
            self.assertIn("connection limit", busy["error"])
            timeout = json.loads(slow.makefile("rb").readline())
            self.assertEqual(timeout, {"ok": False, "error": "request read timed out"})
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
