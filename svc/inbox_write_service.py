#!/usr/bin/env python3
"""owner 收件箱答复写回服务（n077）：与只读服务分开的进程、分开的 socket。

只有一种写操作 ``answer``：在「待你定」的一条条目末尾追加 owner 的答复（行 / 不 / 改成……）。
每条连接先做身份判定（owner_identity.OwnerClientPolicy），不是 owner 界面一律拒绝。
错误响应只有固定错误码与固定说明，不回显请求内容；stderr 也只写固定错误码。
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import socket
import socketserver
import sys
import threading
from typing import Any, Callable

from inbox import InboxError
from inbox_writer import InboxWriter, WriteRejected
from owner_identity import IdentityError, OwnerClientPolicy


MAX_REQUEST_BYTES = 16 * 1024
CONNECTION_READ_TIMEOUT = 5.0

ERROR_MESSAGES = {
    "request_invalid": "request must be one JSON object line of at most 16384 bytes",
    "request_timeout": "request read timed out",
    "unsupported_operation": "unsupported write operation",
    "argument_invalid": "request arguments are invalid",
    # 身份
    "caller_uid_mismatch": "caller is not this user",
    "caller_not_owner_client": "only the owner interface may answer inbox entries",
    "owner_client_under_agent": "owner interface was launched by an agent process",
    "owner_client_changed": "registered owner interface executable changed since service start",
    "owner_client_invalid": "owner interface executable is not acceptable",
    "identity_unavailable": "caller identity could not be established",
    "identity_unsupported_platform": "caller identity is not supported on this platform",
    # 收件箱与条目
    "inbox_changed": "inbox changed since it was read; re-read and try again",
    "inbox_unsafe": "inbox is not a single regular file",
    "inbox_unreadable": "inbox could not be read",
    "inbox_too_large": "inbox is too large",
    "inbox_invalid_encoding": "inbox is not valid UTF-8",
    "entry_not_found": "entry not found at that line with that hash",
    "entry_not_pending": "entry is not an open pending entry",
    "entry_no_checkbox": "entry has no checkbox",
    "entry_unparsed": "entry could not be parsed; answer it in the file",
    "entry_already_answered": "entry already has an answer",
    "entry_possibly_answered": "entry may already have a hand-written answer; check the file",
    "answer_invalid": "answer must be yes, no, or change with one line of text",
    "answer_secret_like": "answer text looks like a secret",
    # 写入过程
    "request_id_conflict": "request id was used with a different request",
    "request_uncertain": "previous attempt with this request id has an uncertain result",
    "writer_busy": "another write is in progress",
    "swap_unavailable": "filesystem does not support atomic exchange",
    "roundtrip_failed": "answer would not parse back; nothing written",
    "write_failed": "write failed; nothing replaced",
    "log_failed": "writeback record could not be written",
    "internal_error": "internal write service error",
}


def log_code(code: str) -> None:
    if code not in ERROR_MESSAGES:
        code = "internal_error"
    print(f"inbox write service error: {code}", file=sys.stderr)


def error_response(code: str, current_sha256: str | None = None) -> dict[str, Any]:
    if code not in ERROR_MESSAGES:
        code = "internal_error"
    response: dict[str, Any] = {"ok": False, "error": code, "message": ERROR_MESSAGES[code]}
    if current_sha256:
        response["current_sha256"] = current_sha256
    return response


class WriteRequestHandler(socketserver.StreamRequestHandler):
    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(self.server.connection_read_timeout)

    def handle(self) -> None:
        identity_error: str | None = None
        try:
            self.server.check_identity(self.connection)
        except IdentityError as exc:
            identity_error = exc.code
        except Exception:
            identity_error = "identity_unavailable"
        try:
            raw = self.rfile.readline(MAX_REQUEST_BYTES + 1)
            if identity_error:
                log_code(identity_error)
                response = error_response(identity_error)
            else:
                response = self._dispatch(raw)
        except socket.timeout:
            response = error_response(identity_error or "request_timeout")
        except Exception:
            log_code("internal_error")
            response = error_response("internal_error")
        try:
            self.wfile.write(json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n")
        except OSError:
            pass

    def _dispatch(self, raw: bytes) -> dict[str, Any]:
        if not raw or len(raw) > MAX_REQUEST_BYTES or not raw.endswith(b"\n"):
            return error_response("request_invalid")
        try:
            request = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return error_response("request_invalid")
        if not isinstance(request, dict):
            return error_response("request_invalid")
        op = request.get("op")
        if op == "health":
            return {"ok": True, "result": {"status": "ok", "service": "anpai-inbox-write", "version": 1}}
        if op != "answer":
            return error_response("unsupported_operation")
        entry = request.get("entry")
        answer = request.get("answer")
        if not isinstance(entry, dict) or not isinstance(answer, dict):
            return error_response("argument_invalid")
        try:
            # 读完请求后再核对一次身份：判定之后对端换了进程（exec 或 pid 复用）就拒绝。
            self.server.check_identity(self.connection)
            result = self.server.writer.answer(
                request_id=request.get("request_id"),
                expected_sha256=request.get("expected_sha256"),
                line_start=entry.get("line_start"),
                entry_sha256=entry.get("sha256"),
                kind=answer.get("kind"),
                text=answer.get("text"),
            )
        except IdentityError as exc:
            log_code(exc.code)
            return error_response(exc.code)
        except WriteRejected as exc:
            log_code(exc.code)
            return error_response(exc.code, exc.current_sha256)
        except InboxError as exc:
            log_code(exc.code)
            return error_response(exc.code)
        return {"ok": True, "result": result}


class WriteServer(socketserver.UnixStreamServer):
    """顺序处理连接：写回本来就要串行，也免得非 owner 的连接占满线程。"""

    def __init__(
        self,
        socket_path: str,
        writer: InboxWriter,
        check_identity: Callable[[socket.socket], int],
        *,
        connection_read_timeout: float = CONNECTION_READ_TIMEOUT,
    ) -> None:
        self.writer = writer
        self.check_identity = check_identity
        self.connection_read_timeout = connection_read_timeout
        super().__init__(socket_path, WriteRequestHandler)

    def handle_error(self, request: Any, client_address: Any) -> None:
        log_code("internal_error")


def serve(socket_path: str, inbox_path: str, state_dir: str, owner_clients: list[str]) -> int:
    path = Path(socket_path).expanduser().absolute()
    if len(os.fsencode(path)) > 96:
        raise InboxError("argument_invalid")
    if path.exists() or path.is_symlink():
        raise InboxError("argument_invalid")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.stat(path.parent).st_mode & 0o077:
        raise InboxError("argument_invalid")
    policy = OwnerClientPolicy(owner_clients)
    writer = InboxWriter(os.path.abspath(inbox_path), state_dir)
    previous_umask = os.umask(0o177)
    try:
        server = WriteServer(str(path), writer, policy.check)
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Anpai owner inbox answer service (owner interface only)")
    subparsers = parser.add_subparsers(dest="command", required=True)
    serve_parser = subparsers.add_parser("serve")
    serve_parser.add_argument("--socket", required=True)
    serve_parser.add_argument("--inbox", required=True)
    serve_parser.add_argument("--state-dir", required=True)
    serve_parser.add_argument("--owner-client", action="append", required=True,
                              help="absolute path of an owner interface executable; repeatable")
    args = parser.parse_args(argv)
    try:
        return serve(args.socket, args.inbox, args.state_dir, args.owner_client)
    except (InboxError, IdentityError) as exc:
        code = exc.code if exc.code in ERROR_MESSAGES else "internal_error"
        print(f"ERROR: {code}: {ERROR_MESSAGES[code]}", file=sys.stderr)
        return 1
    except OSError:
        log_code("internal_error")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
