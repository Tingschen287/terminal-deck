#!/usr/bin/env python3
"""Local-only project read service backed by projectctl read subcommands.

The service relays metadata-only JSON: no evidence, review, loop-log or event
body text crosses the socket (projectctl returns whitelist projections only).
The optional owner inbox view (op "inbox", n077) follows the same rule: entry
text is returned as byte ranges plus sha256, never as text.
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

from inbox import InboxError
from inbox_answers import inbox_view


MAX_REQUEST_BYTES = 64 * 1024
MAX_EVENT_PAGE = 500
CONNECTION_READ_TIMEOUT = 5.0
MAX_CONCURRENT_CONNECTIONS = 16
LISTEN_BACKLOG = 64


# 所有错误响应都是 {"ok": false, "error": <固定错误码>, "message": <固定说明>}，
# 不拼入任何请求内容、projectctl 输出或异常文本。服务 stderr 同样只写固定错误码：
# 任何运行模式下都不收集、不转写 projectctl 的 stderr，不写异常文本，没有调试开关
# （attempt 8，R15）。
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
    # 收件箱读取（n077）：文件路径由服务启动参数 --inbox 指定，请求里不能指定路径。
    "inbox_not_configured": "inbox read is not configured on this service",
    "inbox_unsafe": "inbox is not a single regular file",
    "inbox_unreadable": "inbox could not be read",
    "inbox_too_large": "inbox is too large",
    "inbox_invalid_encoding": "inbox is not valid UTF-8",
    "answers_unsafe": "answers file is not a single regular file",
    "answers_unreadable": "answers file could not be read",
    "answers_too_large": "answers file is too large",
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


def log_code(code: str) -> None:
    """服务 stderr 的唯一出口：只写固定错误码，不接受任何细节参数。"""
    if code not in SERVICE_ERROR_MESSAGES and code not in PROJECTCTL_READ_ERROR_CODES:
        code = "internal_error"
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
                # projectctl 的 stderr 一律丢弃，不收集、不转写。
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            log_code("projectctl_timeout")
            raise ReadServiceError("projectctl_timeout") from exc
        except OSError as exc:
            log_code("projectctl_unavailable")
            raise ReadServiceError("projectctl_unavailable") from exc
        try:
            response = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            log_code("projectctl_invalid_response")
            raise ReadServiceError("projectctl_invalid_response") from exc
        if not isinstance(response, dict) or "ok" not in response:
            raise ReadServiceError("projectctl_invalid_response")
        if completed.returncode != 0 or not response.get("ok"):
            error = response.get("error")
            match = ERROR_CODE_PREFIX_RE.match(error) if isinstance(error, str) else None
            code = match.group(1) if match else ""
            code = code if code in PROJECTCTL_READ_ERROR_CODES else "projectctl_failed"
            log_code(code)
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
            log_code("internal_error")
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
        inbox_path: str | None = None,
    ) -> None:
        if max_connections < 1 or connection_read_timeout <= 0:
            raise ValueError("connection limits must be positive")
        self.client = client
        self.inbox_path = os.path.abspath(inbox_path) if inbox_path else None
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
        log_code("internal_error")

    def dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        if request.get("op") == "health":
            return {"ok": True, "result": {"status": "ok", "service": "anpai-read", "version": 1}}
        if request.get("op") == "inbox":
            return self.read_inbox()
        return self.client.call(request)

    def read_inbox(self) -> dict[str, Any]:
        """收件箱的结构化结果：待你定、已定、信任档位表，叠加答复文件里未合并的答复。
        只读、不写盘，人写文本只给字节范围与 sha256，答复只给 request_id 与 sha256。

        写回答复不在本服务：见 inbox_write_service.py（另一个进程、另一个 socket，只给 owner 界面）。
        """
        if not self.inbox_path:
            raise ReadServiceError("inbox_not_configured")
        try:
            result = inbox_view(self.inbox_path)
        except InboxError as exc:
            code = exc.code if exc.code in SERVICE_ERROR_MESSAGES else "internal_error"
            log_code(code)
            raise ReadServiceError(code) from exc
        return {"ok": True, "result": result}


def serve(socket_path: str, projectctl: str, inbox_path: str | None = None) -> int:
    path = Path(socket_path).expanduser().absolute()
    if len(os.fsencode(path)) > 96:
        raise ReadServiceError("argument_invalid", "socket path is too long; use a path of at most 96 bytes")
    if path.exists() or path.is_symlink():
        raise ReadServiceError("argument_invalid", "socket path already exists; refusing to replace it")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    client = ProjectCtlClient(projectctl)
    previous_umask = os.umask(0o177)
    try:
        server = ReadServer(str(path), client, inbox_path=inbox_path)
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
    serve_parser.add_argument("--inbox", help="owner inbox file to serve read-only (op: inbox)")
    health_parser = subparsers.add_parser("health")
    health_parser.add_argument("--socket", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "serve":
            return serve(args.socket, args.projectctl, args.inbox)
        response = health(args.socket)
        print(json.dumps(response, ensure_ascii=False, indent=2, allow_nan=False))
        return 0 if response.get("ok") else 1
    except ReadServiceError as exc:
        # 固定错误码 + 本模块常量说明。
        print("ERROR: " + str(exc), file=sys.stderr)
        return 1
    except (OSError, json.JSONDecodeError) as exc:
        # OSError 文本含路径等运行时值：默认只写固定码。
        log_code("internal_error")
        print("ERROR: internal_error: " + SERVICE_ERROR_MESSAGES["internal_error"], file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
