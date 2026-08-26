# -*- coding: utf-8 -*-
"""沙箱 exec 服务（运行在沙箱 Pod 内，design §10.2 生产安全基线）。

- 提供 shell / python / write 三个执行端点
- 强制限制（§10.2）：
  - ``max_execution_seconds``：300s
  - ``max_stdout_bytes``：1MB
  - ``max_concurrent_processes``：10（信号量）
  - ``writable_allowlist``：仅 /workspace 与 /tmp 可写（write 端点校验）

启动：``uvicorn agentflow.sandbox.exec_service:app --port 44772``
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

MAX_EXECUTION_SECONDS = int(os.environ.get("SBX_MAX_EXEC_SECONDS", 300))
MAX_STDOUT_BYTES = int(os.environ.get("SBX_MAX_STDOUT_BYTES", 1_048_576))  # 1MB
MAX_CONCURRENT = int(os.environ.get("SBX_MAX_CONCURRENT", 10))
WRITABLE_ALLOWLIST = [Path(p) for p in os.environ.get(
    "SBX_WRITABLE", "/workspace:/tmp"
).split(":") if p]

app = FastAPI(title="agentflow-sandbox-exec", version="0.1.0")
_sem = asyncio.Semaphore(MAX_CONCURRENT)


class ExecRequest(BaseModel):
    cmd: str
    cwd: str | None = None
    timeout: int = MAX_EXECUTION_SECONDS


class PythonRequest(BaseModel):
    code: str
    timeout: int = MAX_EXECUTION_SECONDS


class WriteRequest(BaseModel):
    path: str
    content: str


def _truncate(out: bytes) -> str:
    return out[:MAX_STDOUT_BYTES].decode(errors="replace") + (
        f"\n...[截断，超过 {MAX_STDOUT_BYTES} 字节]" if len(out) > MAX_STDOUT_BYTES else ""
    )


async def _run(args: list[str], cwd: str | None, timeout: int, **kw) -> dict[str, Any]:
    async with _sem:
        try:
            proc = await asyncio.create_subprocess_exec(
                *args, cwd=cwd,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                **kw,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
                timed_out = False
            except asyncio.TimeoutError:
                proc.kill()
                stdout, stderr = await proc.communicate()
                timed_out = True
        except FileNotFoundError as exc:
            return {"rc": 127, "stdout": "", "stderr": str(exc), "timed_out": False}
    return {
        "rc": proc.returncode,
        "stdout": _truncate(stdout),
        "stderr": _truncate(stderr),
        "timed_out": timed_out,
    }


@app.post("/exec")
async def exec_cmd(req: ExecRequest) -> dict:
    if not req.cmd.strip():
        raise HTTPException(400, "cmd 为空")
    return await _run(["/bin/sh", "-c", req.cmd], req.cwd, min(req.timeout, MAX_EXECUTION_SECONDS))


@app.post("/python")
async def exec_python(req: PythonRequest) -> dict:
    if not req.code.strip():
        raise HTTPException(400, "code 为空")
    # 用独立子进程跑，隔离 + 可超时
    py = sys.executable
    return await _run([py, "-c", req.code], None, min(req.timeout, MAX_EXECUTION_SECONDS))


@app.post("/write")
async def write_file(req: WriteRequest) -> dict:
    p = Path(req.path)
    # §10.2 writable_allowlist：只允许 /workspace 与 /tmp
    resolved = p.resolve()
    allowed = any(
        str(resolved).startswith(str(base.resolve()))
        for base in WRITABLE_ALLOWLIST if base.exists()
    )
    if not allowed:
        raise HTTPException(403, f"路径不在可写白名单: {req.path}（仅 {WRITABLE_ALLOWLIST}）")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(req.content, encoding="utf-8")
    return {"written": True, "path": str(p), "bytes": len(req.content.encode("utf-8"))}


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "limits": {
            "max_execution_seconds": MAX_EXECUTION_SECONDS,
            "max_stdout_bytes": MAX_STDOUT_BYTES,
            "max_concurrent_processes": MAX_CONCURRENT,
            "writable_allowlist": [str(p) for p in WRITABLE_ALLOWLIST],
        },
    }


def _self_test() -> None:  # pragma: no cover - 本地自检
    async def main() -> None:
        r = await exec_cmd(ExecRequest(cmd="echo hello && python3 --version && java -version 2>&1"))
        print(json.dumps(r, indent=1)[:500])

    asyncio.run(main())


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    if "--self-test" in sys.argv:
        _self_test()
    else:
        uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("SBX_PORT", 44772)))
