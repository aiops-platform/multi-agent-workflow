# -*- coding: utf-8 -*-
"""沙箱 exec 服务（运行在沙箱 Pod 内，design §10.2 生产安全基线）。

**纯 Python stdlib 实现**（http.server + json）：镜像零 pip 依赖，
离线可构建（python:3.12-slim + 本文件即可）。

- 提供 /exec（shell）/python（执行代码）/write（写文件，白名单）/health
- 强制限制（§10.2）：
  - ``max_execution_seconds``：300s
  - ``max_stdout_bytes``：1MB
  - ``max_concurrent_processes``：10（线程信号量）
  - ``writable_allowlist``：仅 /workspace 与 /tmp（write 校验，resolve 兼容符号链接）

启动：``python -m agentflow.sandbox.exec_service [--port 44772] [--self-test]``
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

MAX_EXECUTION_SECONDS = int(os.environ.get("SBX_MAX_EXEC_SECONDS", 300))
MAX_STDOUT_BYTES = int(os.environ.get("SBX_MAX_STDOUT_BYTES", 1_048_576))  # 1MB
MAX_CONCURRENT = int(os.environ.get("SBX_MAX_CONCURRENT", 10))
WRITABLE_ALLOWLIST = [Path(p) for p in os.environ.get(
    "SBX_WRITABLE", "/workspace:/tmp"
).split(":") if p]

_sem = threading.Semaphore(MAX_CONCURRENT)


@dataclass
class ExecRequest:
    cmd: str = ""
    cwd: str | None = None
    timeout: int = MAX_EXECUTION_SECONDS


@dataclass
class PythonRequest:
    code: str = ""
    timeout: int = MAX_EXECUTION_SECONDS


@dataclass
class WriteRequest:
    path: str = ""
    content: str = ""


def _truncate(out: bytes) -> str:
    return out[:MAX_STDOUT_BYTES].decode(errors="replace") + (
        f"\n...[截断，超过 {MAX_STDOUT_BYTES} 字节]" if len(out) > MAX_STDOUT_BYTES else ""
    )


def _run_sync(args: list[str], cwd: str | None, timeout: int, **kw: Any) -> dict[str, Any]:
    with _sem:
        try:
            proc = subprocess.run(
                args, cwd=cwd, capture_output=True,
                timeout=min(timeout, MAX_EXECUTION_SECONDS), **kw,
            )
            timed_out = False
        except subprocess.TimeoutExpired:
            proc = subprocess.CompletedProcess(args, -1, b"", b"timed out")
            timed_out = True
        except FileNotFoundError as exc:
            return {"rc": 127, "stdout": "", "stderr": str(exc), "timed_out": False}
    return {
        "rc": proc.returncode,
        "stdout": _truncate(proc.stdout or b""),
        "stderr": _truncate(proc.stderr or b""),
        "timed_out": timed_out,
    }


async def exec_cmd(req: ExecRequest) -> dict:
    if not req.cmd.strip():
        return {"rc": 2, "stdout": "", "stderr": "cmd 为空", "timed_out": False}
    return await asyncio.to_thread(_run_sync, ["/bin/sh", "-c", req.cmd], req.cwd, req.timeout)


async def exec_python(req: PythonRequest) -> dict:
    if not req.code.strip():
        return {"rc": 2, "stdout": "", "stderr": "code 为空", "timed_out": False}
    return await asyncio.to_thread(_run_sync, [sys.executable, "-c", req.code], None, req.timeout)


async def write_file(req: WriteRequest) -> dict:
    p = Path(req.path)
    # §10.2 writable_allowlist（resolve 兼容 /tmp → /private/tmp 符号链接）
    resolved = str(p.resolve())
    allowed = any(
        resolved == str(base.resolve()) or resolved.startswith(str(base.resolve()) + "/")
        for base in WRITABLE_ALLOWLIST
    )
    if not allowed:
        return {"written": False, "error": f"路径不在可写白名单: {req.path}（仅 {WRITABLE_ALLOWLIST}）"}
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(req.content, encoding="utf-8")
    return {"written": True, "path": str(p), "bytes": len(req.content.encode("utf-8"))}


# ======================================================================
# stdlib HTTP 层
# ======================================================================
def _body(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length", 0) or 0)
    raw = handler.rfile.read(length) if length else b"{}"
    try:
        return json.loads(raw.decode("utf-8")) if raw else {}
    except json.JSONDecodeError:
        return {}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # 静默访问日志
        pass

    def _send(self, code: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _dispatch(self, route: str) -> None:
        body = _body(self)
        try:
            if route == "/exec":
                result = asyncio.run(exec_cmd(ExecRequest(**body)))
            elif route == "/python":
                result = asyncio.run(exec_python(PythonRequest(**body)))
            elif route == "/write":
                result = asyncio.run(write_file(WriteRequest(**body)))
            else:
                self._send(404, {"error": "not found"})
                return
            self._send(200, result)
        except Exception as exc:  # noqa: BLE001
            self._send(500, {"error": str(exc)})

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/exec":
            self._dispatch("/exec")
        elif self.path == "/python":
            self._dispatch("/python")
        elif self.path == "/write":
            self._dispatch("/write")
        else:
            self._send(404, {"error": f"unknown: {self.path}"})

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._send(200, {
                "status": "ok",
                "limits": {
                    "max_execution_seconds": MAX_EXECUTION_SECONDS,
                    "max_stdout_bytes": MAX_STDOUT_BYTES,
                    "max_concurrent_processes": MAX_CONCURRENT,
                    "writable_allowlist": [str(p) for p in WRITABLE_ALLOWLIST],
                },
            })
        else:
            self._send(404, {"error": f"unknown: {self.path}"})


def create_server(host: str = "0.0.0.0", port: int = 44772) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), _Handler)


def _self_test() -> None:
    async def main() -> None:
        r = await exec_cmd(ExecRequest(cmd="echo hello && python3 --version"))
        print(json.dumps(r, ensure_ascii=False)[:300])

    asyncio.run(main())


if __name__ == "__main__":  # pragma: no cover
    if "--self-test" in sys.argv:
        _self_test()
    else:
        port = int(os.environ.get("SBX_PORT", 44772))
        srv = create_server(port=port)
        print(f"sandbox-exec listening on :{port}", flush=True)
        srv.serve_forever()
