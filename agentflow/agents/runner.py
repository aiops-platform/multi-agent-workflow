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
from ..exec_context import current_tenant
from .mcp import build_toolkit
from .scopes import build_agent, build_permission_context, build_reasoning_model, run_agent
from .transcript import K_LLM_CALL, K_NODE, K_TOOL_CALL, TraceRecorder, scan_denied_blocks

log = logging.getLogger("agentflow.runner")

# trace-analyst 需 max_iters≥12（2 个工具 + 链合成），默认 6 会迭代耗尽返回 {}（CLAUDE.md §10）
# 逐 agent 迭代上限（默认 10）。修复侧需要「列目录 → 读文件 → 写文件 → 取 diff →
# 校验」多轮工具调用，10 轮会在写完前耗尽（实测 fix-implementer 超限返回 {}）；
# tester 要跑 gradle 并读结果，同样放宽。trace-analyst 见 CLAUDE.md §10。
_MAX_ITERS = {"trace-analyst": 12, "fix-implementer": 20, "tester": 16}
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
        agent_config=None,
        agent_config_provider=None,
        shared_datasources: bool = True,
        datasource=None,
        cmdb=None,
    ) -> None:
        self.model = UsageTrackingModel(model)
        self.use_mock_datasource = use_mock_datasource
        # 租户 CMDB（§9.4 TenantMappingProvider）：code-locator 的 locate_code 经它
        # 解析 service→repo（真实 repo_url，供诊断段与工作区准备共用同一映射）。
        self.cmdb = cmdb
        # 真实数据源适配器（agents/datasources.py:RealDataSourceAdapter）；非 None 时
        # 覆盖 mock，L1 工具（query_logs/get_trace/query_metrics/check_infra…）走 testbed。
        # 与 shared_datasources 配合：加固姿态下 L1 工具不构建，此参数随之失效。
        self.datasource = datasource
        self.mcp_manager = mcp_manager
        # AgentSpec DB 配置解析器（agent_config.AgentConfigResolver）：提供 system_prompt 覆盖 + enabled
        self.agent_config = agent_config
        # v5.3 §7：per-tenant 配置解析器提供者 async (tenant_id|None) → AgentConfigResolver
        #（租户库 agent_configs 覆盖行）；注入后按 current_tenant 路由，self.agent_config 作回退
        self.agent_config_provider = agent_config_provider
        # P1 加固姿态：False = 不注入内置共享数据源 L1 工具（数据工具一律租户 MCP 绑定）
        self.shared_datasources = shared_datasources
        # 兼容属性：最近一次节点用量（顺序/单节点场景精确；并行 wave 下以 take_usage 为准）
        self.last_usage: dict[str, float | int] | None = None
        # 按 id(node) 分槽（并行波安全）：executor 跑完取走（pop）→ retry/resume 只留末次成功
        self._trace_by_node: dict[int, list[dict]] = {}
        self._usage_by_node: dict[int, dict] = {}
        # Agent 级启用推理的节点 → thinking-enabled DeepSeek 模型（懒构建缓存，见 _reasoning）
        self._reasoning_model: UsageTrackingModel | None = None

    def _reasoning(self) -> UsageTrackingModel:
        """懒构建并缓存推理模型（thinking_enable=True）。无 API Key → ScriptedJsonModel 回退（封闭）。"""
        if self._reasoning_model is None:
            self._reasoning_model = UsageTrackingModel(build_reasoning_model())
        return self._reasoning_model

    def take_trace(self, node: Node) -> list[dict] | None:
        """pop 取走该节点的明细行（无 → None）。executor 在节点成功后调用。"""
        return self._trace_by_node.pop(id(node), None)

    def take_usage(self, node: Node) -> dict | None:
        """pop 取走该节点的 {tokens,cost}。防并行 agent 波串扰 + 幂等节点无 key 返回 None。"""
        return self._usage_by_node.pop(id(node), None)

    async def __call__(self, node: Node, params: dict) -> Any:
        key = id(node)
        self._trace_by_node.pop(key, None)
        self._usage_by_node.pop(key, None)
        self.last_usage = None
        agent = node.agent
        if not agent:
            return {"node": node.id, "ok": True}
        # AgentSpec DB 配置（覆盖 system_prompt / enabled / reasoning / MCP server 绑定）。
        # per-tenant（v5.3 §7）：provider 按 current_tenant 取该租户库的覆盖行；
        # 未接 provider/resolver 或名字不在配置 → 走内置静态默认，行为不变。
        if self.agent_config_provider is not None:
            resolver = await self.agent_config_provider(current_tenant.get())
            cfg = resolver.resolve(agent) if resolver is not None else None
        else:
            cfg = self.agent_config.resolve(agent) if self.agent_config is not None else None
        if cfg is not None and not cfg.enabled:
            return {"node": node.id, "ok": True, "disabled": True, "note": f"agent {agent!r} 已在配置中停用"}
        # Agent 级启用推理（cfg.reasoning_enabled）→ thinking-enabled 模型，CoT 落 llm_call 明细；
        # 否则用全局正常模型。选完再 reset（不 reset 未用的那只）。
        model = self._reasoning() if (cfg is not None and cfg.reasoning_enabled) else self.model
        model.reset()
        # MCP（hybrid toolkit）：取出该 agent 可用的 client——DB 配置选定 server（两态：无/子集，
        # v1.12.1 起未绑定=没有 server；resolver 注入时空集→无 client，不注入的独立用法才回退全量）
        # + 预计算 allow 名单。allow 规则（§9.5 DONT_ASK + 精确工具名）必须早于 build_agent / 首个工具调用。
        clients, allow_extra = [], None
        tenant_id = current_tenant.get()
        if self.mcp_manager is not None:
            clients = await self.mcp_manager.clients_for_agent(agent, tenant_id=tenant_id)
            allow_extra = await self.mcp_manager.allow_names_for_agent(agent, tenant_id=tenant_id)
        toolkit = build_toolkit(
            agent,
            use_mock=self.use_mock_datasource,
            shared_datasources=self.shared_datasources,
            mcp_clients=clients,
            datasource=self.datasource,
            cmdb=self.cmdb,
        )
        ctx = build_permission_context(agent, allow_extra=allow_extra)
        # 每节点独立 recorder：采集 llm_call / tool_call 明细；DENY 工具跑后补扫
        recorder = TraceRecorder(node.id, agent)
        a = build_agent(
            agent,
            toolkit,
            model,
            permission_context=ctx,
            max_iters=_MAX_ITERS.get(agent, _DEFAULT_MAX_ITERS),
            # DB 配置解析后的有效 system_prompt（含静态回退）；cfg=None → 传 None 走 scopes 静态默认
            system_prompt=cfg.system_prompt if cfg is not None else None,
            middlewares=[recorder],
        )
        user = params if isinstance(params, dict) and params else {"params": params}
        out = await run_agent(a, user)
        tokens = model.input_tokens + model.output_tokens
        cost = (
            model.input_tokens * _PRICE_INPUT_PER_M
            + model.output_tokens * _PRICE_OUTPUT_PER_M
        ) / 1_000_000
        usage = {"tokens": tokens, "cost": round(cost, 6)}
        self.last_usage = usage
        # 明细行 = [node 汇总] + llm/tool + 跑后补扫的 denied；executor 取走落 node_traces
        node_row = {
            "agent": agent,
            "enabled": cfg.enabled if cfg is not None else True,
            "input": user,
            "output": out,
            "tokens": tokens,
            "cost": round(cost, 6),
            "llm_steps": sum(1 for r in recorder.rows if r["kind"] == K_LLM_CALL),
            "tool_steps": sum(1 for r in recorder.rows if r["kind"] == K_TOOL_CALL),
        }
        # 跑后补扫 DENY 工具调用；agent 无 .state.context（轻量桩/测试替换）时退回空
        denied_ctx = getattr(getattr(a, "state", None), "context", None)
        self._trace_by_node[key] = (
            [{"kind": K_NODE, "name": None, "payload": node_row}]
            + recorder.rows
            + scan_denied_blocks(denied_ctx)
        )
        self._usage_by_node[key] = usage
        log.info(
            "agent[%s] -> %s (tokens=%s, cost=$%.6f)",
            agent,
            (str(out)[:120] if out else "{}"),
            tokens,
            cost,
        )
        return out
