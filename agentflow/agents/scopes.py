"""AgentScope 2.0.3 适配层（design §5 锁定版本）。

复用 S-011 实测通过的构建模式（`Agent` + `reply` + `Toolkit`，prompt 要求只输出
严格 JSON，§7 输出契约）。模型按 design §16.3 默认 DeepSeek `deepseek-v4-flash`。
"""
from __future__ import annotations

import json
import re
from collections.abc import AsyncGenerator

from agentscope.agent import Agent, ReActConfig
from agentscope.message import TextBlock, ToolCallBlock, UserMsg
from agentscope.model import ChatModelBase
from agentscope.permission import (
    PermissionBehavior,
    PermissionContext,
    PermissionMode,
    PermissionRule,
)
from agentscope.state import AgentState

from ..config import Settings, get_settings


def build_permission_context(
    agent_name: str,
    *,
    mode: PermissionMode = PermissionMode.DONT_ASK,
    tenant_id: str = "local",
    allow_extra: list[str] | None = None,
) -> PermissionContext:
    """租户级工具权限上下文（design §9.5：DONT_ASK + allow 规则 = 无人值守 + 安全）。

    把 agent 在 Tool Registry 中注册的 L1 工具加入 allow 规则（白名单免确认执行）；
    未授权工具在 DONT_ASK 下由 PermissionEngine 一律 DENY 且不执行。叠加租户
    deny 规则（M5 接入 tenant 配置）后取交集。
    """
    from .tools import tools_for_agent

    ctx = PermissionContext(mode=mode)
    tool_names = [spec.name for spec in tools_for_agent(agent_name)] + (allow_extra or [])
    for tool in tool_names:
        ctx.allow_rules.setdefault(tool, []).append(
            PermissionRule(
                tool_name=tool, rule_content=None,
                behavior=PermissionBehavior.ALLOW, source=f"tenant/{tenant_id}",
            )
        )
    return ctx


def build_model(settings: Settings | None = None) -> ChatModelBase:
    """构建 DeepSeek Chat 模型（AgentScope provider，design §16.3）。

    未配置 API Key 时回退 ScriptedJsonModel（确定性输出，供无 Key / CI 场景）。
    """
    settings = settings or get_settings()
    if not settings.deepseek_api_key:
        return ScriptedJsonModel({"note": "no_api_key"})

    from agentscope.credential import OpenAICredential
    from agentscope.model import OpenAIChatModel

    return OpenAIChatModel(
        credential=OpenAICredential(
            api_key=settings.deepseek_api_key, base_url=settings.deepseek_base_url
        ),
        model=settings.deepseek_model,
        stream=True,
    )


def build_reasoning_model(settings: Settings | None = None) -> ChatModelBase:
    """构建 DeepSeek **推理**模型（thinking 模式，design §16.3 同源）。

    与 ``build_model``（OpenAIChatModel）的关键差异：用 ``DeepSeekChatModel`` 且
    ``Parameters(thinking_enable=True)``——它才会发送 DeepSeek 的 ``extra_body.thinking.type=
    "enabled"``，返回的 ``reasoning_content`` 经 AgentScope 解析成 ``ThinkingBlock``（思维链）。
    供 Agent 级启用推理的节点用；无 API Key 时回退 ScriptedJsonModel（封闭，供无 Key / CI）。
    """
    settings = settings or get_settings()
    if not settings.deepseek_api_key:
        return ScriptedJsonModel({"note": "no_api_key"})

    from agentscope.credential import DeepSeekCredential
    from agentscope.model import DeepSeekChatModel

    return DeepSeekChatModel(
        credential=DeepSeekCredential(
            api_key=settings.deepseek_api_key, base_url=settings.deepseek_base_url
        ),
        model=settings.deepseek_model,
        parameters=DeepSeekChatModel.Parameters(thinking_enable=True),
        stream=True,
    )


def build_agent(
    name: str,
    toolkit,
    model: ChatModelBase,
    *,
    permission_context: PermissionContext | None = None,
    tenant_id: str = "local",
    max_iters: int = 10,
    system_prompt: str | None = None,
    middlewares: list | None = None,
) -> Agent:
    from .prompts import SYSTEM_PROMPTS

    ctx = permission_context or build_permission_context(name, tenant_id=tenant_id)
    return Agent(
        name=name,
        # system_prompt 显式传入（DB 配置覆盖/自定义 agent）优先，否则用内置静态默认
        system_prompt=system_prompt or SYSTEM_PROMPTS.get(name, "你是 AI 运维平台智能体。"),
        model=model,
        toolkit=toolkit,
        middlewares=middlewares or [],
        state=AgentState(permission_context=ctx),
        react_config=ReActConfig(max_iters=max_iters),
    )


class AgentOutputError(RuntimeError):
    """agent 回复文本里不含可解析的 JSON（§7 输出契约未满足）。

    典型成因：ReAct 轮次耗尽（AgentScope 在 max_iters 用尽时 yield 一段固定说明文本
    而非 JSON）、模型只调工具未收尾、回复被截断。

    此前 ``extract_json`` 静默返回 ``{}``，使「做了但没汇报」与「完全没做」不可区分：
    实测 committer 已成功 add+commit，却因轮次耗尽返回 ``{}``，run 照常显示 success，
    副作用是否真的发生无从判断（design-v5.4 §3.1 标注的缺口）。
    """

    def __init__(self, agent_name: str, text: str) -> None:
        preview = " ".join((text or "").split())[:200]
        super().__init__(
            f"agent {agent_name} 未输出合法 JSON（§7 输出契约未满足）: {preview!r}"
        )
        self.agent_name = agent_name
        self.text = text


async def run_agent(agent: Agent, user_input: dict | str) -> dict:
    """喂入输入并解析出严格 JSON（§7 输出契约）。

    解析失败抛 :class:`AgentOutputError`（**不再静默返回 ``{}``**），交由 executor 的
    ``retry`` / ``on_failure`` 策略定夺（§8.1）：诊断侧 ``on_failure: continue`` 会退化为
    负证据（``{"found": false, ...}``），修复侧 ``abort`` 则让节点显式失败。
    """
    content = user_input if isinstance(user_input, str) else json.dumps(user_input, ensure_ascii=False)
    final = await agent.reply(UserMsg(name="user", content=content))
    text = "".join(b.text for b in final.get_content_blocks("text") if b.text)
    out = extract_json(text)
    if out is None:
        raise AgentOutputError(getattr(agent, "name", "?"), text)
    return out


# ======================================================================
# JSON 提取 / 断言辅助（S-011：子串包含断言，§7）
# ======================================================================
def extract_json(text: str) -> dict | None:
    """提取回复文本里第一个完整 JSON 对象；**无合法 JSON 返回 None**。

    容忍 markdown 代码块与前后文噪音。返回 ``None``（而非 ``{}``）以区分
    「没有 JSON」与「JSON 恰好是空对象」——调用方据此决定失败语义
    （:func:`run_agent` 抛 AgentOutputError）。
    """
    if not text:
        return None
    text = re.sub(r"```(?:json)?|```", "", text)
    start = text.find("{")
    if start == -1:
        return None
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                raw = text[start : i + 1]
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    return None
    return None


# ======================================================================
# mock 路径：脚本化模型（确定性输出，无 Key / CI 场景验证管线）
# ======================================================================
class ScriptedJsonModel(ChatModelBase):
    """确定性脚本模型：第 1 次调用发一个工具调用（验证 MCP 管线），之后输出预置 JSON。"""

    class Parameters(ChatModelBase.Parameters):
        pass

    def __init__(self, output_json: dict, model: str = "scripted-json") -> None:
        super().__init__(
            credential=__import__("agentscope.credential").credential.CredentialBase(),
            model=model,
            parameters=self.Parameters(),
            stream=False,
        )
        self._output_json = output_json
        self._call_count = 0

    @classmethod
    def _get_retryable_exceptions(cls):
        return ()

    async def _call_api(self, model_name: str, messages, tools=None, tool_choice=None, **kwargs):
        self._call_count += 1
        if self._call_count == 1 and tools:
            name = tools[0]["function"]["name"]
            return _single(
                ToolCallBlock(id=f"call_smoke_{self._call_count}", name=name, input='{"service": "order-service"}')
            )
        return _single(TextBlock(text=json.dumps(self._output_json, ensure_ascii=False)))


def _single(content_block) -> AsyncGenerator:
    from agentscope.model import ChatResponse

    async def _gen():
        yield ChatResponse(content=[content_block], is_last=True)

    return _gen()
