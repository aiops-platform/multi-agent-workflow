"""MCPClientManager：把 MCPStore 里的 MCP server 配置解析为 AgentScope ``MCPClient``。

职责（对齐 SIP「MCP Server 配置」页 + AgentNodeRunner 运行时）：
- CRUD 后热刷新：``refresh_server(mid)`` 重建/连接（或 enabled=0 时 evict）。
- 运行时取 client：``clients_for_agent(agent_name)`` 返回该 agent 可用的 enabled 且已连接
  client。绑定以 **agent 主表**建模（``agent_configs.mcp_server_ids``），经注入的
  ``server_ids_for``（AgentConfigResolver）按 **server 粒度**过滤：v1.12.1 起「没配置绑定 =
  没有 server」（两态：无/精确子集），不再有「未配置=全量 enabled」的默认。
  stateful 失联做一次重连，失败跳过不阻塞执行。
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

    def __init__(self, store: Any, *, server_ids_for=None, stores_provider=None) -> None:
        # store：默认（全局/单租户回退）mcp store；stores_provider：async (tenant_id|None) → store
        # （v5.3 §7 P4：租户的 mcp_servers 表在租户自己的库里，按租户路由）
        self._store = store
        self._stores_provider = stores_provider
        # 缓存键 (tenant_key, mid)：tenant_key = tenant_id or ""（默认库）——per-tenant 隔离
        self._clients: dict[tuple[str, str], MCPClient] = {}
        self._rows: dict[tuple[str, str], dict[str, Any]] = {}
        self._loaded: set[str] = set()  # 已惰性加载的租户
        self._allow_cache: dict[int, list[str]] = {}
        # agent 主表绑定解析器（AgentConfigResolver.server_ids_for，server 粒度）。不注入（None）→
        # 返回全部 enabled（仅测试/独立用法兼容）；注入后以回调返回的子集过滤——空 set = 无 server。
        # 回调可为 async 且接受 (agent_name, tenant_id)（v5.3：按租户的 agent 绑定解析）。
        self.server_ids_for = server_ids_for

    async def _store_for(self, tenant_id: str | None) -> Any:
        if self._stores_provider is not None:
            return await self._stores_provider(tenant_id)
        return self._store

    @staticmethod
    def _key(tenant_id: str | None) -> str:
        return tenant_id or ""

    async def _ensure_loaded(self, tenant_id: str | None) -> None:
        """租户的 enabled servers 惰性加载（首次访问该租户时）。"""
        tkey = self._key(tenant_id)
        if tkey in self._loaded:
            return
        self._loaded.add(tkey)
        store = await self._store_for(tenant_id)
        rows = await store.list_enabled()
        for row in rows:
            await self._load_row(row, tenant_id)

    async def _load_row(self, row: dict, tenant_id: str | None) -> None:
        tkey = self._key(tenant_id)
        mid = row["id"]
        if (tkey, mid) in self._clients:
            return  # 防重复加载：同 key 旧 client 的 stateful 连接不能被第二个 task 接管
        try:
            client = self._build_client(row)
        except Exception as e:  # noqa: BLE001 —— 单条配置坏不拖垮启动
            log.warning("MCP[%s] 配置解析失败，跳过加载: %s", row.get("name"), e)
            return
        self._rows[(tkey, mid)] = row
        self._clients[(tkey, mid)] = client
        if client.is_stateful:
            try:
                await self._connect(client)
            except Exception as e:  # noqa: BLE001
                log.warning("MCP[%s] connect 失败（启动加载）: %s", client.name, e)

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
    async def load(self, tenant_id: str | None = None) -> None:
        """启动时加载 enabled 记录并建 client（tenant_id=None = 默认库）；stateful 做
        best-effort connect。连接失败仅 log 不阻塞 —— ``clients_for_agent`` 对失联
        client 还有一次重连机会。租户库的 server 由 ``_ensure_loaded`` 首访惰性加载。"""
        store = await self._store_for(tenant_id)
        rows = await store.list_enabled()
        for row in rows:
            await self._load_row(row, tenant_id)

    async def revalidate(self, tenant_id: str | None = None) -> bool:
        """与库对齐：淘汰「已删除 / 已禁用 / 配置已改」的 server，并允许下次重载新增的。

        与 :meth:`refresh_server`（本进程 CRUD 后按 mid 精确重建）不同，本方法供**外部
        进程**（如 Worker）用——它看不到 API 侧的内存状态，只能比对库中现状。

        为什么不能只清 ``_loaded``：``_load_row`` 对已缓存的 (tenant, mid) 会**早退**
        （防 stateful 连接被第二个 task 接管），所以光清 `_loaded` 只会补上新增项，
        删掉或改过的旧 client 会一直留着。必须先按 ``updated_at`` 差异 evict。

        :returns: 是否有条目被淘汰
        """
        tkey = self._key(tenant_id)
        store = await self._store_for(tenant_id)
        fresh = {r["id"]: r for r in await store.list_enabled()}

        changed = False
        for key in list(self._clients):
            if key[0] != tkey:
                continue
            mid = key[1]
            cached = self._rows.get((tkey, mid)) or {}
            row = fresh.get(mid)
            # 记录已删除 / 已禁用（不在 list_enabled）/ 配置变过（updated_at 变）→ 淘汰
            if row is None or row.get("updated_at") != cached.get("updated_at"):
                await self._evict(tkey, mid)
                changed = True
        # 允许下次 _ensure_loaded 重新扫库补齐新增项（已缓存的会早退，不重复建）
        self._loaded.discard(tkey)
        if changed:
            log.info("MCP 配置已变更，淘汰陈旧 client（tenant=%s）", tkey or "<default>")
        return changed

    async def refresh_server(self, mid: str, tenant_id: str | None = None) -> None:
        """CRUD 后重建该 server（租户维度）：先 evict 旧 client，再从库里当前记录重建。

        enabled=0 或记录已删除 → 只 evict 不重建。这样 PUT/DELETE 端点一个入口即可。"""
        tkey = self._key(tenant_id)
        await self._evict(tkey, mid)
        store = await self._store_for(tenant_id)
        row = await store.get(mid)
        if row is None or not row.get("enabled"):
            return
        try:
            client = self._build_client(row)
        except Exception as e:  # noqa: BLE001
            log.warning("MCP[%s] 配置重建失败: %s", row.get("name"), e)
            return
        self._rows[(tkey, mid)] = row
        self._clients[(tkey, mid)] = client
        if client.is_stateful:
            try:
                await self._connect(client)
            except Exception as e:  # noqa: BLE001
                log.warning("MCP[%s] connect 失败（refresh）: %s", client.name, e)

    async def _evict(self, tkey: str, mid: str) -> None:
        """移除一个 client 并关闭其 stateful 连接（杀 stdio 子进程）。"""
        client = self._clients.pop((tkey, mid), None)
        self._rows.pop((tkey, mid), None)
        if client is not None:
            self._allow_cache.pop(id(client), None)
            if client.is_stateful and client.is_connected:
                try:
                    await client.close()
                except Exception as e:  # noqa: BLE001
                    log.warning("MCP[%s] close 失败: %s", client.name, e)

    async def close_all(self) -> None:
        """关闭全部（shutdown 用，杀干净 stdio 子进程）。"""
        for key in list(self._clients):
            await self._evict(key[0], key[1])
        self._loaded.clear()

    def _resolve_server_ids(self, agent_name: str, tenant_id: str | None) -> set[str] | None:
        """server 绑定解析：回调支持 (agent) / (agent, tenant) 两种签名 + sync/async。"""
        if self.server_ids_for is None:
            return None
        import inspect

        try:
            res = self.server_ids_for(agent_name, tenant_id)
        except TypeError:
            res = self.server_ids_for(agent_name)
        if inspect.isawaitable(res):
            raise TypeError(
                "server_ids_for 返回了 awaitable——请用 async 绑定（经 _resolve_server_ids_async）"
            )
        return res or set()

    async def _resolve_server_ids_async(
        self, agent_name: str, tenant_id: str | None
    ) -> set[str] | None:
        if self.server_ids_for is None:
            return None
        import inspect

        try:
            res = self.server_ids_for(agent_name, tenant_id)
        except TypeError:
            res = self.server_ids_for(agent_name)
        if inspect.isawaitable(res):
            res = await res
        return res or set()

    # ------------------------------------------------------------------
    # 运行时查询（AgentNodeRunner 用）
    # ------------------------------------------------------------------
    async def clients_for_agent(
        self, agent_name: str, tenant_id: str | None = None
    ) -> list[MCPClient]:
        """返回该 agent 可用的 enabled 且（stateful）已连接的 client（server 粒度）。

        ``tenant_id`` 提供时在该租户自己的 mcp_servers 表（租户库）范围内解析（v5.3 §7）。
        ``server_ids_for`` 为 None（未注入 resolver，仅测试/独立用法）→ 返回全部 enabled；
        否则按 ``server_ids_for(agent_name, tenant_id)`` 过滤：空 set（未配置/明确不绑）
        → 无任何 client；非空 set → 只返回 ``mid in set`` 的 client。
        stateful 失联 → 尝试一次重连；重连仍失败则跳过（不让一个坏 server 拖垮整次 run）。
        """
        await self._ensure_loaded(tenant_id)
        tkey = self._key(tenant_id)
        allowed = await self._resolve_server_ids_async(agent_name, tenant_id)
        result: list[MCPClient] = []
        for key, row in list(self._rows.items()):
            if key[0] != tkey:
                continue  # 其他租户的 server 不可见（P4 物理隔离）
            mid = key[1]
            if not row.get("enabled"):
                continue
            if allowed is not None and mid not in allowed:
                continue
            client = self._clients.get(key)
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

    async def allow_names_for_agent(
        self, agent_name: str, tenant_id: str | None = None
    ) -> list[str]:
        """当前该 agent 可见 MCP 工具的 AgentScope 精确名（``mcp__{server}__{tool}``）。

        复用 ``clients_for_agent``（含 server_ids_for 过滤）→ 只对真正下发给该 agent 的
        client 生成 allow（``client.list_tools()`` 已应用 enable/disable 过滤）。结果按
        client 缓存；client 被 refresh 重建（id 变化）后自动重算。
        """
        names: list[str] = []
        for client in await self.clients_for_agent(agent_name, tenant_id):
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
