"""场景2 完整修复工作流（`agentflow/seed/workflows/scenario2-bug-fix.yaml`）。

与 `test_problem_diagnose_fix_workflow.py` 是**同一条修复段的两个副本**：
那份没有诊断链（诊断结论走工单入参），这份自带诊断链。发布链（merge → ci →
approve-deploy → deploy → verify-deploy）两份都有，形状应当一致。

本文件锁五件事——它们的共同点是**坏了不会报错**：

1. **能加载**——即已过 `core/dag.py` 的静态校验（无环 / 悬空 / join 一致性 /
   params 只引上游）。种子侧还有一道 CI 闸门（`tests/test_seed_defaults.py` 的
   corpus + schema 字段核对），两边都要过。
2. **收尾压在 `verify-deploy` 之后**——它的入边**改过一次**（`commit` → `verify-deploy`）。
   挂在 `commit` 上时它只证明"PR 开出来了"：后面合并/构建/部署/冒烟四步全没发生
   也照样宣布"处理流程走完了"。位置错了**不会报错**，只会让看板把一条没交付的 run
   算成闭环。
3. **服务名只有一个来源**——5 个发布节点都 `require: [service, ...]` 且取自
   `$.nodes.locate.output.service`。⚠️ **照抄 `problem-diagnose-fix` 那份的
   `$.inputs.bug_report.cmdb_ci.name` 是这条链最容易犯的错**：本仓实测工单的
   `cmdb_ci` 只有 `{name, service}`，而本图的入口工单连 `cmdb_ci` 都不保证有 ——
   写错了会让 5 个节点在真实工单上**全线 fail-fast**，而单测里如果 fixture 也写错，
   两边一起错、测试照样绿。
4. **三道门的 `on_reject` 都显式写出来，且判据是"门之前不可逆的事发生了没有"**——
   `approve-plan` 与 `approve-deploy` 是 `abort`（且**都没有**驳回边）；
   `approve-commit` 是 `continue`（驳回 → recap，不中止）。
   `on_reject` 默认值是 `abort`，写错方向图上不会报错，只会静默换语义。
5. **发布链的结论字段是"判红"而不是"走边"**——`merged` / `built` / `deployed` / `passed`
   为 `false` 时节点 FAILED、整条 run failed（`VERDICT_FIELDS`），**没有**退回 recap 的边。
   留一条那样的边 = 读图的人以为"没部署成功还能走到复盘收口"，而它永远不可达。

用脚本化 runner（不调 LLM、不连 MCP），跑得快且确定性。
"""
from __future__ import annotations

import pytest

from agentflow.core.dag import DONE, SKIPPED
from agentflow.core.workflow import Workflow
from agentflow.executor.dag_executor import DAGExecutor, WorkflowNodeFailed
from agentflow.seed import load_workflow_seeds
from agentflow.statestore.memory import InMemoryStateStore

#: 从**种子**里取 YAML 而不是从文件路径取：这条流程是靠种子发给新租户的，
#: 走的正是 `load_workflow_seeds()`（manifest → 文本 → 逐字节入库）。
#: 直接读文件的写法会在"manifest 忘了登记"时照样通过——那正是要拦的。
SEED_ID = "seed-scenario2-bug-fix"

PLAN_GATE = "approve-plan"
COMMIT_GATE = "approve-commit"
DEPLOY_GATE = "approve-deploy"
VERIFY_NODE = "verify-deploy"

#: 发布链的 5 个节点，按图的顺序。测试多处按它遍历。
RELEASE_CHAIN = ["merge", "ci", DEPLOY_GATE, "deploy", VERIFY_NODE]

#: 结论字段（`VERDICT_FIELDS`）：为 `false` ⇒ 节点判红、run 失败。
#: `ci-builder` 的 `built` 语义是**产物就绪**（jar 与镜像都成了），不是"jar 出来了"。
#:
#: ⚠️ **那张表是按 agent 名索引的，不是节点 id** —— 本图两者刻意不同名
#: （`merge`/`merger`、`ci`/`ci-builder`、`deploy`/`deployer`、`verify-deploy`/`smoke-tester`）。
#: 按节点 id 查会**恒得到 None**，于是"查不到"看起来就像"不需要覆盖"。
VERDICT = {"merge": ("merger", "merged"), "ci": ("ci-builder", "built"),
           "deploy": ("deployer", "deployed"), VERIFY_NODE: ("smoke-tester", "passed")}

INPUTS = {
    "bug_report": {
        "number": "PR-0007",
        "short_description": "结账无响应（warranty fin 缺参 + 吞异常）",
        "cmdb_ci": {"name": "warranty-service"},
    },
    "window_start": "2026-09-10T08:30:00Z",
    "window_end": "2026-09-10T10:30:00Z",
}

#: `plan` 节点产出的业务探针（`FixPlanSchema.plan.verification_probe`）。
PROBE_FIXTURE = {"path": "/warranty/claim?orderId=ORD001", "expect": 200, "broken_expect": 500}

#: 各节点的"正常路径"输出（键名与各 agent 的 schema 对齐）。**必须给全**：
#: 默认 mock 输出 `{"node": ..., "ok": True}` 里没有 `passed` / `approved` / `insufficient`，
#: 而 `when: $.nodes.scope.output.insufficient == false` 之类的边取不到字段即**不满足** ——
#: 条件边会静默走成另一条路（这里就是 `halt`），测试于是测的根本不是成功路径。
OK_OUTPUTS: dict[str, dict] = {
    # ── 诊断链 ──────────────────────────────────────────────────────────
    "triage": {"symptom_type": "code_bug", "severity": "high", "summary": "结账接口无响应"},
    # ⚠️ `insufficient: false` **必须给**：给漏了 → `scope → logs/trace/metrics/infra`
    #    四条边全失活 → 取数节点 SKIPPED → `rca` 的 required_edges 凑不齐 →
    #    整条链静默塌到 `halt`，而 run 照样是 done。
    "scope": {"insufficient": False, "candidate_services": ["warranty-service"],
              "primary_service": "warranty-service", "summary": "warranty 业务域"},
    "logs": {"found": True, "services": ["warranty-service"], "error_type": "NullPointerException",
             "summary": "warranty-service 报 NPE"},
    "trace": {"found": True, "failing_service": "warranty-service", "summary": "故障 span 在 fin 参数校验"},
    "metrics": {"summary": "错误率上升"},
    "infra": {"summary": "Pod 正常"},
    # ⚠️ `found: true` + `service` 都要给：`locate → halt` 的判据是 `found == false`，
    #    而 5 个发布节点的 `service` 全取自这里。
    "locate": {"found": True, "service": "warranty-service",
               "repo_url": "https://github.com/xqfgbc/aiops-test-warranty-service",
               "target_source": "trace", "summary": "定到 WarrantyService.java"},
    "know": {"found": True, "summary": "无相似历史事故"},
    # ⚠️ `insufficient: false`（`rca → halt` 的判据）。
    "rca": {"insufficient": False, "confidence": 0.82, "root_cause_type": "code_bug",
            "summary": "fin 参数为 null 时未校验，异常被吞", "hypotheses": ["fin 缺失"],
            "ruled_out": ["基础设施"]},
    # ⚠️ **形状照 `FixPlanSchema` 来（`{"plan": {…}}` 嵌套），不是平铺的。**
    # 这里平铺过一次的代价：`verify-deploy` 的 `probe` 入参路径**无论如何都解析得出**，
    # 因为测试 fixture 里根本没有那条真路径。fixture 与真源的形状一致，断言才有意义。
    "plan": {"plan": {
        "summary": "补 fin 空值校验并让异常上抛",
        "steps": [{"type": "code_fix", "target": "WarrantyService.java"}],
        "verification_probe": PROBE_FIXTURE,
    }},
    # ── 修复段 ──────────────────────────────────────────────────────────
    "fix": {"diff": "--- a/WarrantyService.java\n+++ b/WarrantyService.java\n",
            "files_changed": ["WarrantyService.java"], "explanation": "加空值校验"},
    "test": {"passed": True, "tests_run": 12, "failed": []},
    "review": {"approved": True, "comments": [], "risk": "low"},
    "commit": {"pr_url": "https://github.com/xqfgbc/aiops-test-warranty-service/pull/42",
               "pr_number": 42, "base_sha": "abc123"},
    # ── 发布链 ──────────────────────────────────────────────────────────
    "merge": {"merged": True, "already_merged": False,
              "pr_url": "https://github.com/xqfgbc/aiops-test-warranty-service/pull/42",
              "pr_number": 42, "merge_commit": "d34db33f", "head_ref": "aiops/RUN_run_s2"},
    "ci": {"built": True, "image_built": True, "image_tag": "warranty-service:d34db33f0000",
           "artifact": "build/libs/warranty-service-0.0.1-SNAPSHOT.jar", "artifact_bytes": 35285684,
           "merge_commit": "d34db33f"},
    "deploy": {"deployed": True, "image_tag": "warranty-service:d34db33f0000",
               "observed_image": "warranty-service:d34db33f0000", "pod": "warranty-service-x-y",
               "stage": ""},
    VERIFY_NODE: {"passed": True, "coverage": "business", "pod": "warranty-service-x-y",
                  "observed_image": "warranty-service:d34db33f0000",
                  "probes": [{"path": "/actuator/health", "status": 200, "ok": True},
                             {"path": "/warranty/claim?orderId=ORD001", "status": 200, "ok": True}],
                  "failed": [], "summary": "2/2 探针通过（业务链路已验）"},
    "recap": {"summary": "已闭环", "root_cause": "fin 空值未校验",
              "actions": ["修复并合并", "构建镜像", "滚动部署", "冒烟通过"], "followups": []},
}


def load() -> Workflow:
    yaml_text = next(w["yaml"] for w in load_workflow_seeds() if w["id"] == SEED_ID)
    return Workflow.load_yaml(yaml_text)


def build(
    outputs: dict | None = None, inputs: dict | None = None, seen: dict | None = None
) -> DAGExecutor:
    """按节点 id 返回脚本化输出；未给出的节点回落到 mock 形态。

    ``seen``：给了就把每个节点**解析后**的 params 记进去（键=节点 id）。断言"下游到底
    收到了什么"只能靠它 —— ``node_states[nid]["output"]`` 是 agent 的**输出**，不是入参。
    """
    table = OK_OUTPUTS if outputs is None else outputs

    async def runner(node, params):
        if seen is not None:
            seen[node.id] = params
        return table.get(node.id, {"node": node.id, "ok": True})

    wf = load()
    return DAGExecutor(
        "run_s2", "otr", wf.dag, InMemoryStateStore(),
        inputs=inputs if inputs is not None else dict(INPUTS), node_runner=runner,
    )


async def run_to_completion(ex: DAGExecutor) -> str:
    """一路批到底（三道门全放行），返回最后一次 `run()` 的结论。"""
    for gate in (PLAN_GATE, COMMIT_GATE, DEPLOY_GATE):
        await ex.run()
        await ex.approve(gate, approved=True, by="lead-engineer")
    return await ex.run()


# ── 结构守卫 ────────────────────────────────────────────────────────────────


def test_workflow_loads_with_exactly_the_expected_nodes() -> None:
    """节点集合就是这条流程的定义本身——多一个少一个都是语义变化。

    ⚠️ 断言**集合相等**而不是包含：漏掉 `verify-deploy`（最后一道关）或
    `ticket-closed`（显式终点）都是"图上少一块"，跑起来不会有任何报错。
    """
    dag = load().dag
    assert set(dag.nodes) == {
        # 诊断链
        "triage", "scope", "logs", "trace", "metrics", "infra", "halt",
        "locate", "know", "rca",
        # 修复段
        PLAN_GATE, "plan", "fix", "test", "review", COMMIT_GATE, "commit",
        # 发布链
        "merge", "ci", DEPLOY_GATE, "deploy", VERIFY_NODE,
        # 收尾
        "ticket-closed", "recap",
    }
    assert dag.nodes[PLAN_GATE].is_approval
    assert dag.nodes[COMMIT_GATE].is_approval
    assert dag.nodes[DEPLOY_GATE].is_approval
    assert dag.nodes["halt"].is_halt
    assert dag.nodes["ticket-closed"].is_closed


def test_the_release_chain_is_linear_and_the_tail_moved() -> None:
    """`commit → merge → ci → approve-deploy → deploy → verify-deploy → ticket-closed`。

    ⚠️ 这条是**发布链的骨架**：`commit` 必须**不再**直连 `ticket-closed`
    （那是加链之前的旧形状，改回去不会有任何报错，只会让收尾重新变成"PR 一开就宣布完工"）。
    全线性 ⇒ 每个节点只有一条入边 ⇒ **不需要** `join: all` / `required_edges`。
    """
    dag = load().dag
    assert [(e.source, e.target) for e in dag.edges if e.source in {"commit", *RELEASE_CHAIN}] == [
        ("commit", "merge"),
        ("merge", "ci"),
        ("ci", DEPLOY_GATE),
        (DEPLOY_GATE, "deploy"),
        ("deploy", VERIFY_NODE),
        (VERIFY_NODE, "ticket-closed"),
    ]
    # 每个发布节点只有一条入边与一条出边——join 语义不需要显式声明，
    # 且"只有一条出边"本身就说明**没有退路**（见 VERDICT_FIELDS 那条用例）。
    for nid in [*RELEASE_CHAIN, "ticket-closed"]:
        assert len(dag.nodes[nid].in_edges) == 1, f"{nid} 不止一条入边——join 语义需要显式声明"
        assert len([e for e in dag.edges if e.source == nid]) == 1, (
            f"{nid} 不止一条出边——那意味着图上有退路"
        )

    # 条件边：只有"成功"那一侧，没有"失败 → 收口"那一侧（见 VERDICT 那条用例）
    wheres = {e.source: e.when for e in dag.edges if e.source in RELEASE_CHAIN}
    assert wheres == {
        "ci": "$.nodes.ci.output.built == true",
        DEPLOY_GATE: f"$.nodes.{DEPLOY_GATE}.output.approved == true",
        "deploy": "$.nodes.deploy.output.deployed == true",
        VERIFY_NODE: f"$.nodes.{VERIFY_NODE}.output.passed == true",
        "merge": None,  # 合并没有条件：commit 成功就该合
    }


def test_all_three_gates_declare_on_reject_explicitly() -> None:
    """三道门都**显式**写 `on_reject`，且判据是「这道门之前，不可逆的事发生了没有」。

    | 门 | 之前的不可逆动作 | `on_reject` | 驳回边 |
    |---|---|---|---|
    | `approve-plan` | 无 | `abort` | 没有（abort 下不可达，写了是骗人） |
    | `approve-commit` | 只改了工作区（可丢弃） | `continue` | 有 → `recap` |
    | `approve-deploy` | **PR 已合、主干已动** | `abort` | 没有 |

    ⚠️ 默认值是 `abort` ⇒ 少写一个 `continue` 不会报错，只会把**人的否决**报成
    run `failed`（"执行出错"的语义）。而 `approve-deploy` 用 `continue` 的代价更重：
    run 会**判 done（绿）**，而工单永远停在"处理中"、也没有任何补偿。
    """
    dag = load().dag
    assert dag.nodes[PLAN_GATE].on_reject == "abort"
    assert dag.nodes[COMMIT_GATE].on_reject == "continue"
    assert dag.nodes[DEPLOY_GATE].on_reject == "abort"

    assert [(e.target, e.when) for e in dag.edges if e.source == PLAN_GATE] == [
        ("fix", f"$.nodes.{PLAN_GATE}.output.approved == true")
    ]
    assert sorted((e.target, e.when) for e in dag.edges if e.source == COMMIT_GATE) == sorted([
        ("commit", f"$.nodes.{COMMIT_GATE}.output.approved == true"),
        ("recap", f"$.nodes.{COMMIT_GATE}.output.approved == false"),
    ])
    # 发布门：abort + 刻意**没有**驳回边。加载期只查反方向（abort + 驳回边 = 直接报错），
    # 所以"该有的驳回边被误加了"由加载器拦、"本来就不该有"只能靠这条钉住。
    assert [(e.target, e.when) for e in dag.edges if e.source == DEPLOY_GATE] == [
        ("deploy", f"$.nodes.{DEPLOY_GATE}.output.approved == true")
    ]


def test_deploy_gate_metadata_is_raw_and_the_card_reads_upstream_output() -> None:
    """审批节点的 params **从不解析** —— 这条锁的是"卡片上的值从哪来"。

    `_process_approvals` 直接读 `node.params`（不做 `resolve_params`），所以：
      · `timeout` 必须是**字面量**（`dag.py` 建图时把 YAML 顶层的 `timeout:` 折成 int
        放进 params；写成 `$.` 引用会得到一个字符串，而下游是 `int(...)`）；
      · 而 `image_tag` / `ci` / `merge` 这些 `$.` 项，入库存的就是**表达式本身**。
    卡片上的实际数据走的是另一条路：`api/app.py` 的 `upstream_out` —— 取**上游节点的
    output**。所以"审批人看得到 image_tag"这件事，靠的是 `ci` 输出里有它，
    **不是**靠这里的 params。
    """
    node = load().dag.nodes[DEPLOY_GATE]
    assert isinstance(node.params["timeout"], int), node.params["timeout"]
    assert node.params["timeout"] == 7200
    assert node.params["approvers"] == ["lead-engineer"]
    # 这三项刻意保留原样的 `$.` 表达式 —— 它们只声明"这张门要看什么"，
    # 不是给人看的值。断言它仍是原样，是为了防止有人误以为它会被解析。
    assert node.params["image_tag"] == "$.nodes.ci.output.image_tag"
    assert node.params["ci"] == "$.nodes.ci.output"
    assert node.params["merge"] == "$.nodes.merge.output"


@pytest.mark.parametrize(("nid", "required"), [
    ("merge", {"service", "pr_url"}),
    ("ci", {"service", "merge_commit"}),
    ("deploy", {"service", "image_tag"}),
    (VERIFY_NODE, {"service", "image_tag"}),
])
def test_the_chain_fails_fast_without_its_scalar_anchor(nid: str, required: set[str]) -> None:
    """每个发布节点的标量锚点都是 `require` 硬前置 —— 缺了直接 fail-fast。

    没有 `require` 时，缺键只让 params 解析成 None：`ws_merge_pr` 会拿到一个空
    `pr_url` 去"猜一个要合的 PR"（而 main 上堆着多个历史 run 留下的未合并 PR，
    猜错就是合了别人的分支）；`ws_rollout` 会拿空 tag 去滚。
    """
    node = load().dag.nodes[nid]
    assert set(node.require) == required, f"{nid} 的 require 不是 {sorted(required)}"
    assert node.on_failure == "abort", f"{nid} 失败必须中止——它下游是收尾"


@pytest.mark.parametrize("nid", [n for n in RELEASE_CHAIN if n != DEPLOY_GATE])
def test_every_release_node_takes_the_service_from_locate_not_the_ticket(nid: str) -> None:
    """发布链里要动工作区/集群的 4 个节点，`service` **只有一个来源**：`locate` 的输出。

    （`approve-deploy` 不在参数里：审批节点的 params **从不解析**，它没有 `service`
    这个入参——它批的是 `ci` 的产物，不自己碰服务。）

    ⚠️ 这是本图与 `problem-diagnose-fix` 那份**刻意不同**的地方，也是最容易抄错的
    一处：那份没有诊断链，工单的 `cmdb_ci.name` 是唯一来源；本图的工单**不保证**
    有 `cmdb_ci`，服务名是 `code-locator` 定出来的。
    抄错的形态：`$.inputs.bug_report.cmdb_ci.name` 恒解析成 None ⇒ 5 个节点全线
    fail-fast（响亮，但错得离原因很远）；或者更糟——单测的 INPUTS 若也照抄一份
    `cmdb_ci`，两边一起错、**测试照样绿**。
    """
    node = load().dag.nodes[nid]
    assert node.params.get("service") == "$.nodes.locate.output.service", (
        f"{nid} 的 service 不是取自 locate —— 抄了 problem-diagnose-fix 的写法？"
    )
    # 与它自己的修复段保持同一个真源（`fix` / `test` / `commit` 早就这么写了，
    # 发布链没有理由另立一个）
    assert node.params["service"] == load().dag.nodes["fix"].params["service"]


def test_verify_deploy_takes_the_business_probe_from_plan_and_walks_the_nested_path() -> None:
    """业务探针**整条**从 `plan` 来，且路径必须是 `output.plan.verification_probe`（**两层**）。

    ⚠️ 为什么值一条**字符串相等**的断言（而不是只靠种子那张通用网）：
    路径写错**不会报错**，只会恒解析成 None ⇒ 冒烟静默退回"只探健康层" ⇒
    `coverage: health_only` ⇒ run 照样绿。而**两种写错法的网不一样**（实测过）：

    | 写错法 | `test_seed_defaults` 的字段核对 | 本用例 |
    |---|---|---|
    | 少一层：`output.verification_probe` | ✅ 抓得到（首段变成 `verification_probe`，不在 schema 里） | ✅ |
    | 叶子拼错：`plan.verificationProbe` | ❌ **放行**（首段仍是 `plan`，在 schema 里） | ✅ |

    那张通用网只看路径的**第一段**，所以本用例是第二行**唯一的网** ——
    而"写对了层级、写错了叶子"恰恰是最容易发生的一种（复制粘贴后改一半）。

    另一半：探针**不由人配**（没有 `AGENTFLOW_DEPLOY_TARGETS` 那种第二个真源）——
    健康层从 Deployment 自己声明的探针读，业务层从 plan 读。
    """
    node = load().dag.nodes[VERIFY_NODE]
    assert node.params["probe"] == "$.nodes.plan.output.plan.verification_probe"
    # 同一条链上的另两个取数：也必须是"上游输出"，不是字面量
    assert node.params["image_tag"] == "$.nodes.ci.output.image_tag"
    assert node.params["deploy"] == "$.nodes.deploy.output"


@pytest.mark.parametrize(("nid", "agent", "field"), [
    (nid, *pair) for nid, pair in sorted(VERDICT.items())
])
def test_release_verdicts_are_failures_not_edges(nid: str, agent: str, field: str) -> None:
    """`merged` / `built` / `deployed` / `passed` 为 `false` ⇒ **节点判红**，不走边。

    判定在 **executor 层**（`VERDICT_FIELDS`），不在图的 `when` 上：失败节点的出边
    **恒失活**，所以任何"`xxx == false → recap`"的退路边都**永远不可达**——
    留着不报错，只是让读图的人以为"没部署成功还能走到复盘收口"。
    代价如实记：这条链上任何一环失败 ⇒ run 判 failed、**没有复盘**。

    没有这条映射的形态是"看着成功"：`deployer` 输出 `{"deployed": false}` 而节点是
    DONE（绿），下游继续去冒烟、去宣布"处理流程走完了"。
    """
    from agentflow.executor.dag_executor import VERDICT_FIELDS

    dag = load().dag
    # ⚠️ 判据是 **agent 名**（`merger`），不是节点 id（`merge`）。
    # 本图里两者刻意不同名，按节点 id 查会恒得到 None —— 而"查不到"看起来就像"不用覆盖"。
    assert dag.nodes[nid].agent == agent
    assert VERDICT_FIELDS.get(agent) == field, (
        f"agent {agent!r}（节点 {nid}）的结论字段 {field!r} 不在 VERDICT_FIELDS 里 —— "
        "`false` 会被当成 DONE（绿），下游继续往下走"
    )
    # 唯一的出边是"成功那一侧"，没有任何回收到 recap 的边
    assert not [e for e in dag.edges if e.source == nid and e.target == "recap"], (
        f"{nid} 有一条 → recap 的退路——它在 VERDICT_FIELDS 落地后永远不可达"
    )


def test_smoke_tester_is_read_only_and_does_not_join_the_side_effect_list() -> None:
    """`smoke-tester` **不进** `SIDE_EFFECT_AGENTS`——它只发 HTTP GET。

    进了的代价：它会被接上 `run_id:node_id` 的幂等缓存，**重跑直接返回缓存输出**，
    而那时 pod 可能已经换过了 —— 缓存让节点报成功。
    （同族：`ci-builder` 也不进，理由见该清单上的注释。）
    `merger` / `deployer` 则**必须**在里面：一个动主干、一个滚 Deployment，都不可逆。
    """
    from agentflow.executor.dag_executor import SIDE_EFFECT_AGENTS

    assert "smoke-tester" not in SIDE_EFFECT_AGENTS
    assert "ci-builder" not in SIDE_EFFECT_AGENTS
    assert {"merger", "deployer"} <= SIDE_EFFECT_AGENTS


def test_recap_carries_every_release_artefact() -> None:
    """复盘的入参里有**发布链每一环的整个输出**——各传整份而不是某个字段。

    有值 ⇒ 走到过；`null` ⇒ 这条路径没走到（被审批驳回）。两种含义下游都读得出来，
    比一个恒空的具体字段诚实（本仓踩过：`$.nodes.commit.status` 恒为 null，20+ 条 run 全是）。
    `deployed` 只说明滚上去了，**不说明它跑得起来** —— 所以 `verify` 那条不能省。
    """
    node = load().dag.nodes["recap"]
    assert node.params["merge"] == "$.nodes.merge.output"
    assert node.params["ci"] == "$.nodes.ci.output"
    assert node.params["deploy"] == "$.nodes.deploy.output"
    assert node.params["verify"] == f"$.nodes.{VERIFY_NODE}.output"
    assert node.params["commit"] == "$.nodes.commit.output"


def test_workspace_gets_prepared_for_the_repair_chain() -> None:
    """图里至少有一个节点会触发工作区准备（否则每个 ws_* 工具都会早退）。

    `WORKSPACE_AGENTS` 少一个 agent 的后果不是报错，是"那个节点什么都没做但红了/绿了"。
    """
    from agentflow.service import WORKSPACE_AGENTS

    agents = {n.agent for n in load().dag.nodes.values()}
    assert agents & WORKSPACE_AGENTS, f"没有任何节点会触发工作区准备（WORKSPACE_AGENTS={sorted(WORKSPACE_AGENTS)}）"
    # 发布链里要碰工作区的那三个必须在（判据是"用不用工作区"，不是"写不写代码"）
    assert {"merger", "ci-builder"} <= WORKSPACE_AGENTS


# ── 行为：三道门 + 发布链 ────────────────────────────────────────────────────


async def test_happy_path_parks_at_three_gates_and_converges() -> None:
    """计划门 → 提交门 → **发布门** → 收尾 → 复盘，全程无中断。

    「**三次**停放」是这条流程的形状，三道门各压在一类动作之前：

    | 门 | 压住的动作 | 审批人看什么 |
    |---|---|---|
    | `approve-plan` | 改代码 | 计划 |
    | `approve-commit` | 推 PR **并合并到主干** | diff + 测试证据 |
    | `approve-deploy` | 交付（动集群） | 构建产物（`image_tag` + 构建日志） |

    任一处不批，下游都不该跑。
    """
    seen: dict = {}
    ex = build(seen=seen)
    assert await ex.run() == "waiting_approval"
    assert ex.pending_approvals() == [PLAN_GATE]
    assert ex.get_status("fix") == "pending"  # 计划没批之前，一行代码都不许改

    await ex.approve(PLAN_GATE, approved=True, by="lead-engineer")
    assert await ex.run() == "waiting_approval"
    assert ex.pending_approvals() == [COMMIT_GATE]
    for nid in ("fix", "test", "review"):
        assert ex.get_status(nid) == DONE, nid
    assert ex.get_status("commit") == "pending"  # 没批就不推 PR

    await ex.approve(COMMIT_GATE, approved=True, by="lead-engineer")
    assert await ex.run() == "waiting_approval"
    assert ex.pending_approvals() == [DEPLOY_GATE]
    # 走到发布门时，产物已经定型（这正是"批的是这个镜像"的前提）
    for nid in ("commit", "merge", "ci"):
        assert ex.get_status(nid) == DONE, nid
    assert ex.get_status("deploy") == "pending"  # 没批就不碰集群

    await ex.approve(DEPLOY_GATE, approved=True, by="lead-engineer")
    assert await ex.run() == "done"
    # 批完发布门之后**还要自己再跑一步**：`deploy → verify-deploy → ticket-closed → recap`
    for nid in ("deploy", VERIFY_NODE, "ticket-closed", "recap"):
        assert ex.get_status(nid) == DONE, nid
    assert ex.halt_triggered() is False

    # ── "下游到底收到了什么"（`seen` 是解析后的 params，不是 agent 输出）──
    # merge 真的收到了 commit 的 PR（不是 None，也不是空串）
    assert seen["merge"]["pr_url"].endswith("/pull/42")
    # ci 拿的是**主干那个合并提交**，不是工作区 HEAD —— tag 由它算
    assert seen["ci"]["merge_commit"] == "d34db33f"
    # ⭐ 业务探针**真的从 plan 传到了 verify-deploy**（不是 None，也不是空壳）。
    # 这条是"plan 产出 → 冒烟用上"在单测里唯一的锚点：路径写少一层、或 fixture 写成平铺，
    # 它都会拿到 None，而那**不会报错**（工具只探健康层、run 照样绿）。
    assert seen[VERIFY_NODE]["probe"] == PROBE_FIXTURE
    # 发布链沿途的每一环都收到了**同一个** image_tag（"部署 A、验证 B"在构造上不可能）
    assert seen["deploy"]["image_tag"] == seen[VERIFY_NODE]["image_tag"] == "warranty-service:d34db33f0000"
    # 复盘拿得到全线产物：能说出"进没进主干 / 能不能发 / 上没上 / 验没验"
    assert seen["recap"]["merge"]["merge_commit"] == "d34db33f"
    assert seen["recap"]["deploy"]["deployed"] is True
    assert seen["recap"]["verify"]["coverage"] == "business"
    # ⚠️ 审批节点**不走 runner**（`_process_approvals` 直接置 WAITING_APPROVAL），
    # 所以脚本化 runner 的 `seen` 里**不会有它** —— 它同时也是"审批 params 从不解析"的根因。
    assert DEPLOY_GATE not in seen
    # `ticket-closed` 是 `kind: closed`：**确定性节点，不调 LLM、不走 runner**（同 halt）。
    assert "ticket-closed" not in seen


async def test_smoke_failure_blocks_the_close_out() -> None:
    """冒烟不过 ⇒ 节点判红 ⇒ run failed ⇒ **收尾不发生**。

    判红发生在 **executor 层**（`passed` 在 `VERDICT_FIELDS` 里），不在图的 `when` 上：
    所以"没验过照样宣布完工"在这里不可能出现——失败节点的出边**恒失活**，
    `ticket-closed` 与 `recap` 连一次都不会跑。这也正是这个节点存在的理由：
    `deploy` 只证明滚上去了，**它跑不跑得起来**没人问过。
    """
    outputs = {
        **OK_OUTPUTS,
        VERIFY_NODE: {
            "passed": False, "coverage": "business", "pod": "warranty-service-x-y",
            "probes": [{"path": "/actuator/health", "status": 200, "ok": True},
                       {"path": "/warranty/claim?orderId=ORD001", "status": 500, "ok": False,
                        "why": "**这条路径还是坏的**：返回故障态的 500"}],
            "failed": [{"path": "/warranty/claim?orderId=ORD001", "status": 500}],
            "summary": "1/2 探针通过",
        },
    }
    seen: dict = {}
    ex = build(outputs=outputs, seen=seen)
    with pytest.raises(WorkflowNodeFailed) as ei:
        await run_to_completion(ex)
    assert VERIFY_NODE in str(ei.value)
    assert ex.get_status(VERIFY_NODE) == "failed"
    # 判据是"复盘的 agent 一次都没被调用"，不看状态名 —— 下游是 pending 还是 skipped
    # 是 executor 的实现细节（失败中止 vs 驳回中止不一样），而"有没有宣布完工"才是要害。
    assert "recap" not in seen, "冒烟没过却走到了复盘 —— 收尾不该发生"


async def test_deploy_failure_stops_before_the_smoke_test() -> None:
    """`deployed: false` ⇒ 节点判红 ⇒ **冒烟压根不跑**（打一个没滚上去的服务毫无意义）。

    同族判据：`VERDICT_FIELDS` 让"跑完了但结论是否"变成节点失败，而不是一个下游照常继续的
    普通输出。少了这条映射，`deploy` 报 `deployed: false` 而节点是 DONE（绿），
    冒烟会去打**旧 pod** 并可能返回 200 —— 那 200 什么都没证明。
    """
    outputs = {
        **OK_OUTPUTS,
        "deploy": {"deployed": False, "image_tag": "warranty-service:d34db33f0000",
                   "observed_image": "", "stage": "wait_for_available",
                   "summary": "滚动超时：pod 未就绪"},
    }
    seen: dict = {}
    ex = build(outputs=outputs, seen=seen)
    with pytest.raises(WorkflowNodeFailed) as ei:
        await run_to_completion(ex)
    assert "deploy" in str(ei.value)
    assert ex.get_status("deploy") == "failed"
    assert VERIFY_NODE not in seen, "没滚上去却打了冒烟 —— 打的是旧 pod，200 什么都不能证明"
    assert "recap" not in seen


async def test_merge_failure_stops_the_chain_at_the_first_irreversible_step() -> None:
    """`merged: false` ⇒ 节点判红，构建/部署/冒烟全不跑。

    `merge` 是本图第一个**动了就回不去**的节点。合不上却继续去构建、去部署，
    等于把一个**根本没进主干**的东西往线上推。
    """
    outputs = {**OK_OUTPUTS, "merge": {"merged": False, "already_merged": False,
                                       "pr_url": "", "merge_commit": "", "summary": "分支落后主干，拒绝合并"}}
    seen: dict = {}
    ex = build(outputs=outputs, seen=seen)
    with pytest.raises(WorkflowNodeFailed) as ei:
        await run_to_completion(ex)
    assert "merge" in str(ei.value)
    assert ex.get_status("merge") == "failed"
    for nid in ("ci", "deploy", VERIFY_NODE):
        assert nid not in seen, f"{nid} 在没合进主干的情况下跑了"


async def test_commit_gate_reject_converges_without_merging_or_deploying() -> None:
    """提交门驳回 ⇒ 沿驳回边到 `recap`，**run 判 done**（人否决不是执行出错）。

    判据是"发布链一个节点都没跑"，不看状态名 —— 驳回走的是 `continue`，
    未跑的节点是 SKIPPED（不是 pending），但那与"有没有去合主干"无关。
    """
    seen: dict = {}
    ex = build(seen=seen)
    await ex.run()
    await ex.approve(PLAN_GATE, approved=True, by="lead-engineer")
    await ex.run()
    await ex.approve(COMMIT_GATE, approved=False, by="lead-engineer", comment="证据不足")
    assert await ex.run() == "done"

    for nid in ("commit", *RELEASE_CHAIN):
        assert ex.get_status(nid) == SKIPPED, f"{nid} 在驳回之后还跑了"
        assert nid not in seen, nid
    assert ex.get_status("recap") == DONE
    assert ex.rejected_abort_node() is None  # `continue` 不是中止语义


async def test_deploy_gate_reject_aborts_instead_of_greening_a_undelivered_run() -> None:
    """发布门驳回 ⇒ `abort`，run 判 **failed**，集群上什么都没发生。

    ⚠️ 为什么这里不是 `continue`：走到这道门时 **PR 已合、主干已动**。若用
    `continue` + 驳回边 → `recap`，run 会判 **done（绿）** 而工单永远停在"处理中"
    （`ticket-closed` 是 SKIPPED）、没有任何补偿 —— 看板会把一条没交付的 run 算成闭环。
    那正是本仓反复写的「绿着一条没交付的 run 比红着更危险」。
    """
    seen: dict = {}
    ex = build(seen=seen)
    await ex.run()
    await ex.approve(PLAN_GATE, approved=True, by="lead-engineer")
    await ex.run()
    await ex.approve(COMMIT_GATE, approved=True, by="lead-engineer")
    await ex.run()
    await ex.approve(DEPLOY_GATE, approved=False, by="lead-engineer", comment="不发布")

    with pytest.raises(WorkflowNodeFailed) as ei:
        await ex.run()
    assert "人工决策" in str(ei.value), "驳回被报成了执行出错——两者的收口方式不同"
    assert ex.get_status(DEPLOY_GATE) == "rejected"
    # abort 语义：由节点状态 + 图上的 on_reject 声明判定，不看动作（queue 模式下
    # Worker 经 from_checkpoint 重建后直接 run()、不经过 approve()）
    assert ex.rejected_abort_node() == DEPLOY_GATE
    for nid in ("deploy", VERIFY_NODE, "ticket-closed", "recap"):
        assert ex.get_status(nid) == SKIPPED, nid
        assert nid not in seen, nid
