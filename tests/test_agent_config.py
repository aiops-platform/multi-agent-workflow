"""AgentConfigResolver 测试：DB 覆盖/内置静态默认合并解析（agents 层，纯内存构造）。

纯构造入参（store.list() 形状的行），不碰 DB：验证 merge（NULL→静态回退）、
mcp_server_ids 两态（NULL/[]=无 server；非空=精确子集）、all() 顺序 = 内置 DIAGNOSE+FIX → 自定义。
"""
from agentflow.agents.agent_config import AgentConfigResolver
from agentflow.agents.prompts import AGENT_SCHEMAS, SYSTEM_PROMPTS
from agentflow.agents.registry import (
    AGENT_DESCRIPTIONS,
    AGENT_STAGES,
    DIAGNOSE_AGENTS,
    FIX_AGENTS,
)

BUILTIN_15 = DIAGNOSE_AGENTS + FIX_AGENTS


def _row(name: str = "triage", **over) -> dict:
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


def test_resolve_db_row_override_with_static_fallback() -> None:
    res = AgentConfigResolver([_row(description="覆盖描述", enabled=False)])
    r = res.resolve("triage")
    assert r.name == "triage"
    assert r.description == "覆盖描述"  # DB 覆盖
    assert r.system_prompt == SYSTEM_PROMPTS["triage"]  # NULL → 回退静态
    assert r.schema == AGENT_SCHEMAS.get("triage", {})  # NULL → 回退静态
    assert r.enabled is False
    assert r.origin == "builtin"
    assert r.mcp_server_ids == set()  # NULL → 无 server（两态）


def test_resolve_builtin_with_no_row_returns_static_default() -> None:
    r = AgentConfigResolver([]).resolve("triage")
    assert r is not None
    assert r.role == "diagnose"
    assert r.stage == AGENT_STAGES["triage"]
    assert r.description == AGENT_DESCRIPTIONS["triage"]
    assert r.system_prompt == SYSTEM_PROMPTS["triage"]
    assert r.origin == "builtin"
    assert r.mcp_server_ids == set()  # 无 DB 行 → 无 server（两态）
    assert r.enabled is True


def test_resolve_reasoning_enabled_flag() -> None:
    """Agent 级推理开关：DB 行 True → 生效；缺省/内置静态 → False。"""
    assert AgentConfigResolver([_row(reasoning_enabled=True)]).resolve("triage").reasoning_enabled is True
    assert AgentConfigResolver([_row()]).resolve("triage").reasoning_enabled is False  # DB 行缺省
    assert AgentConfigResolver([]).resolve("triage").reasoning_enabled is False  # 内置静态默认


def test_resolve_unknown_name_returns_none() -> None:
    assert AgentConfigResolver([]).resolve("not-an-agent") is None


def test_resolve_custom_row() -> None:
    rows = [
        _row(
            name="custom-x",
            origin="custom",
            role="fix",
            stage="fix",
            system_prompt="自定提示",
            schema={"type": "object"},
            mcp_server_ids=["m1"],
        )
    ]
    r = AgentConfigResolver(rows).resolve("custom-x")
    assert r.origin == "custom"
    assert r.role == "fix"
    assert r.stage == "fix"
    assert r.system_prompt == "自定提示"
    assert r.schema == {"type": "object"}
    assert r.mcp_server_ids == {"m1"}
    assert r.enabled is True


def test_server_ids_for_two_state() -> None:
    """两态（v1.12.1）：NULL/[] = 无 server；非空数组 = 精确子集；未命中同样空集。"""
    # NULL（未配置）→ 空 set（无 server）
    assert AgentConfigResolver([_row()]).server_ids_for("triage") == set()
    # []（明确不绑，存储已归一 NULL）→ 同样空 set
    assert AgentConfigResolver([_row(mcp_server_ids=[])]).server_ids_for("triage") == set()
    # [mid,…] → 精确子集
    assert AgentConfigResolver([_row(mcp_server_ids=["m1", "m2"])]).server_ids_for("triage") == {"m1", "m2"}
    # 非内置且无 DB 行 → 空 set（无 server）
    assert AgentConfigResolver([]).server_ids_for("ghost") == set()


def test_all_builtin_then_custom_order() -> None:
    rows = [
        _row(name="triage", description="覆盖"),
        _row(name="custom-x", origin="custom", role="diagnose", stage="other", system_prompt="sp"),
    ]
    resolved = AgentConfigResolver(rows).all()
    assert [a.name for a in resolved] == BUILTIN_15 + ["custom-x"]
    triage = next(a for a in resolved if a.name == "triage")
    assert triage.description == "覆盖"  # 内置行按 DB 覆盖合并
    assert triage.origin == "builtin"
    custom = resolved[-1]
    assert custom.origin == "custom"
    assert custom.system_prompt == "sp"


def test_names_returns_all_names() -> None:
    rows = [_row(name="custom-a", origin="custom", role="diagnose", stage="other", system_prompt="sp")]
    assert AgentConfigResolver(rows).names() == BUILTIN_15 + ["custom-a"]


def test_get_returns_raw_row_or_none() -> None:
    res = AgentConfigResolver([_row(description="覆盖")])
    assert res.get("triage")["description"] == "覆盖"  # 原始行（未合并）
    assert res.get("missing") is None


def test_remediation_plan_prompt_has_direction_contract() -> None:
    """remediation-planning-analyst 静态默认：decisions 方向契约（形态 A）+ 既有输出契约不破坏。"""
    name = "remediation-planning-analyst"
    sp = SYSTEM_PROMPTS[name]
    schema = AGENT_SCHEMAS[name]
    # 方向显式化 + recommended 默认采纳（形态 A：通过=采纳推荐 / 改选=带方向驳回重写）
    assert "decisions[" in sp
    assert "recommended" in sp
    # 三条新规都在：禁止静默选边 / 出稿前先核实 / 交付前自检
    assert "禁止静默选边" in sp
    assert "出稿前先核实" in sp
    assert "交付前自检" in sp
    # decisions schema：options/recommended/accept_criteria + 选项字段齐全
    d = schema["properties"]["decisions"]["items"]["properties"]
    assert {"id", "question", "options", "recommended", "accept_criteria"} <= set(d)
    opt = d["options"]["items"]["properties"]
    assert {"pros", "cons", "effort", "risk", "rollback"} <= set(opt)
    # 既有步骤契约不破坏（rollback 仍必填项）
    assert "rollback" in schema["properties"]["steps"]["items"]["properties"]
    assert "required" in schema["properties"]["steps"]["items"]


def test_resolve_custom_row_null_prompt_falls_back_to_canonical() -> None:
    """自定义行字段清空（NULL）→ 回退到 prompts.py 的 canonical 静态默认（非通用兜底提示）。"""
    rows = [
        _row(
            name="remediation-planning-analyst",
            origin="custom",
            role="fix",
            stage="fix",
            system_prompt=None,
            schema=None,
        )
    ]
    r = AgentConfigResolver(rows).resolve("remediation-planning-analyst")
    assert r.system_prompt == SYSTEM_PROMPTS["remediation-planning-analyst"]
    assert r.schema == AGENT_SCHEMAS["remediation-planning-analyst"]
    assert r.origin == "custom"
