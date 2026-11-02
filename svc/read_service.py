#!/usr/bin/env python3
"""Local-only project read service backed by projectctl read subcommands.

The service relays metadata-only JSON: no evidence, review, loop-log or event
body text crosses the socket (projectctl returns whitelist projections only).
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
import signal
import socket
import socketserver
import subprocess
import sys
import threading
from typing import Any


MAX_REQUEST_BYTES = 64 * 1024
MAX_EVENT_PAGE = 500
CONNECTION_READ_TIMEOUT = 5.0
MAX_CONCURRENT_CONNECTIONS = 16
LISTEN_BACKLOG = 64


# 所有错误响应都是 {"ok": false, "error": <固定错误码>, "message": <固定说明>}，
# 不拼入任何请求内容、projectctl 输出或异常文本。服务 stderr 同样只写固定错误码：
# 默认不转写 projectctl 的 stderr、不写异常文本；只有显式设置 PROJECTCTL_READ_DEBUG=1
# 时才把原始细节写到本机 stderr 供调试（attempt 7，R15）。
READ_DEBUG_ENV = "PROJECTCTL_READ_DEBUG"
SERVICE_ERROR_MESSAGES = {
    "request_invalid": "request must be one JSON object line of at most 65536 bytes",
    "request_timeout": "request read timed out",
    "capacity_reached": "server connection limit reached",
    "unsupported_operation": "unsupported read operation",
    "argument_invalid": "request arguments are invalid",
    "projectctl_unavailable": "projectctl could not be started",
    "projectctl_timeout": "projectctl read timed out",
    "projectctl_invalid_response": "projectctl returned an invalid response",
    "projectctl_failed": "projectctl read failed",
    "internal_error": "internal read service error",
}
# projectctl 读取命令的固定错误码（见 projectctl READ_ERROR_MESSAGES）；svc 只转发错误码。
PROJECTCTL_READ_ERROR_CODES = frozenset(
    {
        "project_unresolved", "project_root_unsafe", "graph_unreadable", "graph_invalid", "graph_changed_retry",
        "ledger_unreadable", "ledger_invalid", "ledger_chain_invalid", "history_invalid",
        "readme_unreadable", "frontmatter_invalid", "node_not_found", "cursor_not_found",
        "argument_invalid", "path_outside_project", "file_not_found", "file_unreadable",
        "file_too_large", "record_invalid", "read_failed",
    }
)
ERROR_CODE_PREFIX_RE = re.compile(r"([a-z_]{1,40}):")


class ReadServiceError(RuntimeError):
    def __init__(self, code: str, message: str | None = None) -> None:
        if code not in SERVICE_ERROR_MESSAGES and code not in PROJECTCTL_READ_ERROR_CODES:
            code = "internal_error"
        self.code = code
        self.message = message or SERVICE_ERROR_MESSAGES.get(code, "projectctl read failed: " + code)
        super().__init__(f"{self.code}: {self.message}")

    def response(self) -> dict[str, Any]:
        return {"ok": False, "error": self.code, "message": self.message}


def read_debug_enabled() -> bool:
    return os.environ.get(READ_DEBUG_ENV) == "1"


def log_detail(code: str, detail: object) -> None:
    """服务 stderr：默认只有固定错误码；调试开关打开时才附原始细节。"""
    if code not in SERVICE_ERROR_MESSAGES and code not in PROJECTCTL_READ_ERROR_CODES:
        code = "internal_error"
    if read_debug_enabled():
        print(f"read service debug ({code}): {detail}", file=sys.stderr)
    else:
        print(f"read service error: {code}", file=sys.stderr)


class ProjectCtlClient:
    def __init__(self, executable: str, timeout: float = 30.0) -> None:
        self.executable = os.path.abspath(executable)
        self.timeout = timeout
        if not os.path.isfile(self.executable):
            raise ReadServiceError("projectctl_unavailable")

    def call(self, request: dict[str, Any]) -> dict[str, Any]:
        operation = request.get("op")
        project = request.get("project")
        if not isinstance(project, str) or not project:
            raise ReadServiceError("argument_invalid")
        arguments = [
            sys.executable,
            self.executable,
            "--project",
            project,
            "--format",
            "json",
        ]
        if operation == "project":
            arguments.append("read-project")
        elif operation == "node":
            node_id = request.get("node_id")
            if not isinstance(node_id, str) or not node_id:
                raise ReadServiceError("argument_invalid")
            arguments.extend(["read-node", node_id])
        elif operation == "events":
            arguments.append("read-events")
            limit = request.get("limit", 50)
            if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_EVENT_PAGE:
                raise ReadServiceError("argument_invalid")
            arguments.extend(["--limit", str(limit)])
            cursor = request.get("cursor")
            before = request.get("before")
            if cursor is not None and before is not None:
                raise ReadServiceError("argument_invalid")
            for name, value in (("cursor", cursor), ("before", before)):
                if value is not None:
                    if not isinstance(value, str) or not value:
                        raise ReadServiceError("argument_invalid")
                    arguments.extend(["--" + name, value])
        elif operation == "file":
            # 只取元数据（大小、sha256、mtime、账本引用）；正文不经 socket，
            # GUI 用响应里的 project_root 与 path 在本机自己打开。
            path = request.get("path")
            if not isinstance(path, str) or not path:
                raise ReadServiceError("argument_invalid")
            arguments.extend(["read-file", path])
        else:
            raise ReadServiceError("unsupported_operation")
        try:
            completed = subprocess.run(
                arguments,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                # 默认丢弃 projectctl 的 stderr，不转写；调试开关打开时才收集。
                stderr=subprocess.PIPE if read_debug_enabled() else subprocess.DEVNULL,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            log_detail("projectctl_timeout", exc)
            raise ReadServiceError("projectctl_timeout") from exc
        except OSError as exc:
            log_detail("projectctl_unavailable", exc)
            raise ReadServiceError("projectctl_unavailable") from exc
        if completed.stderr and read_debug_enabled():
            log_detail("projectctl_failed", completed.stderr.strip())
        try:
            response = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            log_detail("projectctl_invalid_response", exc)
            raise ReadServiceError("projectctl_invalid_response") from exc
        if not isinstance(response, dict) or "ok" not in response:
            raise ReadServiceError("projectctl_invalid_response")
        if completed.returncode != 0 or not response.get("ok"):
            error = response.get("error")
            match = ERROR_CODE_PREFIX_RE.match(error) if isinstance(error, str) else None
            code = match.group(1) if match else ""
            code = code if code in PROJECTCTL_READ_ERROR_CODES else "projectctl_failed"
            log_detail(code, "projectctl read returned an error")
            raise ReadServiceError(code)
        return response


class ReadRequestHandler(socketserver.StreamRequestHandler):
    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(self.server.connection_read_timeout)

    def handle(self) -> None:
        try:
            raw = self.rfile.readline(MAX_REQUEST_BYTES + 1)
            if not raw or len(raw) > MAX_REQUEST_BYTES or not raw.endswith(b"\n"):
                raise ReadServiceError("request_invalid")
            try:
                request = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ReadServiceError("request_invalid") from exc
            if not isinstance(request, dict):
                raise ReadServiceError("request_invalid")
            response = self.server.dispatch(request)
        except socket.timeout:
            response = ReadServiceError("request_timeout").response()
        except ReadServiceError as exc:
            response = exc.response()
        except Exception as exc:
            log_detail("internal_error", repr(exc))
            response = ReadServiceError("internal_error").response()
        try:
            self.wfile.write(
                json.dumps(response, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
            )
        except OSError:
            pass


class ReadServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    request_queue_size = LISTEN_BACKLOG

    def __init__(
        self,
        socket_path: str,
        client: ProjectCtlClient,
        *,
        max_connections: int = MAX_CONCURRENT_CONNECTIONS,
        connection_read_timeout: float = CONNECTION_READ_TIMEOUT,
    ) -> None:
        if max_connections < 1 or connection_read_timeout <= 0:
            raise ValueError("connection limits must be positive")
        self.client = client
        self.connection_read_timeout = connection_read_timeout
        self._connection_slots = threading.BoundedSemaphore(max_connections)
        super().__init__(socket_path, ReadRequestHandler)

    def process_request(self, request: socket.socket, client_address: Any) -> None:
        if not self._connection_slots.acquire(blocking=False):
            try:
                request.sendall(
                    json.dumps(ReadServiceError("capacity_reached").response()).encode("utf-8")
                    + b"\n"
                )
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._connection_slots.release()
            raise

    def process_request_thread(self, request: socket.socket, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._connection_slots.release()

    def handle_error(self, request: Any, client_address: Any) -> None:
        # socketserver 默认把完整 traceback 打到 stderr；这里只写固定错误码。
        log_detail("internal_error", repr(sys.exc_info()[1]))

    def dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        if request.get("op") == "health":
            return {"ok": True, "result": {"status": "ok", "service": "anpai-read", "version": 1}}
        return self.client.call(request)


def serve(socket_path: str, projectctl: str) -> int:
    path = Path(socket_path).expanduser().absolute()
    if len(os.fsencode(path)) > 96:
        raise ReadServiceError("argument_invalid", "socket path is too long; use a path of at most 96 bytes")
    if path.exists() or path.is_symlink():
        raise ReadServiceError("argument_invalid", "socket path already exists; refusing to replace it")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    client = ProjectCtlClient(projectctl)
    previous_umask = os.umask(0o177)
    try:
        server = ReadServer(str(path), client)
    finally:
        os.umask(previous_umask)
    try:
        os.chmod(path, 0o600)

        def stop(_signum: int, _frame: Any) -> None:
            threading.Thread(target=server.shutdown, daemon=True).start()

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
        path.unlink(missing_ok=True)
    return 0


def health(socket_path: str) -> dict[str, Any]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(5.0)
        connection.connect(socket_path)
        connection.sendall(b'{"op":"health"}\n')
        response = connection.makefile("rb").readline(MAX_REQUEST_BYTES + 1)
    value = json.loads(response)
    if not isinstance(value, dict):
        raise ReadServiceError("internal_error", "invalid health response")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Anpai local-only project read service")
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve_parser = subparsers.add_parser("serve")
    serve_parser.add_argument("--socket", required=True)
    serve_parser.add_argument("--projectctl", required=True)
    health_parser = subparsers.add_parser("health")
    health_parser.add_argument("--socket", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "serve":
            return serve(args.socket, args.projectctl)
        response = health(args.socket)
        print(json.dumps(response, ensure_ascii=False, indent=2, allow_nan=False))
        return 0 if response.get("ok") else 1
    except ReadServiceError as exc:
        # 固定错误码 + 本模块常量说明。
        print("ERROR: " + str(exc), file=sys.stderr)
        return 1
    except (OSError, json.JSONDecodeError) as exc:
        # OSError 文本含路径等运行时值：默认只写固定码。
        log_detail("internal_error", repr(exc))
        print("ERROR: internal_error: " + SERVICE_ERROR_MESSAGES["internal_error"], file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
