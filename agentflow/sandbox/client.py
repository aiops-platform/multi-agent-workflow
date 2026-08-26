# -*- coding: utf-8 -*-
"""SandboxClient：Worker → 沙箱 Pod exec 服务的 HTTP 客户端（design §4.1 gRPC 的本地形态）。

- ``run_shell`` / ``run_python`` / ``write_file``：对应沙箱 L2 工具（§7 Tool Registry）
- 超时 / 输出上限由 exec 服务强制（§10.2）；客户端仅透传
"""
from __future__ import annotations

import httpx
from pydantic import BaseModel

from ..agents.datasources import DataSourceError

EXEC_PATH = "/exec"
PYTHON_PATH = "/python"
WRITE_PATH = "/write"


class SandboxResult(BaseModel):
    rc: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False


class SandboxClient:
    def __init__(self, base_url: str, *, timeout: float = 320.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=timeout)

    async def run_shell(self, cmd: str, *, cwd: str | None = None, timeout: int = 300) -> SandboxResult:
        return await self._post(EXEC_PATH, {"cmd": cmd, "cwd": cwd, "timeout": timeout})

    async def run_python(self, code: str, *, timeout: int = 300) -> SandboxResult:
        return await self._post(PYTHON_PATH, {"code": code, "timeout": timeout})

    async def write_file(self, path: str, content: str) -> dict:
        resp = await self._client.post(f"{self.base_url}{WRITE_PATH}", json={"path": path, "content": content})
        if resp.status_code >= 400:
            raise DataSourceError(f"沙箱写文件失败 ({resp.status_code}): {resp.text[:200]}")
        return resp.json()

    async def health(self) -> dict:
        resp = await self._client.get(f"{self.base_url}/health")
        resp.raise_for_status()
        return resp.json()

    async def _post(self, path: str, body: dict) -> SandboxResult:
        resp = await self._client.post(f"{self.base_url}{path}", json=body)
        if resp.status_code >= 400:
            raise DataSourceError(f"沙箱 {path} 失败 ({resp.status_code}): {resp.text[:200]}")
        return SandboxResult(**resp.json())

    async def aclose(self) -> None:
        await self._client.aclose()
