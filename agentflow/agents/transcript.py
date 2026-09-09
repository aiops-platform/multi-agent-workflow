"""节点级明细采集：TraceRecorder（AgentScope middleware）→ ``node_traces`` 行。

本模块是**纯采集**数据源：不碰 StateStore、不持有 run_id（executor 按 (run,node) 落库），
任何采集失败都被吞掉（logger debug 记录），绝不影响 Agent 主流程。行分类：
- kind='llm_call'（``on_model_call``）：每次喂给模型的完整 messages 原文 + 本次 usage；
- kind='tool_call'（``on_acting``）：**只包已通过权限执行**的工具——含 MCP server 名、
  输入、结果、read_only 尽力识别；DENY 的工具不进 on_acting；
- kind='denied'（``scan_denied_blocks``）：节点跑完后补扫 context，把被拒工具按 id 配对记下；
- kind='node' 汇总行由 AgentNodeRunner 生成（本模块不负责）。
"""
from __future__ import annotations

import inspect
import json
import logging
from typing import Any

from agentscope.middleware import MiddlewareBase

log = logging.getLogger("agentflow.transcript")

# kind 常量（供 executor/测试引用）
K_LLM_CALL = "llm_call"
K_TOOL_CALL = "tool_call"
K_DENIED = "denied"
K_NODE = "node"


def server_from_tool_name(name: str | None) -> str | None:
    """MCP 工具对 LLM 的名 = ``mcp__{server}__{sanitized_tool}`` → 还原 server。

    普通 function tool（``query_logs``）或畸形名（``mcp__abc`` 无工具段）→ None。
    """
    if not isinstance(name, str) or not name.startswith("mcp__"):
        return None
    parts = name.split("__")
    return parts[1] if len(parts) >= 3 else None


def _state_str(s: Any) -> str:
    """枚举/字符串/None → 稳定小写字符串（ToolResultState.DENIED → 'denied'）。"""
    if s is None:
        return ""
    return str(getattr(s, "value", s)).lower()


def _dump_block(b: Any) -> Any:
    """单个 content block → JSON 安全对象（DataBlock/URL/Base64 兜底描述）。"""
    if hasattr(b, "model_dump"):
        try:
            return b.model_dump(mode="json")
        except Exception:  # noqa: BLE001 —— bytes 等不可 JSON 化时降级
            pass
    # 兜底：取最常用的可序列化字段（含 thinking——assistant 推理块（ThinkingBlock）的正文）
    d: dict[str, Any] = {"type": getattr(b, "type", type(b).__name__)}
    for k in ("text", "thinking", "name", "id", "output", "input"):
        v = getattr(b, k, None)
        if v is None:
            continue
        if isinstance(v, (str, int, float, bool)):
            d[k] = v
        else:
            d[k] = _json_able(v)
    return d


def _json_able(v: Any) -> Any:
    """尝试把任意对象转成 JSON 安全结构；失败则退回可打印字符串。"""
    if isinstance(v, (dict, list)):
        try:
            return json.loads(json.dumps(v, ensure_ascii=False, default=str))
        except Exception:  # noqa: BLE001
            return str(v)
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    try:
        return json.loads(json.dumps(v, ensure_ascii=False, default=str))
    except Exception:  # noqa: BLE001
        return str(v)


def _dump_msg(m: Any) -> dict[str, Any]:
    """Msg → JSON 安全 dict（content 逐 block 展开；失败退回字符串摘要）。"""
    if m is None:
        return {}
    out: dict[str, Any] = {}
    for k in ("role", "name"):
        if getattr(m, k, None) is not None:
            out[k] = getattr(m, k)
    blocks = getattr(m, "content", None)
    if isinstance(blocks, list):
        out["content"] = [_dump_block(b) for b in blocks]
    else:
        out["content"] = _json_able(blocks)
    return out


def _dump_messages(messages: Any) -> list[dict]:
    try:
        return [_dump_msg(m) for m in (messages or [])]
    except Exception:  # noqa: BLE001
        return []


def _tool_names(tools: Any) -> list[str]:
    """模型侧 tools 参数 → 工具名列表（留名即可，schema 可由名回溯，避免每轮重复大行）。"""
    names: list[str] = []
    for t in tools or []:
        if isinstance(t, dict):
            fn = t.get("function") or {}
            n = fn.get("name") if isinstance(fn, dict) else None
            if n:
                names.append(n)
    return names


def _parse_input_json(raw: Any) -> Any:
    """ToolCallBlock.input 是 JSON 字符串 → 解析为 dict；失败原样返回（不含已含工具名）。"""
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:  # noqa: BLE001
            return raw
    return _json_able(raw)


def _content_text(content: Any, cap: int = 0) -> str:
    """从 block/content 列表抽取文本（ToolResponse.content）。cap>0 截断超长文本。"""
    parts: list[str] = []
    total = 0
    for b in content or []:
        t = getattr(b, "text", None)
        if isinstance(t, str):
            parts.append(t)
            total += len(t)
            if cap and total > cap:
                break
    return "".join(parts)


def _reasoning_text(content: Any) -> str:
    """从模型响应的 content 里提取 ThinkingBlock 推理全文。

    非流式：整段一块（``ThinkingBlock(type='thinking', thinking=…)``）；流式：每 chunk 携带该
    推理流的**增量**段。这里只把当前 content 里的增量段串起来（不跨 chunk 累积）——
    ``on_model_call`` 对流式逐 chunk 收集增量、最后 ``"".join`` 还原完整思维链。
    """
    parts: list[str] = []
    for b in content or []:
        if getattr(b, "type", None) == "thinking":
            t = getattr(b, "thinking", "")
            if t:
                parts.append(t)
    return "".join(parts)


class TraceRecorder(MiddlewareBase):
    """按节点实例化的采集 middleware：每次工具/模型调用 append 到 ``self.rows``。

    ``rows`` 每项 = ``{"kind", "name", "payload"}``（name 是工具/模型名，node 行用 None）。
    仅实现 on_model_call / on_acting 两个钩子；异常在内部捕获。
    """

    def __init__(self, node_id: str, agent_name: str) -> None:
        self.node_id = node_id
        self.agent_name = agent_name
        self.rows: list[dict[str, Any]] = []

    def _add(self, kind: str, name: str | None, payload: dict[str, Any]) -> None:
        self.rows.append({"kind": kind, "name": name, "payload": payload})

    # ---- on_model_call：一次 LLM 推理 = 一行 ----
    async def on_model_call(self, agent, input_kwargs: dict, next_handler):
        messages = _dump_messages(input_kwargs.get("messages"))
        names = _tool_names(input_kwargs.get("tools"))
        model_name = getattr(input_kwargs.get("current_model"), "model", None)
        res = await next_handler()  # 模型异常照常向 Agent 传播（保留 retry/回退语义）
        # 注意用 inspect.isasyncgen 而非 hasattr(res,'__aiter__')：AgentScope 的
        # ChatResponse 自定义 __getattr__ 对缺失 dunder 抛 KeyError，hasattr 不吞 KeyError
        if inspect.isasyncgen(res):
            # streaming：usage 只在末 chunk；thinking 推理是逐 chunk 增量，包一层收集齐再记
            async def _gen():
                usage = None
                reasoning_parts: list[str] = []
                try:
                    async for chunk in res:
                        u = getattr(chunk, "usage", None)
                        if u is not None:
                            usage = u
                        # DeepSeek 末 chunk（is_last=True）会在 content 里**重发**整段已累积的
                        # thinking（= 之前所有增量块的拼接）→ 跳过防重复；非末 chunk 只带增量。
                        if not getattr(chunk, "is_last", False):
                            t = _reasoning_text(getattr(chunk, "content", None) or [])
                            if t:
                                reasoning_parts.append(t)
                        yield chunk
                finally:
                    try:
                        reasoning = "".join(reasoning_parts) or None
                        self._add_llm(messages, names, model_name, usage, reasoning)
                    except Exception:
                        log.debug("transcript on_model_call record failed", exc_info=True)

            return _gen()
        try:
            reasoning = _reasoning_text(getattr(res, "content", None) or []) or None
            self._add_llm(messages, names, model_name, getattr(res, "usage", None), reasoning)
        except Exception:
            log.debug("transcript on_model_call record failed", exc_info=True)
        return res

    def _add_llm(
        self,
        messages: list[dict],
        tool_names: list[str],
        model_name,
        usage,
        reasoning: str | None = None,
    ) -> None:
        u: dict[str, int] = {}
        if usage is not None:
            u = {
                "input_tokens": getattr(usage, "input_tokens", None) or 0,
                "output_tokens": getattr(usage, "output_tokens", None) or 0,
            }
        payload: dict[str, Any] = {"messages": messages, "tools": tool_names, "usage": u}
        # 有推理内容（thinking-enabled 模型的思维链）才写字段，避免无推理场景的空串噪音
        if reasoning:
            payload["reasoning"] = reasoning
        self._add(K_LLM_CALL, model_name or "llm", payload)

    # ---- on_acting：一次已放行工具执行 = 一行（只含 ALLOW 的工具）----
    async def on_acting(self, agent, input_kwargs: dict, next_handler):
        tc = input_kwargs.get("tool_call")
        last = None
        try:
            ag = next_handler()
            async for item in ag:
                last = item
                yield item
        finally:
            try:
                await self._record_tool(agent, tc, last)
            except Exception:
                log.debug("transcript on_acting record failed", exc_info=True)

    async def _record_tool(self, agent, tc, last) -> None:
        name = getattr(tc, "name", None)
        inp = _parse_input_json(getattr(tc, "input", None))
        # 尽力读工具元数据：只读提示 / 是否 MCP 工具（缺属性时静默降级）
        is_mcp, read_only = False, None
        try:
            tool = await agent.toolkit.get_tool(name) if name else None
            is_mcp = bool(getattr(tool, "is_mcp", False))
            read_only = getattr(tool, "is_read_only", None)
        except Exception:  # noqa: BLE001
            pass
        # 结果：末对象通常是 ToolResponse(content=[TextBlock...], state=enum)
        result_state = None
        result = ""
        if last is not None:
            result_state = _state_str(getattr(last, "state", None)) or None
            result = _content_text(getattr(last, "content", None))
        self._add(
            K_TOOL_CALL,
            name,
            {
                "tool_call_id": getattr(tc, "id", None),
                "server": server_from_tool_name(name),
                "is_mcp": is_mcp,
                "read_only": read_only,
                "input": inp,
                "result_state": result_state,
                "result": result,
            },
        )


def scan_denied_blocks(context) -> list[dict]:
    """补扫 context 中被权限系统 DENY 的工具调用（kind='denied' 行）。

    DENY 工具不会进 ``on_acting``，而是在 context 里以 ``ToolResultBlock(state='denied')``
    形式留下（output 为 deny message）。按 block.id 与同轮的 ``ToolCallBlock`` 配对，
    取出工具名与尝试输入；无配对时降级用 result 自身字段。
    """
    calls: dict[str, Any] = {}
    denied: list[Any] = []
    for msg in context or []:
        blocks = getattr(msg, "content", None)
        if not isinstance(blocks, list):
            continue
        for b in blocks:
            btype = type(b).__name__
            bid = getattr(b, "id", None)
            if btype == "ToolCallBlock":
                calls[bid] = b
            elif btype == "ToolResultBlock" and _state_str(getattr(b, "state", None)) == "denied":
                denied.append(b)
    out: list[dict[str, Any]] = []
    for d in denied:
        c = calls.get(getattr(d, "id", None))
        tool_name = getattr(d, "name", None) or (getattr(c, "name", None) if c else None)
        reason = getattr(d, "output", None)
        out.append({
            "kind": K_DENIED,
            "name": tool_name,
            "payload": {
                "server": server_from_tool_name(tool_name),
                "input": _parse_input_json(getattr(c, "input", None)) if c else None,
                "reason": reason if isinstance(reason, str) else _content_text(reason),
            },
        })
    return out
