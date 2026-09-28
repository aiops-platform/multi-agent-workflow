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
    # 步骤字段必须与 UI 渲染器 dgxOptionBody 读的键**同名**——缺一个就是那一块静默不渲染。
    # 曾因 schema 用 scope+expected 且没有 type/change/suggested_diff，导致类别标签、
    # 「怎么改」正文、示意 diff 三处**永远空白**，而 action 被当成短标签渲染却塞进整段长文。
    step = schema["properties"]["steps"]["items"]
    assert {"type", "action", "target", "change", "expected_effect",
            "verification", "rollback", "risk", "suggested_diff"} <= set(step["properties"])
    # 每个选项自带**自己的** steps：共用一份时页签切换毫无意义（点开哪个正文都一样）。
    assert "steps" in opt
    assert "steps" in d["options"]["items"]["required"], "选项必须必填 steps"
    assert opt["steps"]["items"] is step, "选项的 steps 必须与顶层同一份 schema，防两处漂移"


def test_remediation_plan_prompt_caps_output_size() -> None:
    """规划 prompt 必须带篇幅限额，且**写明理由** —— 超限等于整份 JSON 作废。

    实测 run_bc6418134f（2026-09-23）：plan 节点的 reasoning 写了 23,005 字符，
    输出预算（``deepseek_max_tokens=8192``）耗尽后答案只写到 3,973 字符就在**句子中间**
    被截断（结尾停在 ``…500→400 会改变其'``），下游却只看到「未输出合法 JSON（§7 输出契约未满足）」
    —— 报错把排查指向"JSON 写坏了"，而真相是"没写完"。

    与 reviewer 节点（TODO §32⑤，run_74a0db73ae / run_3f977237be）是**同一形态**，
    当时三条对策里的第 3 条就是本条：把限额与理由一起写进 prompt（改内置即生效，
    otr 没有该 agent 的 ``agent_configs`` 覆盖行）。
    """
    sp = SYSTEM_PROMPTS["remediation-planning-analyst"]
    assert "控制篇幅" in sp
    assert "不要贴代码" in sp
    # 理由必须写明：只给数字、不说"超了会怎样"，模型不会当回事（照抄 reviewer 那条的写法）
    assert "被截断" in sp
    assert "整段 JSON 作废" in sp
    # suggested_diff 是体积大户（schema 里「给人看、无执行语义」），必须单独限额
    assert "≤ 15 行" in sp
    # 自检清单里也要有一条，否则限额只是"建议"
    assert "规则 9 的限额" in sp


def test_code_locator_prompt_has_budget_convergence_rule() -> None:
    """`code-locator` 必须带**收口判据** —— 否则会在没有栈帧时无限猜关键词、耗尽轮次。

    实测 run_a7e825f855（2026-09-23）：`locate` 拿到的日志证据里**没有 `stack_trace`**，
    于是它转为在仓库里搜 `"out of bounds"` / `"Index"` / `"split("` 这类通用词 ——
    **猜测型搜索没有收敛判据**，正好把 `_DEFAULT_MAX_ITERS = 10` 用完
    （trace 里 10 次 llm_call + 23 次 tool_call），最终
    `AgentOutputError: Executed maximum iterations of reasoning-acting loop`（仅 79 字符，
    **不是超长**）→ 负证据 `found: false` → 命中 `locate → halt` → **整条诊断中断**。

    注意成因与 plan 那条（输出 token 截断）**完全不同**，别用同一个修法。
    """
    sp = SYSTEM_PROMPTS["code-locator"]
    assert "预算意识" in sp
    assert "必须收口" in sp
    # 关键是点名"没有栈帧时不要猜关键词"这个具体陷阱
    assert "没有栈帧" in sp
    assert "不要靠关键词猜仓库" in sp
    # 要写出后果，否则模型不会当回事（与 reviewer / plan 两条同一手法）
    assert "轮次用尽" in sp
    assert "halt" in sp


def test_fix_planner_declares_a_falsifiable_verification_probe() -> None:
    """`fix-planner` 必须产出**可证伪**的部署后探针（`verification_probe`）—— 三条约束都得在。

    为什么它是提示词契约而不是"随便加个字段"：交付链最后一步（`verify-deploy`）拿它去打冒烟，
    而**探针的质量完全由这里决定**。没有这三条约束，这条路就退化成"让模型编一个 URL"：

    1. **路径只能取自入参**（工单 / rca / plan 里**出现过**的接口）—— 同 `service-scoper`
       那条已写死的判据「不要自己添加工具没返回的候选」。编出来的必然 404：**响亮地红，
       但那是噪声**（它证明的不是"修复失败"，而是"提示词没约束住"）。
    2. **`broken_expect` 必填且与 `expect` 不同** —— 把"这条探针有没有鉴别力"从**判断题**
       变成**声明 + 校验**（相同 ⇒ 下游直接拒）。实测反例 `/quotation/exception`：
       它看着最像"该探的"（故障注入入口、名字带 exception），但修复把它从裸 NPE 500
       变成了**受控的业务异常 400** —— 那是修复的*设计语义*；配 `expect: 200` 会**永远红**。
    3. **给不出就明确不给**（`null`）—— CPU 打满那类故障没有 HTTP 链路，静默退化成
       `coverage: health_only` 是对的；**编一条**才是错的。
    """
    sp = SYSTEM_PROMPTS["fix-planner"]
    assert "verification_probe" in sp
    # ① 只取自入参
    assert "路径只能取自入参" in sp
    assert "不许自己造一条" in sp
    # ② 两个码都要给、且必须不同（附反例，否则模型不会当回事 —— 与 reviewer/plan 同一手法）
    assert "broken_expect" in sp
    assert "必须给、且必须不同" in sp
    # ③ 给不出就不给
    assert "给不出就填" in sp
    # ④ ⭐ **状态码真的要变** ——这条是实测踩出来的（2026-09-28 run_f2ce1b59c9）：
    # 提示词里原先把 `/quotation/exception` 写成「修复把它从 500 变成了受控的 400」，
    # 模型**照抄**这个例子产出 `expect: 400` —— 而实测那条路径修复前后**都是 500**
    # （NPE → 受控 QuotationException，`GlobalExceptionHandler` 把两者都映射成 500；
    # 不带参数时的 400 其实是 Spring 的「缺必填参数」）。后果是交付链**假红**。
    # 所以提示词必须点破这类"只改日志签名/异常类型"的修复，并要求填 null。
    assert "状态码要真的变" in sp
    assert "只改了日志/异常类型" in sp
    assert "需用日志验证" in sp

    # schema 与提示词对齐：`null` 合法（D6），而三个字段在对象形态下**全必填**
    probe = AGENT_SCHEMAS["fix-planner"]["properties"]["plan"]["properties"]["verification_probe"]
    assert "null" in probe["type"], "verification_probe 必须允许 null（基础设施类故障没有 HTTP 链路）"
    assert set(probe["properties"]) == {"path", "expect", "broken_expect"}
    assert set(probe["required"]) == {"path", "expect", "broken_expect"}


def test_smoke_tester_is_wired_into_the_verdict_path() -> None:
    """`smoke-tester`（`verify-deploy` 的 agent）接线完整：注册表 / 提示词 / schema / 判红 四处齐。

    这个 agent **本地零工具**（工具全在 MCP `deploy-ops`，绑定在 `seed/dataplane.yaml`），
    所以这里只锁"代码里那四处"，绑定由 `tests/test_seed_defaults.py` 那一族守。

    为什么单拎出来测：加一个 agent 要同时改 4 个地方，**漏一处都不报错**——
    漏 `FIX_AGENTS` ⇒ `AGENT_REGISTRY` 里没有它（`get_agent_spec` KeyError 要到运行期才现形）；
    漏 `AGENT_SCHEMAS` ⇒ agent 装配出的 schema 是空的（模型不知道要输出什么）；
    漏 `VERDICT_FIELDS` ⇒ **冒烟不过也判绿**，这条链白加。
    """
    from agentflow.executor.dag_executor import SIDE_EFFECT_AGENTS, VERDICT_FIELDS

    assert "smoke-tester" in FIX_AGENTS
    assert AGENT_STAGES["smoke-tester"] == "verify"       # 与 tester/reviewer 同一段
    assert AGENT_DESCRIPTIONS["smoke-tester"]
    assert "smoke-tester" in SYSTEM_PROMPTS
    assert "smoke-tester" in AGENT_SCHEMAS
    # 结论字段与 schema 对齐（写错字段名 = 判红永不生效，且不会有任何报错）
    field = VERDICT_FIELDS["smoke-tester"]
    assert field in AGENT_SCHEMAS["smoke-tester"]["properties"]
    assert field in AGENT_SCHEMAS["smoke-tester"]["required"]
    # 只读（只发 GET）⇒ 不进副作用清单；但它在判红路径里（它答的是"成没成"）
    assert "smoke-tester" not in SIDE_EFFECT_AGENTS

    sp = SYSTEM_PROMPTS["smoke-tester"]
    assert "deploy-ops" in sp
    # 结论只能来自工具的真实返回，且 health_only 不许被说成"业务链路也验过了"
    assert "只能来自工具的真实返回" in sp
    # ⚠️ **工具报错也要判红**：出错的失败只活在工具的错误信息里，节点判红读的是 agent 的
    # `passed` —— 不写这条就等于把"这次没验成"押在模型自觉上（fail-open）。
    assert "工具报错时同样输出 `passed: false`" in sp
    assert "pod_not_found" in sp and "deployment_probe_missing" in sp and "forward_failed" in sp
    assert "不要因为它是 null 就自己造一条路径" in sp
    assert "coverage: health_only" in sp
    # 不重试、不回滚：一过性抖动与"服务真的起不来"混在一起，重试只会把后者洗成绿
    assert "不要重试" in sp


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
