"""SandboxClient：Worker → 沙箱 Pod exec 服务的 HTTP 客户端（design §4.1 gRPC 的本地形态）。

- ``run_shell`` / ``run_python`` / ``write_file``：对应沙箱 L2 工具（§7 Tool Registry）
- 超时 / 输出上限由 exec 服务强制（§10.2）；客户端仅透传
"""
from __future__ import annotations

import httpx
from pydantic import BaseModel

from ..errors import DataSourceError

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
        # trust_env=False：**沙箱是 loopback sidecar，不该走代理**。
        # httpx 默认 trust_env=False，会认 http_proxy/all_proxy，而且**不像 urllib 那样
        # 自动跳过 loopback** —— 于是同一条 URL 在 worker 侧经代理、在 `_env_preflight`
        # （走 urllib）侧直连，两边结论可以相反。实测（2026-09-22，本机有
        # http_proxy=127.0.0.1:7890）：经代理打一个不通的端口得到**代理的 502**
        # （响应体还是空的，见下面 write_file 的注释），直连得到 httpx.ReadError。
        # 判据：这个 URL 永远指向本机/同 Pod 的 exec 服务，代理只会把判据搞乱。
        self._client = httpx.AsyncClient(timeout=timeout, trust_env=False)

    async def run_shell(self, cmd: str, *, cwd: str | None = None, timeout: int = 300) -> SandboxResult:
        return await self._post(EXEC_PATH, {"cmd": cmd, "cwd": cwd, "timeout": timeout})

    async def run_python(self, code: str, *, timeout: int = 300) -> SandboxResult:
        return await self._post(PYTHON_PATH, {"code": code, "timeout": timeout})

    async def write_file(self, path: str, content: str) -> dict:
        resp = await self._client.post(f"{self.base_url}{WRITE_PATH}", json={"path": path, "content": content})
        if resp.status_code >= 400:
            raise DataSourceError(f"沙箱写文件失败 ({resp.status_code}): {resp.text[:200]}")
        body = resp.json()
        # ⚠️ exec 服务的**拒写也是 HTTP 200**（`{"written": false, "error": "路径不在可写白名单…"}`）。
        # 只看状态码会把"被拒"读成"成功"：`ws_write_file` 于是回一句「新建 X（沙箱）」，
        # 而工作区里什么都没有 —— 又一条**静默成功**，且它不依赖任何环境错配，
        # 只要工作区根不在 SBX_WRITABLE 里就会发生。判据必须单边：**只认 written 为 true**。
        if body.get("written") is not True:
            raise DataSourceError(f"沙箱拒绝写入 {path}: {body.get('error') or body}")
        return body

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
