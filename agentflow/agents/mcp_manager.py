# -*- coding: utf-8 -*-
"""MCPClientManager：把 MCPStore 里的 MCP server 配置解析为 AgentScope ``MCPClient``。

职责（对齐 SIP「MCP Server 配置」页 + AgentNodeRunner 运行时）：
- CRUD 后热刷新：``refresh_server(mid)`` 重建/连接（或 enabled=0 时 evict）。
- 运行时取 client：``clients_for_agent(agent_name)`` 返回全部 enabled 且已连接的 client
  （**server 侧无 agent 绑定**——原 ``agents`` 字段已移除；运行时把全部 enabled server
  下发给 agent，待 agent 主表落地后再按 agent 所选 server 过滤。stateful 失联做一次重连，
  失败跳过不阻塞执行）。
- 预计算 allow 名单：``allow_names_for_agent(agent_name)`` 经 ``MCPTool.name`` 取 AgentScope
  侧的精确工具名（``mcp__{server}__{sanitized}``，避免 sanitize 规则漂移），供
  ``build_permission_context`` 在 build_agent 前注入（§9.5 DONT_ASK + 精确 allow）。
- ``test_connection(row)``：临时建 client（不落库）连一次并列出工具，供「测试连接」按钮。

约定/坑：
- 构造函数不做 DB/I/O —— 便于测试 import 后直接 monkeypatch store（ASGITransport 不触发 lifespan）。
- ``Toolkit.__init__`` 对「stateful 但未 connect」的 client 抛 ValueError（agentscope _toolkit.py）
  → 本模块 load/refresh 时 best-effort connect，失败仅 log 并保留未连接状态；hybrid
  ``build_toolkit`` 侧再做一层防御过滤。
- 并发 run 共享同一缓存 stateful session → 只在 CRUD 后刷新，不在 run 中途 refresh。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from agentscope.mcp import HttpMCPConfig, MCPClient, StdioMCPConfig

log = logging.getLogger("agentflow.mcp_manager")

# 一次「测试连接」的整体超时（秒）。mcp/anyio 在 wait_for 超时取消时可能打噪音日志，可接受。
_TEST_TIMEOUT = 10.0

# stdio/http 连接字段：只挑 UI 配置里配置模型认识的键，避免把杂项字段丢给 pydantic。
_STDIO_KEYS = ("command", "args", "env", "cwd")
_HTTP_KEYS = ("url", "headers", "timeout")


def _exc_message(exc: BaseException) -> str:
    """把异常压成一行可读信息。

    mcp/anyio 常把底层连接错误（连接拒绝/超时）包成 ``ExceptionGroup``，直接 ``str()`` 只会得到
    ``unhandled errors in a TaskGroup (1 sub-exception)`` 这类不指向根因的话；递归取首个子异常
    才能露出真实原因（如 ``[Errno 61] Connection refused``）。单元素 ExceptionGroup 取首个即可。
    """
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    text = str(exc).strip()
    return text or type(exc).__name__


class MCPClientManager:
    """MCP server 配置 → AgentScope MCPClient 的缓存管理器（无 per-server agent 绑定）。

    ``_clients: dict[mid, MCPClient]`` / ``_rows: dict[mid, dict]`` 仅缓存 enabled 记录；
    ``_allow_cache: dict[id(client), list[str]]`` 存每 client 的 LLM 侧工具名（client 被
    refresh 重建后 id 变化 → 自然失效重算）。
    """

    def __init__(self, store: Any) -> None:
        self._store = store
        self._clients: dict[str, MCPClient] = {}
        self._rows: dict[str, dict[str, Any]] = {}
        self._allow_cache: dict[int, list[str]] = {}

    # ------------------------------------------------------------------
    # client 构造
    # ------------------------------------------------------------------
    @staticmethod
    def _build_client(row: dict[str, Any]) -> MCPClient:
        """把一条存储 row 解析为 ``agentscope.mcp.MCPClient``。

        stdio 强制 stateful（AgentScope 硬性约束）；http 按 ``is_stateful``。
        配置缺失/非法时抛 pydantic 异常，由调用方兜底（API 400 / test_connection error）。
        """
        transport = row["transport"]
        cfg = row.get("config") or {}
        if transport == "stdio":
            mcp_config = StdioMCPConfig(
                command=cfg["command"],
                args=cfg.get("args"),
                env=cfg.get("env"),
                cwd=cfg.get("cwd"),
            )
            stateful = True
        elif transport == "http":
            mcp_config = HttpMCPConfig(
                url=cfg["url"],
                headers=cfg.get("headers"),
                timeout=cfg.get("timeout"),
            )
            stateful = bool(row.get("is_stateful"))
        else:
            raise ValueError(f"不支持的 transport: {transport!r}")
        return MCPClient(
            name=row["name"],
            is_stateful=stateful,
            mcp_config=mcp_config,
            enable_tools=row.get("enable_tools"),
            disable_tools=row.get("disable_tools"),
        )

    @staticmethod
    async def _connect(client: MCPClient) -> None:
        """stateful client 若未连接则连接（stateless 是 no-op）。"""
        if client.is_stateful and not client.is_connected:
            await client.connect()

    async def _describe_tools(self, client: MCPClient) -> list[dict[str, Any]]:
        """列出 client 的可用工具：原始名 + 描述 + 只读标注 + AgentScope LLM 侧精确名。"""
        out: list[dict[str, Any]] = []
        for raw in await client.list_raw_tools():
            wrapped = await client.get_tool(raw.name)  # 命中 client 已缓存 raw tools，不再往返
            out.append(
                {
                    "name": raw.name,
                    "description": raw.description or "",
                    "read_only": bool(wrapped.is_read_only),
                    "llm_name": wrapped.name,
                },
            )
        return out

    # ------------------------------------------------------------------
    # 生命周期：load / refresh / evict / close_all
    # ------------------------------------------------------------------
    async def load(self) -> None:
        """启动时加载 enabled 记录并建 client；stateful 做 best-effort connect。

        连接失败仅 log 并保留（未连接）状态，不阻塞启动 —— 之后 ``clients_for_agent``
        对失联 client 还有一次重连机会。
        """
        rows = await self._store.list_enabled()
        for row in rows:
            mid = row["id"]
            try:
                client = self._build_client(row)
            except Exception as e:  # noqa: BLE001 —— 单条配置坏不拖垮启动
                log.warning("MCP[%s] 配置解析失败，跳过加载: %s", row.get("name"), e)
                continue
            self._rows[mid] = row
            self._clients[mid] = client
            if client.is_stateful:
                try:
                    await self._connect(client)
                except Exception as e:  # noqa: BLE001
                    log.warning("MCP[%s] connect 失败（启动加载）: %s", client.name, e)

    async def refresh_server(self, mid: str) -> None:
        """CRUD 后重建该 server：先 evict 旧 client，再从库里当前记录重建。

        enabled=0 或记录已删除 → 只 evict 不重建。这样 PUT/DELETE 端点一个入口即可。
        """
        await self._evict(mid)
        row = await self._store.get(mid)
        if row is None or not row.get("enabled"):
            return
        try:
            client = self._build_client(row)
        except Exception as e:  # noqa: BLE001
            log.warning("MCP[%s] 配置重建失败: %s", row.get("name"), e)
            return
        self._rows[mid] = row
        self._clients[mid] = client
        if client.is_stateful:
            try:
                await self._connect(client)
            except Exception as e:  # noqa: BLE001
                log.warning("MCP[%s] connect 失败（refresh）: %s", client.name, e)

    async def _evict(self, mid: str) -> None:
        """移除一个 client 并关闭其 stateful 连接（杀 stdio 子进程）。"""
        client = self._clients.pop(mid, None)
        self._rows.pop(mid, None)
        if client is not None:
            self._allow_cache.pop(id(client), None)
            if client.is_stateful and client.is_connected:
                try:
                    await client.close()
                except Exception as e:  # noqa: BLE001
                    log.warning("MCP[%s] close 失败: %s", client.name, e)

    async def close_all(self) -> None:
        """关闭全部（shutdown 用，杀干净 stdio 子进程）。"""
        for mid in list(self._clients):
            await self._evict(mid)

    # ------------------------------------------------------------------
    # 运行时查询（AgentNodeRunner 用）
    # ------------------------------------------------------------------
    async def clients_for_agent(self, agent_name: str) -> list[MCPClient]:
        """返回全部 enabled 且（stateful）已连接的 client。

        参数 ``agent_name`` 暂不参与过滤：server 侧绑定（原 ``agents`` 字段）已移除，
        运行时把全部 enabled server 下发给每个 agent；后续以 agent 为主表挂载 MCP server
        id 时，再回到这里按 agent 过滤。stateful 失联 → 尝试一次重连；重连仍失败则跳过
        （log 告警，不让一个坏 server 拖垮整次 run）。
        """
        result: list[MCPClient] = []
        for mid, row in list(self._rows.items()):
            if not row.get("enabled"):
                continue
            client = self._clients.get(mid)
            if client is None:
                continue
            if client.is_stateful and not client.is_connected:
                try:
                    await self._connect(client)
                    log.info("MCP[%s] 失联后重连成功", client.name)
                except Exception as e:  # noqa: BLE001
                    log.warning("MCP[%s] 失联且重连失败，本次 run 跳过: %s", client.name, e)
                    continue
            result.append(client)
        return result

    async def allow_names_for_agent(self, agent_name: str) -> list[str]:
        """当前该 agent 可见 MCP 工具的 AgentScope 精确名（``mcp__{server}__{tool}``）。

        server 侧绑定已移除 → 等于全部 enabled client 的工具（agent 参数同
        ``clients_for_agent`` 暂不参与过滤）。只对真正下发的工具生成 allow
        （``client.list_tools()`` 已应用 enable/disable 过滤）。结果按 client 缓存；
        client 被 refresh 重建（id 变化）后自动重算。
        """
        names: list[str] = []
        for client in await self.clients_for_agent(agent_name):
            cached = self._allow_cache.get(id(client))
            if cached is not None:
                names.extend(cached)
                continue
            try:
                tools = await client.list_tools()
            except Exception as e:  # noqa: BLE001
                log.warning("MCP[%s] list_tools 失败，跳过 allow 生成: %s", client.name, e)
                continue
            tool_names = [t.name for t in tools]
            self._allow_cache[id(client)] = tool_names
            names.extend(tool_names)
        return names

    # ------------------------------------------------------------------
    # 测试连接（不落库）
    # ------------------------------------------------------------------
    async def test_connection(
        self,
        row: dict[str, Any],
        *,
        timeout: float = _TEST_TIMEOUT,
    ) -> dict[str, Any]:
        """临时建 client 连一次并列出工具，返回 ``{ok, transport, tools, error?}``。

        失败不抛异常：连不上/超时/配置坏都收敛为 ``{ok: False, error: ...}``。
        ``timeout`` 覆盖单次探测整体超时（create/update 自动拉工具快照时用短值避免拖慢保存）。

        为什么 probe 要放进独立 Task 并吞 BaseException：mcp 的 streamable http client 在
        底层请求出错时（如服务端返回 401/404），会经 anyio cancel scope 把 ``HTTPStatusError``
        转成 ``CancelledError``（BaseException，``except Exception`` 抓不住）甚至抛跨任务
        cancel scope 的 RuntimeError。若任其冒泡 → 测试连接直接 500。因此 probe 内 ``except
        BaseException`` 全量收敛为结果 dict；外层只在真正超时才 ``probe.cancel()``（取消落在
        probe 自己的上下文里，清理不跨任务）。真正的外层取消（客户端断开）则原样 ``raise``。
        """
        transport = row.get("transport", "http")
        try:
            client = self._build_client(row)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "transport": transport, "tools": [], "error": f"配置解析失败：{e}"}

        async def _probe() -> dict[str, Any]:
            # BaseException 全收：mcp/anyio 在 401/404/中断时抛 CancelledError 也收敛为错误
            try:
                await self._connect(client)
                tools = await self._describe_tools(client)
                return {"ok": True, "tools": tools}
            except asyncio.CancelledError:
                # 底层请求经 cancel scope 取消（典型：目标可达但鉴权失败/非 MCP 端点）
                return {"ok": False, "tools": [], "error": "连接被中断：目标服务可达但鉴权失败或非 MCP 端点（服务端返回 HTTP 错误）"}
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "tools": [], "error": _exc_message(e)}
            finally:
                if client.is_stateful and client.is_connected:
                    try:
                        await client.close()
                    except Exception:  # noqa: BLE001, S110 —— 尽力清理，失败可忽略
                        pass

        probe_task = asyncio.create_task(_probe())
        try:
            result = await asyncio.wait_for(asyncio.shield(probe_task), timeout=timeout)
            result["transport"] = transport
            return result
        except TimeoutError:
            # probe 由 shield 保护仍在跑；取消它并等它在自己上下文里收尾（清理不跨任务）。
            probe_task.cancel()
            try:
                await probe_task  # probe 已吞 BaseException，正常返回 dict
            except BaseException:  # noqa: BLE001, S110 —— 保险：绝不让清理异常掩盖超时结果
                pass
            return {"ok": False, "transport": transport, "tools": [], "error": f"连接超时（{int(timeout)} 秒）"}
        except asyncio.CancelledError:
            # 外层被真正取消（客户端断开 / 服务关停）：停掉 probe 后继续向上抛
            probe_task.cancel()
            try:
                await probe_task
            except BaseException:  # noqa: BLE001, S110 —— 清理失败可忽略
                pass
            raise
