#!/usr/bin/env python3
"""Local-only project read service backed by projectctl read subcommands."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import socket
import socketserver
import subprocess
import sys
from typing import Any


MAX_REQUEST_BYTES = 64 * 1024
MAX_EVENT_PAGE = 500
MAX_TEXT_BYTES = 5 * 1024 * 1024


class ReadServiceError(RuntimeError):
    pass


class ProjectCtlClient:
    def __init__(self, executable: str, timeout: float = 30.0) -> None:
        self.executable = os.path.abspath(executable)
        self.timeout = timeout
        if not os.path.isfile(self.executable):
            raise ReadServiceError("projectctl executable is not a file")

    def call(self, request: dict[str, Any]) -> dict[str, Any]:
        operation = request.get("op")
        project = request.get("project")
        if not isinstance(project, str) or not project:
            raise ReadServiceError("project must be a non-empty string")
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
                raise ReadServiceError("node_id must be a non-empty string")
            arguments.extend(["read-node", node_id])
        elif operation == "events":
            arguments.append("read-events")
            limit = request.get("limit", 50)
            if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_EVENT_PAGE:
                raise ReadServiceError("limit must be an integer between 1 and 500")
            arguments.extend(["--limit", str(limit)])
            cursor = request.get("cursor")
            before = request.get("before")
            if cursor is not None and before is not None:
                raise ReadServiceError("cursor and before are mutually exclusive")
            for name, value in (("cursor", cursor), ("before", before)):
                if value is not None:
                    if not isinstance(value, str) or not value:
                        raise ReadServiceError(name + " must be a non-empty string")
                    arguments.extend(["--" + name, value])
        elif operation == "text":
            path = request.get("path")
            if not isinstance(path, str) or not path:
                raise ReadServiceError("path must be a non-empty string")
            max_bytes = request.get("max_bytes", 1024 * 1024)
            if (
                isinstance(max_bytes, bool)
                or not isinstance(max_bytes, int)
                or not 1 <= max_bytes <= MAX_TEXT_BYTES
            ):
                raise ReadServiceError("max_bytes must be an integer between 1 and 5242880")
            arguments.extend(["read-text", path, "--max-bytes", str(max_bytes)])
        else:
            raise ReadServiceError("unsupported read operation")
        try:
            completed = subprocess.run(
                arguments,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ReadServiceError("projectctl read timed out") from exc
        except OSError as exc:
            raise ReadServiceError("cannot start projectctl: " + str(exc)) from exc
        try:
            response = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise ReadServiceError("projectctl returned invalid JSON") from exc
        if not isinstance(response, dict) or "ok" not in response:
            raise ReadServiceError("projectctl returned an invalid response")
        if completed.returncode != 0 or not response.get("ok"):
            error = response.get("error", "projectctl read failed")
            raise ReadServiceError(str(error)[:2000])
        return response


class ReadRequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        try:
            raw = self.rfile.readline(MAX_REQUEST_BYTES + 1)
            if not raw or len(raw) > MAX_REQUEST_BYTES or not raw.endswith(b"\n"):
                raise ReadServiceError("request must be one JSON line of at most 65536 bytes")
            try:
                request = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ReadServiceError("request is not valid JSON") from exc
            if not isinstance(request, dict):
                raise ReadServiceError("request must be a JSON object")
            response = self.server.dispatch(request)
        except ReadServiceError as exc:
            response = {"ok": False, "error": str(exc)}
        except Exception:
            response = {"ok": False, "error": "internal read service error"}
        self.wfile.write(json.dumps(response, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n")


class ReadServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, socket_path: str, client: ProjectCtlClient) -> None:
        self.client = client
        super().__init__(socket_path, ReadRequestHandler)

    def dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        if request.get("op") == "health":
            return {"ok": True, "result": {"status": "ok", "service": "anpai-read", "version": 1}}
        return self.client.call(request)


def serve(socket_path: str, projectctl: str) -> int:
    path = Path(socket_path).expanduser().absolute()
    if len(os.fsencode(path)) > 96:
        raise ReadServiceError("socket path is too long; use a path of at most 96 bytes")
    if path.exists() or path.is_symlink():
        raise ReadServiceError("socket path already exists; refusing to replace it")
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
            import threading

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
        raise ReadServiceError("invalid health response")
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
    except (ReadServiceError, OSError, json.JSONDecodeError) as exc:
        print("ERROR: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
