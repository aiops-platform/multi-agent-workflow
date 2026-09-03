"""控制面 API 的 node_runner：按 ``node.agent`` 分发到真实 DeepSeek agent。

参考 ``agentflow/agentflow``（agentflow-ui 原后端）的执行接缝：它给 DAGExecutor 注入
``AgentRuntime``（``OpenCodeAdapter`` → opencode serve，每节点一个 session），永远真实执行、
无 mock 模式。本模块是 multi-agent-workflow 的等价物——但我们走自己的 ``node_runner`` 接缝
（``dag_executor.py:46``），真实执行路径用 AgentScope/DeepSeek（``build_agent``/``run_agent``）。

启用：``app.py init()`` 检测到 ``DEEPSEEK_API_KEY`` 时注入 ``AgentNodeRunner``；
无 Key 时保持默认 mock（``_default_runner``），Bug Solve 页全绿、token/cost 诚实为 0。
数据源默认 mock（``build_toolkit(use_mock=True)``，无需 testbed 端口转发）。
"""
from __future__ import annotations

import logging
from typing import Any

from agentscope.model import ChatModelBase, ChatResponse

from ..core.dag import Node
from .mcp import build_toolkit
from .scopes import build_agent, build_permission_context, run_agent

log = logging.getLogger("agentflow.runner")

# trace-analyst 需 max_iters≥12（2 个工具 + 链合成），默认 6 会迭代耗尽返回 {}（CLAUDE.md §10）
_MAX_ITERS = {"trace-analyst": 12}
_DEFAULT_MAX_ITERS = 10

# DeepSeek 计费（美元 / 百万 token）。deepseek-v4-flash 未公开单独费率，
# 取 DeepSeek 标准费率（input $0.07/M、output $0.28/M）；如需按实际账单调整改这里即可。
_PRICE_INPUT_PER_M = 0.07
_PRICE_OUTPUT_PER_M = 0.28


class UsageTrackingModel(ChatModelBase):
    """包装真实模型，拦截 ``__call__`` / ``generate_structured_output`` 累加 ``ChatUsage``。

    AgentScope 的 token 计量在响应里（``ChatResponse.usage`` / ``StructuredResponse.usage``），
    但模型调用发生在 Agent 内部（react 循环）。本代理记录累计的 input/output token，
    供 ``AgentNodeRunner`` 每节点读出并写入节点 checkpoint —— 这样聚合 ``GET /runs/{id}``
    就能展示真实 LLM 计量。其余属性/方法（``count_tokens``、``context_size`` 等）委托给内层模型。
    """

    def __init__(self, model: ChatModelBase) -> None:
        # 不调用 super().__init__：仅需保留内层模型引用 + 计数器
        self._model = model
        self.input_tokens = 0
        self.output_tokens = 0

    def reset(self) -> None:
        self.input_tokens = 0
        self.output_tokens = 0

    def _acc(self, usage) -> None:
        if usage is None:
            return
        self.input_tokens += getattr(usage, "input_tokens", 0) or 0
        self.output_tokens += getattr(usage, "output_tokens", 0) or 0

    async def _wrap_call(self, coro):
        res = await coro
        if isinstance(res, ChatResponse):
            self._acc(res.usage)
            return res
        if hasattr(res, "__aiter__"):
            # streaming：AsyncGenerator[ChatResponse]，逐 chunk 累加（usage 通常在末 chunk）
            async def _gen():
                async for chunk in res:
                    self._acc(chunk.usage)
                    yield chunk

            return _gen()
        return res

    async def __call__(self, messages, tools=None, tool_choice=None, **kwargs):
        return await self._wrap_call(
            self._model(messages, tools=tools, tool_choice=tool_choice, **kwargs)
        )

    async def generate_structured_output(self, messages, structured_model, **kwargs):
        res = await self._model.generate_structured_output(messages, structured_model, **kwargs)
        self._acc(res.usage)
        return res

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):  # 防 __getattr__ 递归
            raise AttributeError(name)
        return getattr(self._model, name)


class AgentNodeRunner:
    """通用 agent 分发 runner：每个执行节点按 ``node.agent`` 建 agent 并真实调用 LLM。

    ``node.agent`` 为空（审批节点实际不会走到 runner）时返回结构化占位，
    避免 DAGExecutor 收到 ``None`` 输出。

    计量：内部模型包一层 ``UsageTrackingModel``，每节点跑完把累计 token/cost 暴露到
    ``last_usage``，DAGExecutor 写入节点 checkpoint → 聚合 GET 返回真实值。
    mock 模型（ScriptedJsonModel）无 usage → ``last_usage`` 仍为 0/0.0（诚实不伪造）。
    """

    def __init__(
        self,
        model,
        *,
        use_mock_datasource: bool = True,
        mcp_manager=None,
    ) -> None:
        self.model = UsageTrackingModel(model)
        self.use_mock_datasource = use_mock_datasource
        self.mcp_manager = mcp_manager
        self.last_usage: dict[str, float | int] | None = None

    async def __call__(self, node: Node, params: dict) -> Any:
        self.model.reset()
        self.last_usage = None
        agent = node.agent
        if not agent:
            return {"node": node.id, "ok": True}
        # MCP（hybrid toolkit）：取出全部 enabled client + 预计算 allow 名单。
        # server 侧无 agent 绑定（原 agents 字段已移除）→ 每个 agent 都拿到全部 enabled server；
        # allow 规则（§9.5 DONT_ASK + 精确工具名）必须早于 build_agent / 首个工具调用。
        clients, allow_extra = [], None
        if self.mcp_manager is not None:
            clients = await self.mcp_manager.clients_for_agent(agent)
            allow_extra = await self.mcp_manager.allow_names_for_agent(agent)
        toolkit = build_toolkit(
            agent,
            use_mock=self.use_mock_datasource,
            mcp_clients=clients,
        )
        ctx = build_permission_context(agent, allow_extra=allow_extra)
        a = build_agent(
            agent,
            toolkit,
            self.model,
            permission_context=ctx,
            max_iters=_MAX_ITERS.get(agent, _DEFAULT_MAX_ITERS),
        )
        user = params if isinstance(params, dict) and params else {"params": params}
        out = await run_agent(a, user)
        tokens = self.model.input_tokens + self.model.output_tokens
        cost = (
            self.model.input_tokens * _PRICE_INPUT_PER_M
            + self.model.output_tokens * _PRICE_OUTPUT_PER_M
        ) / 1_000_000
        self.last_usage = {"tokens": tokens, "cost": round(cost, 6)}
        log.info(
            "agent[%s] -> %s (tokens=%s, cost=$%.6f)",
            agent,
            (str(out)[:120] if out else "{}"),
            tokens,
            cost,
        )
        return out
