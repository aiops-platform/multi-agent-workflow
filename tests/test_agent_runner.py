"""AgentNodeRunner × AgentConfigResolver 接线测试：enabled/disabled 短路 + system_prompt 覆盖透传。

monkeypatch runner 模块级的 build_agent/run_agent/build_toolkit/build_permission_context，
把真实 agent/LLM/toolkit 都替换掉，只验证 runner 读 DB 配置（AgentConfigResolver）后的装配决策。
"""
import agentflow.agents.runner as runner_mod
from agentflow.agents.agent_config import AgentConfigResolver
from agentflow.agents.runner import AgentNodeRunner
from agentflow.core.dag import Node


def _cfg_row(name: str = "triage", **over) -> dict:
    row = {
        "name": name,
        "origin": "builtin",
        "role": "diagnose",
        "stage": "detect",
        "description": None,
        "system_prompt": None,
        "schema": None,
        "mcp_server_ids": None,
        "enabled": True,
        "reasoning_enabled": False,
    }
    row.update(over)
    return row


def _noop_assemblers(monkeypatch, captured: dict | None = None) -> None:
    """把 build_toolkit / build_agent / run_agent 换成记录型假实现（captured 收集装配参数）。"""

    def _toolkit(agent: str, **kw):
        return object()

    def _perm(agent: str, **kw):
        return object()

    def _build_agent(name: str, toolkit, model, **kw):
        if captured is not None:
            captured["system_prompt"] = kw.get("system_prompt")
            captured["model"] = model
        return object()

    async def _run(agent, user_input):
        if captured is not None:
            captured["ran"] = True
        return {"ok": True}

    monkeypatch.setattr(runner_mod, "build_toolkit", _toolkit)
    monkeypatch.setattr(runner_mod, "build_permission_context", _perm)
    monkeypatch.setattr(runner_mod, "build_agent", _build_agent)
    monkeypatch.setattr(runner_mod, "run_agent", _run)


async def test_disabled_agent_short_circuits_without_llm(monkeypatch) -> None:
    captured = {}
    _noop_assemblers(monkeypatch, captured)
    runner = AgentNodeRunner(None, agent_config=AgentConfigResolver([_cfg_row(enabled=False)]))
    out = await runner(Node(id="n1", agent="triage"), {"bug": "x"})
    assert out["node"] == "n1" and out["ok"] is True and out["disabled"] is True
    assert "note" in out  # 附带中文说明
    assert captured.get("ran") is None  # 未走到 run_agent（无 LLM 调用）


async def test_db_system_prompt_forwarded_to_build_agent(monkeypatch) -> None:
    captured = {}
    _noop_assemblers(monkeypatch, captured)
    runner = AgentNodeRunner(
        None,
        agent_config=AgentConfigResolver([_cfg_row(system_prompt="DB 覆盖提示词")]),
    )
    out = await runner(Node(id="n1", agent="triage"), {"bug": "x"})
    assert out == {"ok": True}
    assert captured["system_prompt"] == "DB 覆盖提示词"


async def test_no_resolver_passes_none_system_prompt(monkeypatch) -> None:
    """agent_config=None（未接 resolver）→ system_prompt=None → build_agent 内回退内置静态。"""
    captured = {}
    _noop_assemblers(monkeypatch, captured)
    runner = AgentNodeRunner(None)
    out = await runner(Node(id="n1", agent="triage"), {"bug": "x"})
    assert out == {"ok": True}
    assert captured["system_prompt"] is None


async def test_enabled_config_still_runs_agent(monkeypatch) -> None:
    """enabled=True（默认/DB 行）→ 正常装配执行，不短路。"""
    captured = {}
    _noop_assemblers(monkeypatch, captured)
    runner = AgentNodeRunner(None, agent_config=AgentConfigResolver([_cfg_row()]))
    out = await runner(Node(id="n1", agent="triage"), {"bug": "x"})
    assert out == {"ok": True}
    assert captured.get("ran") is True


async def test_reasoning_enabled_routes_to_reasoning_model(monkeypatch) -> None:
    """cfg.reasoning_enabled=True → 懒构建推理模型并把它交给 build_agent（其余装配不变）。"""
    captured = {}
    _noop_assemblers(monkeypatch, captured)
    sentinel = object()
    monkeypatch.setattr(runner_mod, "build_reasoning_model", lambda: sentinel)
    runner = AgentNodeRunner(
        None,
        agent_config=AgentConfigResolver([_cfg_row(reasoning_enabled=True)]),
    )
    out = await runner(Node(id="n1", agent="triage"), {"bug": "x"})
    assert out == {"ok": True}
    assert runner._reasoning_model is not None  # 懒构建缓存命中
    assert captured["model"]._model is sentinel  # 节点用的是推理模型（非全局正常模型）


async def test_reasoning_disabled_keeps_base_model(monkeypatch) -> None:
    """cfg.reasoning_enabled=False（缺省）→ 用全局正常模型，不建推理模型。"""
    captured = {}
    _noop_assemblers(monkeypatch, captured)
    runner = AgentNodeRunner(None, agent_config=AgentConfigResolver([_cfg_row()]))
    out = await runner(Node(id="n1", agent="triage"), {"bug": "x"})
    assert out == {"ok": True}
    assert runner._reasoning_model is None  # 未触碰推理路径
    assert captured["model"] is runner.model  # 就是全局正常模型


# ---- build_reasoning_model 工厂：有 Key → DeepSeek thinking；无 Key → ScriptedJsonModel ----
def test_build_reasoning_model_thinking_enabled_with_key() -> None:
    from agentscope.model import DeepSeekChatModel

    from agentflow.agents.scopes import build_reasoning_model
    from agentflow.config import Settings

    s = Settings(
        deepseek_api_key="sk-test",
        deepseek_base_url="http://localhost:8001/v1",
        deepseek_model="deepseek-v4-flash",
        _env_file=None,
    )
    m = build_reasoning_model(s)
    assert isinstance(m, DeepSeekChatModel)
    assert m.parameters.thinking_enable is True  # thinking 模式 → CoT
    assert m.credential.base_url == "http://localhost:8001/v1"  # 走 testbed 网关


def test_build_reasoning_model_no_key_falls_back_scripted() -> None:
    from agentflow.agents.scopes import ScriptedJsonModel, build_reasoning_model
    from agentflow.config import Settings

    # 显式空 key（覆盖环境变量里的真 key）→ 回退脚本模型，保持封闭
    assert isinstance(build_reasoning_model(Settings(deepseek_api_key="", _env_file=None)), ScriptedJsonModel)
