# merge 节点实现计划（发布链第一步）

> **谁在什么时刻读它**：实施者（动手前通读）+ 评审者（看 §1.3 幂等与 §5 边改动时）。
> 实现完成、结论沉进 `docs/constraints/09.7-pr-gh.md` 之后，本文即可删除。
>
> 完整五节点的设计在 `RELEASE_CHAIN_PLAN_zh-CN.md`。**本文只覆盖 `merge` 一个节点。**

---

## 0. 为什么先做 merge，以及做完是什么样

merge 是五步里**唯一不可逆、且守卫最多**的一步。单独插进去**不会留下半截图** ——
它直接接回 `ticket-done`，端到端能跑完、能验证、能回滚。剩下四个节点以后再往
`merge` 与 `ticket-done` 之间插，届时只改边与 params。

```
现在:   commit → ticket-done → recap
本轮:   commit → merge → ticket-done → recap
以后:   commit → merge → ci → approve-deploy → deploy → verify-deploy → ticket-done → recap
```

**完成判据**

- [ ] 图变成 `commit → merge → ticket-done → recap`
- [ ] `ws_merge_pr` 的**六条守卫**与**幂等回落**都有测试，含"回落时不许再调 merge"这条断言
- [ ] 真机对一个**测试 PR** 合成功；**再调一次**返回 `already_merged` 且没有第二次合并
- [ ] `make test` 不退化（当前基线 `0 failed / 503 passed`）、`make lint` 绿

---

## 1. 新增 `agentflow/agents/release_tools.py`

放 `ws_merge_pr` 的实现与注册元数据。**复用 `workspace_tools` 的既有件，不另写一份**：

```python
from .workspace_tools import WorkspaceToolError, _resolve_repo, _require_github_origin, _run
```

### 1.1 先做一处小重构（改 `workspace_tools.py`）

`ws_open_pr` 现在把"origin 必须是 GitHub 远端"那段**内联在自己里面**（`workspace_tools.py:471`）。
抽成 `_require_github_origin(repo) -> str`（返回 origin URL，不合格就 raise），
供 `ws_open_pr` 与 `ws_merge_pr` **共用**。

> 依据：本仓为"两份实现必然漂移"付过代价（两份 `_mark_cancelled`，CLAUDE.md §11）。
> 这次重构**有现成的安全网**：`tests/test_workspace.py:751`
> `test_ws_open_pr_refuses_non_github_origin` 就是守它的 —— 重构后它必须照样绿。

### 1.2 `ws_merge_pr(service: str, pr_url: str) -> dict`

入参只有两个。`pr_url` 由工作流从 `$.nodes.commit.output.pr_url` 喂进来 ——
**图上有现成的 PR，工具不该装作看不见去自己反查分支**。

校验顺序固定，每条在自己的位置 raise：

| # | 检查 | 失败文案要点 |
|---|---|---|
| 0 | `shutil.which("gh")` | 「本机没有 gh」——别让它退化成 `FileNotFoundError` 冒到 runner |
| 1 | 解析 `pr_url` → `(host, owner, repo, number)`；host 必须是 `github.com` | 「commit 给的 pr_url 不是 GitHub PR 链接」 |
| 2 | `_require_github_origin(repo)` 后解析 → `(owner, repo)`；与 1 **必须一致** | 「commit 说 PR 在 A，可我的工作区是 B」 |
| 3 | `gh pr view <n> --json state,headRefName,headRefOid,mergeCommit,mergeStateStatus` | gh 报错原样带出（PR 不存在 / 无权限） |
| 4 | `state == MERGED` → **直接返回** `already_merged=True` | 见 §1.3 |
| 5 | `state == CLOSED` | 「PR 已被关闭，拒绝合并」 |
| 6 | `headRefOid != git rev-parse HEAD` | 「PR 的头是 X，工作区 HEAD 是 Y」 |
| 7 | `headRefName != 当前分支` | 报错里两个都打出来 |
| 8 | `mergeStateStatus == CONFLICTING` | 「主干已动，需人工 rebase」 |
| 9 | `gh api -X PUT /repos/{o}/{r}/pulls/{n}/merge -f merge_method=squash` | gh 报错原样带出 |

第 1–2 与第 6–7 步是关键：**用 commit 给的 url 去定位，但要求它落在我自己的仓库 +
我自己的分支 + 我自己那个 commit 上。** 三者任一不符就停。

> 顺带白拿一个好处：这让 `commit` 那句"我开了 PR"变成**可核对的**，而不只是被信 ——
> 与 §3.3「**声称改了 ≠ 真改了**」同族，判据从"沙箱里有没有那次成功写入"
> 换成"远端那个 PR 的头是不是我这个 commit"。

**用 `gh api` 而不是 `gh pr merge`**：`PUT .../pulls/{n}/merge` 一次调用就返回
`{"merged": true, "sha": "<合并提交>"}` —— 那个 sha 就是后续比树 / 打镜像 tag 要用的。
`gh pr merge` 只打印一段人话，还得再补一次 `gh pr view --json mergeCommit`：
多一次调用、多一个竞态窗口，且那段话要正则解析。

**两个"不要"**：

- **不要 `--delete-branch`**（gh 的 `--delete-branch` 连**本地**工作区分支一起删，
  而 `ws_git` 后面还要在这个分支上 `rev-parse`）。远端分支若被仓库的 auto-delete 设置删掉，
  不影响我们 —— 本地那份不动。
- **`_GIT_ALLOWED` 里不要加 `merge`**：我们合的是远端 PR，不是本地分支。
  留一行注释说明，否则后来人会以为 merge "坏了"。

**合并方式不暴露成参数**，工具内固定 `squash`。让模型选 `--merge/--rebase` 等于让它选
不可逆历史的形状，与"让它挑 base"是同一件事。squash 还有个好处：主干线性。

**返回值**（字段名要与 `AGENT_SCHEMAS["merger"]` 和 YAML 里的 `$.nodes.merge.output.*` 逐字一致）：

```python
{"merged": True, "already_merged": False, "pr_url": ..., "pr_number": n,
 "merge_commit": "<主干上那个 squash 提交>", "merge_method": "squash",
 "head_ref": "<分支名>", "summary": "已合并 #n 到 main"}
```

### 1.3 ⚠️ 幂等回落是**承重的**，不是锦上添花

`execute_with_idempotency` 的缓存只在 **agent 吐出合法 JSON** 时才记成功。
若 agent 已经合成功了、却在收尾时失败（`AgentOutputError`，本仓高频故障），
那条 attempt 记的是 `failed` ⇒ resume 时节点重跑（`FAILED` 不在 `TERMINAL` 里）
⇒ **工具会被真真切切再调一次**。此时 PR 已经是 `MERGED`，
没有第 4 步的回落就会报"PR 找不到" → `on_failure: abort` → **把已经发生的合并报成失败**。

所以测试里必须有一条硬断言：**`state == MERGED` 时 `PUT merge` 一次都不能被调用。**

判据一句话：**这个工具重跑一次，外部世界不该再多一次可见的变化，也不该把一个已经发生的动作报成失败。**

### 1.4 注册元数据

```python
RELEASE_TOOLS = {"ws_merge_pr": ws_merge_pr}

def build_release_tools(agent_name: str) -> list[dict]:
    """照 build_workspace_tools 的形状；read_only=False（它是对外的写动作）。"""
```

---

## 2. 把工具真正接上（**三处，缺一即静默失效**）

1. `agentflow/agents/tools.py` 的 `TOOL_REGISTRY` 加：

   ```python
   "ws_merge_pr": ToolSpec("ws_merge_pr", ["merger"], timeout=120,
                           needs_approval=True, level="L2",
                           description="把本次 run 的 PR 合并到主干（squash）")
   ```

   （gh 调用是秒级，默认 `_SUBPROC_TIMEOUT_SEC = 120` 够用，**本轮不动 `_run` 的签名**。）

2. `agentflow/agents/mcp.py` 的 `_build_function_tools`（:48-80）加一行调 `build_release_tools`。

   ⚠️ **不加这一行，`build_permission_context` 照样发 allow 规则，但模型看不到工具** ——
   症状是烧完轮次报"未输出合法 JSON"，与工具根本不存在一模一样。
   （`build_l2_tools` 是白名单式的，新工具不在这三处任一，就是"有权限、没工具"。）

3. 补一条**不变量测试**：`TOOL_REGISTRY` 里每个 spec 都能被某个 builder 组装出来。
   今天全仓没有这条测试（`grep -rn TOOL_REGISTRY tests/` 无输出）—— 这正是上面那个坑的成因。

---

## 3. 新 agent `merger`

- **`agentflow/agents/registry.py`**：`FIX_AGENTS` 加 `"merger"`；
  `AGENT_STAGES["merger"] = "deliver"`；`AGENT_DESCRIPTIONS["merger"]` 一条；
  文件头 docstring 的 `16-agent 编队` 改成 17。
- **`agentflow/agents/prompts.py`**：
  - `SYSTEM_PROMPTS["merger"]` —— **输出契约要内联在提示词里**
    （本仓事实：`AGENT_SCHEMAS` 不进任何提示词，只是元数据 + 测试锚点）。
    要点：只有一个工具 `ws_merge_pr(service, pr_url)`；**`pr_url` 原样取自入参
    `commit.pr_url`，不许自己改、不许自己编**；`merged` 必须来自工具的真实返回；
    工具报错就把真实原因写进 `summary`，**不要因为"看着该合了"就报 `merged: true`**；
    最后一次回复只输出 JSON。
  - `AGENT_SCHEMAS["merger"]` —— 字段与 §1.2 的返回值逐字一致。
- **`agentflow/service.py:64-69`** 的 `WORKSPACE_AGENTS` 加 `"merger"`（它要碰工作区）。
  ⚠️ 不加不会立刻坏（图里已有 `fix-implementer` / `committer` 兜着早退判断），
  **所以测试也不会红** —— 静默缺口，别漏。
- `_MAX_ITERS` **不调**（默认 10 够，工具只有一个）。

---

## 4. 执行器两个常量（`agentflow/executor/dag_executor.py`）

- `:66` `SIDE_EFFECT_AGENTS` += `"merger"` ⇒ 拿到 `run_id:node_id` 确定性键。
- `:96` `VERDICT_FIELDS` += `"merger": "merged"` ⇒ 工具返回 `merged: false` 而 agent 如实照报时，
  节点判 FAILED。**没有这条映射，节点照样 DONE，下游会继续去处理一个根本没合进去的东西。**
  （参数化用例 `tests/test_executor.py:458` 跑在映射表上，加一条自动多一个用例，不用改测试。）

---

## 5. 工作流 YAML（`agentflow/seed/workflows/problem-diagnose-fix.yaml`）

新增节点，放在 `commit` 与 `ticket-done` 之间：

```yaml
  # ===== 合并（本图第一个不可逆动作）=====
  # 输入取自 commit 的输出：图上有现成的 pr_url，工具不该装作看不见去自己反查分支。
  # 但工具会拿它去核 —— origin / 分支 / HEAD 三者任一不符就停（见 release_tools）。
  # 这是本仓第一处"动了就回不去"的节点，所以 on_failure 必须是 abort：
  # 合不了就不该继续往下走。
  merge:
    agent: merger
    description: "合并：把 commit 开出的那个 PR 合入主干（squash）"
    require: [service, pr_url]
    params:
      commit: "$.nodes.commit.output"
      pr_url: "$.nodes.commit.output.pr_url"
      service: "$.inputs.bug_report.cmdb_ci.name"
    on_failure: abort
```

**边**：删掉 `- { from: commit, to: ticket-done }`，换成

```yaml
  - { from: commit, to: merge }
  - { from: merge, to: ticket-done }
```

**params 增补**：`ticket-done` 与 `recap` 各加一条 `merge: "$.nodes.merge.output"`
（`approve-commit` 驳回那条路上它是 `None`，与既有的 `commit` 同款 ——
下游分得清"没走到"与"图里忘了写"）。

**顺带必须改的一处**：`approve-commit` 的 `name` / `description`。
它现在写的是「审核 diff + 测试证据，通过后才提交 PR」；加了 merge 之后，
**同一次点击的含义变成"同意提交并合并到主干"**。不改的形态是最坏的那种：
**审批人以为只开 PR，实际合了主干，而图上一切正常。**

---

## 6. 测试

### 6.1 `ws_merge_pr` 的单元测试（建议新文件 `tests/test_release_tools.py`）

照 `tests/test_workspace.py` 已有的桩写：

| 现成件 | 位置 | 作用 |
|---|---|---|
| `_FakeProc` | `:613` | gh 调用替身，提供 `communicate()` / `returncode` |
| `_stub_gh` | `:632` | monkeypatch `asyncio.create_subprocess_exec`，**只桩 gh、其余走真的** |
| `_with_origin` | `:667` | fetch URL 长得像 GitHub，push 落本地裸仓库 |
| `exec_context.current_run.set(...)` | `:269` | 工具定位工作区的前提（用完 reset） |

用例（**每条守卫一条**，外加幂等那条）：

| 用例 | 断言 |
|---|---|
| 正常合并 | PUT 被调用；返回 `merged=True`、`merge_commit` 来自响应 |
| `pr_url` 不是 GitHub 链接 | raise，且 `gh` 一次都没被调 |
| origin 与 pr_url 的 owner/repo 不一致 | raise，**没调 PUT** |
| `state == CLOSED` | raise，没调 PUT |
| `headRefOid != 工作区 HEAD` | raise，没调 PUT |
| `mergeStateStatus == CONFLICTING` | raise，没调 PUT |
| **`state == MERGED`** | 返回 `already_merged=True`；**PUT 一次都没被调用** ← §1.3 那条 |
| gh 缺失（打桩 `shutil.which` 返回 None） | raise，文案里含 "gh" |

### 6.2 工作流级（`tests/test_problem_diagnose_fix_workflow.py`）

- `test_workflow_loads_with_exactly_the_expected_nodes`：节点集合加 `"merge"`。
- `OK_OUTPUTS` 加 `"merge": {"merged": True, "already_merged": False, ...}`。
- `test_ticket_done_sits_on_the_success_path_only`：入边断言从 `["commit"]` 改成 `["merge"]`。
- 新增：`merge` 的 `require == {"service", "pr_url"}`，且 `pr_url` 指向
  `$.nodes.commit.output.pr_url`（照 `test_plan_takes_the_diagnosis_from_the_ticket_...`
  的写法）—— **缺 pr_url 时 fail-fast，而不是把 None 喂给工具**。
- 新增：`approve-commit` 驳回路径上 `recap` 收到的 `merge` 是 `None`
  （照既有的 `commit is None` 那条）。
- 既有"停放两次"的断言**不用改**（本轮没加门）。

### 6.3 变异验证（要附"变异真的生效"的证据：`grep -c` 归零 / diff）

- 去掉 `require` 里的 `pr_url` → 6.2 的 fail-fast 用例必须红。
- 把 params 里 `pr_url` 的路径写错一个字段名 →
  `test_seed_params_reference_real_schema_fields` 必须红（假失败不算数，它得真红）。
- 注释掉 `state == MERGED` 那条回落 → 6.1 的幂等用例必须红。

---

## 7. 上线与传播（不做会静默跑旧东西）

1. **重启 API** —— inline 模式下（`.env` 没配 `AGENTFLOW_RUN_MODE`）执行体就是 API 进程；
   prompt / schema / 工具注册全在代码里，**不重启 = 跑旧的**。
2. `DRY=1 make sync-workflows TENANT=otr` 看清楚，再 `make sync-workflows TENANT=otr`
   （workflow 的真源是库，§6.0）。
   ⚠️ `default` / `local` 两个库里存的是**旧变体**（顶层 `inputs.rca`，实测）。
   `TENANT=otr` **不会碰它们**（脚本只写该租户的库）；但它们将来对齐时是一次
   **输入契约的变更**，调用方也得跟着改 —— 不是一次例行同步。
3. **不需要 `sync-agents`**：`ticket-done` 的提示词本轮不动（merge 在成功路径上，
   它的判据仍是"commit 有 pr_url"），`merger` 是内置 agent、不走绑定。

---

## 8. 真机验证（先不烧 LLM token）

1. 临时脚本里置位 `exec_context.current_run.set("<run_id>")` + `current_tenant.set("<tenant>")`
   —— **不置位会撞 `_resolve_repo` 的那句守卫，验的就只是那句守卫**。
2. ⚠️ **它会真的合并一个 PR。** 先开一个**一次性的测试 PR**
   （例如往 README 加一行无意义改动 + 空提交），**不要拿历史 PR #4–#11 试**，那些是真实修复。
3. 断言：`gh pr view <n> --json state` 变成 `MERGED`；返回值里的 `merge_commit`
   能在 `gh api repos/xqfgbc/aiops-test-order-service/commits/<sha>` 查到。
4. **再调一次** —— 必须返回 `already_merged=True`，且 GitHub 上 `mergedAt` 没变
   （没有第二次合并）。这是 §1.3 那条回落的真机证据。
5. `make test` 全绿 + `make lint` 绿。
   `.importlinter` 契约：新增的 `agents/release_tools.py` 只能往下 import
   （`..config` 这一类），**不得 import service / worker**。

> 一个前置：**工作区的 origin 必须是 https 形态**。本机现存的老工作区
> （如 `~/agentflow-workspace/local/run_5d06478155/repos/order-service`）origin 还是
> `file:///…`（`.env.dev` 时代建的），merge 会**直接拒**（§1.2 第 2 条）。
> 用一个新 run 新建的工作区；只做 §8 的干跑则手工准备一个 https origin 的临时仓库即可。

---

## 9. 本轮明确**不做**（划清楚，免得顺手扩）

- `ci` / `approve-deploy` / `deploy` / `verify-deploy` 四个节点；
- `AGENTFLOW_BUILD_CMDS` / `AGENTFLOW_DEPLOY_TARGETS` 两个配置；
- `_run` 的 `timeout` 形参（等 deploy 那步再改）；
- **合并之后没有补偿路径**这件事（构建/部署失败时主干上已经躺着改动了，平台没有 revert、
  工单也收不到回音）—— 记在 `RELEASE_CHAIN_PLAN_zh-CN.md` D3.1 末尾，不在本轮解决；
- `docs/design-v5.8.md` §4.15 的补记（等五个节点齐了一并写）。
