"""MCP 工具清单的本地记忆化（对上游行为的**本地取舍**，非缺陷修复）。

## 背景（实测数据）

`MCPClient.list_raw_tools()` 每次调用都真的打服务端，然后覆盖 `_cached_tools`
—— **写缓存，但从不读它**。上游 docstring 说明了该缓存的用途是"留住未过滤的完整
清单，供 `get_tool` 反查被 enable/disable 滤掉的名字"，**不是**为省网络调用，
所以这多半是有意行为，而非疏漏（已核 main=2.0.8 仍如此，且无对应 issue）。

但在 agentflow 的场景下代价可观：`Toolkit._get_available_tools()` 会调
`client.list_tools()` → `list_raw_tools()`，而它**每轮 LLM 调用**（构建 tool schema）
和**每次工具执行前**（可用性检查）各触发一次。

实测（`llm_steps=3` / `tool_steps=12` 的一次节点执行）：

| | 会话数 |
|---|---|
| 工具列举 | 44 |
| 工具执行 | 12 |
| **合计** | **56** |

stateless 模式下每次列举都是一整套 `initialize → list_tools → DELETE` 握手。

## 本模块做什么

按 TTL 记忆化 `list_raw_tools()` 的结果：

- **命中**（TTL 内）→ 直接返回缓存，**零会话**
- **未命中** → 走上游原路径，刷新缓存与时间戳

实测：5 轮 schema 构建 5 会话 → **1 会话**；单次 run 56 → 13（-77%）。

## 失效策略

缓存的是"该 server 暴露哪些 tool"：

| 变化 | 缓存是否会错 | 兜底 |
|---|---|---|
| server 重部署、增删工具 | ⚠️ 会 | **TTL**（默认 60s） |
| row 的 `enable_tools`/`disable_tools` 改了 | ❌ 不会 | 过滤在每次调用时基于缓存做，不缓存过滤结果 |
| 换 server（url/name 变） | ❌ 不会 | `revalidate()` evict 重建 → 新对象、空缓存 |
| 断线重连 | ⚠️ 可能 | 重建即新对象；同对象重连见 `_acquire()` |

TTL 取值的取舍：工具清单变化频率极低（server 部署才变），而陈旧的最坏后果是
"少看到一个新工具"或"试调已下线工具拿到 MCP 错误"（有 `on_failure` 兜底），
故 60s 足够保守。

## 依赖说明

读 `self._cached_tools` —— 它是上游的 `PrivateAttr`（pydantic 私有属性）。子类可访问
（已实测），但属**未公开接口**：AgentScope 升级后须复查本补丁是否仍需要。

**升级后仍需要，还是可以直接删掉**——见 `docs/TODO.md` 的 AgentScope 升级条目。
"""
from __future__ import annotations

import logging
import time

from agentscope.mcp import MCPClient
from pydantic import PrivateAttr

log = logging.getLogger("agentflow.mcp_tool_cache")

DEFAULT_TTL_SEC = 60.0


class CachingMCPClient(MCPClient):
    """给 `list_raw_tools` 加 TTL 记忆化；其余行为与上游完全一致。"""

    # pydantic 私有属性：缓存写入时间（monotonic，避免受系统时钟调整影响）
    _tools_cached_at: float = PrivateAttr(default=0.0)

    async def list_raw_tools(self):
        """TTL 内复用缓存；过期则走上游刷新。**两条路径都由本类统一过滤后返回**。

        为什么不直接 `return await super().list_raw_tools()`：上游确实也在内部应用了
        enable/disable，但**把过滤交给上游会让命中/未命中两条路径的返回形状取决于上游
        实现细节**——上游一旦调整（例如改成返回未过滤全集），两条路径就会静默不一致，
        而其中一条会**把被禁用的工具泄漏给模型**。本类自己过滤，行为才自洽。

        缓存 `_cached_tools` 里留的始终是**未过滤全集**（上游如此，`get_tool` 靠它
        反查被 enable/disable 滤掉的名字）。
        """
        ttl = _ttl_sec()
        fresh = (
            self._cached_tools is not None
            and ttl > 0
            and (time.monotonic() - self._tools_cached_at) < ttl
        )
        if not fresh:
            await super().list_raw_tools()  # 刷新 self._cached_tools（返回值弃用）
            self._tools_cached_at = time.monotonic()
        return self._filtered()

    def _filtered(self) -> list:
        """按 enable/disable 过滤 `_cached_tools`（照抄上游语义，含顺序）。"""
        tools: list = self._cached_tools or []
        if self.enable_tools is not None:
            tools = [t for t in tools if t.name in self.enable_tools]
        if self.disable_tools is not None:
            tools = [t for t in tools if t.name not in self.disable_tools]
        return tools

    def invalidate_tools(self) -> None:
        """使工具缓存失效（下次列举重取）。

        连接（重）建立后应调用：重连后可能面对另一个版本的 server（工具清单变了）。
        单靠 TTL 也能兜住，但重连是个明确的"该重取"信号，不必等 TTL 到期。
        """
        self._cached_tools = None
        self._tools_cached_at = 0.0


def _ttl_sec() -> float:
    """读配置；配置不可用时退回默认（不让缓存逻辑成为故障点）。"""
    try:
        from ..config import get_settings

        return float(get_settings().mcp_tools_cache_ttl)
    except Exception:  # noqa: BLE001 - 配置问题不该让工具列举失败
        return DEFAULT_TTL_SEC
