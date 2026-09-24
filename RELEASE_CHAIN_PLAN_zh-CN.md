# 发布链实施计划：在 `problem-diagnose-fix` 的 commit 之后加 5 个节点

> **谁在什么时刻读它**：实施者（动手前通读）+ 评审者（拍板 D2 / D5.1 / D6 三个取舍时读）。
> 实施完成后它的结论应沉进 `docs/design-v5.8.md` §4.15 与 `docs/constraints/`，本文即可删除。

---

## 1. 目标形状

现在的成功路径是 `commit → ticket-done → recap`：**PR 一开出来就回传工单「已解决」**，
没有任何一步证明这次修复真的能构建、能部署、能跑起来。

本次在 `commit` 与 `ticket-done` 之间插一条发布链：

```
commit → merge → ci → approve-deploy → deploy → verify-deploy → ticket-done → recap
                       └── reject ────→ ?（见 D5.1，建议 abort）
```

| # | 节点 | kind | agent | 干什么 |
|---|---|---|---|---|
| 1 | `merge` | agent | `merger` | 把本次 run 的 PR 合并到主干（`gh pr merge --squash`） |
| 2 | `ci` | agent | `ci-builder` | 沙箱里编译打包出 jar，产出内容寻址的镜像 tag |
| 3 | `approve-deploy` | **approval** | — | 人工门：看构建证据，决定要不要发布 |
| 4 | `deploy` | agent | `deployer` | 宿主建镜像 → load 进 minikube → 滚动 Deployment → 等就绪 |
| 5 | `verify-deploy` | agent | `smoke-tester` | HTTP 冒烟：探针 + 业务接口 |

> ⚠️ **本仓没有任何 CI/CD 资产**：无 `.github/workflows`、无构建业务镜像的脚本、
> 无 minikube/kubectl 的 Makefile target（`tenantctl deploy` 只打印一句 kubectl 提示，
> 「实际滚动由 CI/CD 执行」而那个 CI/CD 不存在）。唯一可参照的既有流程是
> `agentflow-testbed/build-and-deploy.sh`。**这条链是本仓的第一套 CI/CD。**
>
> ⚠️ 它同时是本仓**第一条真的会动 git 主干、动镜像、滚 Deployment 的链路**。
> 对照 `docs/TODO.md` §22：到今天为止 `remediate`（K8s 止血）**只产计划、不执行**，
> `commit` 只推分支开 PR。所以下面那些"把不可信代码与不可逆动作压到人工门之后"的取舍
> 不是洁癖 —— 它们是这条链上线前的必要条件。

---

## 2. 已核实的前提（不要假设）

| 事实 | 怎么核实的 |
|---|---|
| 目标仓库是 `xqfgbc/aiops-test-{order,warranty,gateway}-service`，**不是** `xqfgbc/order-service` | `gh repo list xqfgbc`；服务名→仓库名的映射在 `.env` 的 `AGENTFLOW_REPO_MAP` |
| 本账号对该仓库**有 push 权限** | `gh api repos/xqfgbc/aiops-test-order-service --jq .permissions` → `{"push": true, ...}`；且该仓已有 8 个 `aiops/RUN_*` 分支，是历史 run 推上去的 |
| `main` **没有分支保护** | `GET .../branches/main/protection` → 404 ⇒ `gh pr merge --squash` 不需要 review/check，也不需要 `--admin` |
| main 上**堆着 8 个未合并的 PR**（#4–#11） | `gh pr list --repo ... --state all`。含义：合并其中一条会让其余几条冲突 |
| 三个 testbed 服务的 Dockerfile **没有 `RUN` 指令** | 只有 `FROM eclipse-temurin:21-jre` + `COPY build/libs/<svc>-0.0.1-SNAPSHOT.jar`。今天 `docker build` 不执行仓库代码 —— **但这是被构建仓库给的运气，不是平台给的保证** |
| 真实工单**没有 `cmdb_ci.namespace`** | 本机 `otr` 库 7 张工单：手建的带 `{"name","namespace"}`，**APM 建的那条路径只有 `{"name","service"}`**。见 D6 |
| 沙箱 `./gradlew test` 离线可跑 | `docs/constraints/09.6-sandbox.md` 的既有验证。**`clean build` 没验过**，见 §6.3 |
| 本机 docker / minikube / kubectl / gh 都在位 | `minikube status` → Running；`kubectl -n order get pods` → 8 个 Running；`docker info` → podman 6.0.2 |

---

## 3. 设计决定（含"为什么"）

### D1. 用 `kind: agent` + 新建内置 agent，**不新增 node kind**

新 kind 的改动面：`core/dag.py:297` 白名单 + `is_xxx` property + `_exec_node_inner`
（`executor/dag_executor.py:874-951`）分支 + 调度语义（`_process_skips` / `_ready_nodes` /
`_node_decision`）+ **组合根注入**（`worker.py` / `service.py` / `api/app.py` 六处，
照 `TicketCreator` 的形状）。而 agent 节点是本仓既定的扩展路径 —— `committer` 本身就是
「让 agent 真的执行外部动作」的先例。

新 agent 一律做成**内置**（`registry.FIX_AGENTS` + `prompts.SYSTEM_PROMPTS` + `AGENT_SCHEMAS`），
**不做 `seed/agents/*.yaml` 自定义 agent**：自定义 agent 的定义只在种子里，已开通租户要走
`sync-agents`，而那个脚本对**已有的行是不动的**（见 §6.1）。内置 agent 没有这个传播问题。

> **代价如实记**：5 个节点都是 LLM 驱动，每多一个 agent 就多一次
> 「模型没调对工具 → `AgentOutputError` → `on_failure: abort` 中止整条 run」的机会
> （§9.6 记了一串这类 run）。缓解：**每个 agent 只给一个工具**，提示词按 `ticket-done`
> 的形状写死"调一次工具 → 只输出 JSON"。
>
> **评审给过的替代方案**（记录在案，未采纳）：把 5 个压成 2 个（`merge` 复用 `committer`，
> 其余收进一个 `deployer`）。不采纳的理由有两条：① 用户要的就是图上能看见的 5 个节点；
> ② **`approve-deploy` 这道门本身就强制了 `ci` 与 `deploy` 必须是两个节点** ——
> 门不能长在一个节点内部。

### D2. `ci` 只做沙箱编译打包；`docker build` 归到 `deploy`

用户的 CI 定义是「编译、打包」= `./gradlew` 出 jar。把 `docker build` 放进 `deploy`：

1. **不可信代码的执行挪到人工门之后**。`docker build` 执行仓库里的 `Dockerfile`；
   worker 持有 DeepSeek key / DSN / PAT（§9.6：「谁持有密钥，谁不执行不可信代码」）。
   放 `ci` ⇒ 在任何审批之前就执行；放 `deploy` ⇒ 在 `approve-deploy` 之后。
2. jar 与镜像在同一节点内前后脚，少一次跨节点产物交接。

> 今天三个 testbed 服务的 Dockerfile 恰好没有 `RUN`，所以这一步现在是安全的；
> **换个带 `RUN` 的 Dockerfile 就失效** —— 这个保护来自被构建的仓库，不来自平台。
> 记进 `docs/TODO.md` 作为已知接受风险。

### D3. 五个节点各配一个工具，工具只做确定性的事

| 工具 | 做什么 | 关键约束 |
|---|---|---|
| `ws_merge_pr(service, pr_url)` | 用 **commit 给出的 PR** 去合主干 | 见 **D3.1**（唯一不可逆的一步，单列一节） |
| `ws_build_artifact(service)` | 沙箱跑 `AGENTFLOW_BUILD_CMDS[service]`，产出 jar | 命令**只来自部署配置**，不接受调用方传参（照 `test_cmd_for`）；未配置即 raise；产物要按 §6.3 复读 |
| `ws_build_image(service, commit_sha)` | 宿主 `docker build -t <svc>:<sha12> <repo>`，前置校验"树 == 主干上的树"（见 D7） | tag 用**主干合并提交**的 sha12 |
| `ws_rollout(service, image_tag)` | 校验产物存在 → `minikube image load` → `kubectl -n <ns> set image` → `wait --for=condition=available` → **回读 pod 的 `.spec.containers[0].image`** | ns / deployment / container 由部署配置解析（D6）；回读是这道工具里的「**声称改了 ≠ 真改了**」（§3.3）—— 只回 `set image` 的 rc 不证明滚动到了新镜像 |
| `ws_smoke_probe(service, image_tag)` | 按配置的探针路径起临时 `kubectl port-forward pod/<pod>`，然后 HTTP GET（in-cluster 时用 `<svc>.<ns>.svc` 直连） | 路径与期望码由配置定死；**只认正在跑 `image_tag` 的那个 pod**，否则冒烟打的是旧 pod、`200` 什么都没证明；`port-forward` 子进程必须在 `finally` 里 kill |

### D3.1 `merge` 节点：拿 commit 的 PR 去合，但**每一步都核**

**输入来自 `commit` 的输出** —— 图上有现成的 `pr_url` / `pr_number`，
工具装作看不见、自己按分支反查，等于把一条显式的事实换成隐式推断。所以：

```yaml
  merge:
    agent: merger
    description: "合并：把 commit 开出的那个 PR 合入主干"
    require: [service, pr_url]          # 标量锚点：缺了立刻失败，不是静默 None
    params:
      commit: "$.nodes.commit.output"   # 整个输出给 agent 看（PR 号 / base_sha / 摘要）
      pr_url: "$.nodes.commit.output.pr_url"
      service: "$.inputs.bug_report.cmdb_ci.name"
    on_failure: abort
```

「整个 output + 拍平一个标量」这个双写是照 `remediate` 的做法
（`problem-diagnose-fix.yaml:109-112` 的注释：**标量是 `require` 的锚点，整个对象是给 agent 看的上下文**）。

**工具签名**：`ws_merge_pr(service: str, pr_url: str) -> dict`

**校验顺序**（一个错的 url 不能合到别人的 PR 上，但也不能因此就不看这个 url）：

```
1. 解析 pr_url → (host, owner, repo, number)；host 必须是 github.com，否则拒
2. 解析工作区 origin → (owner, repo)；与 1 不一致 ⇒ 拒
   「commit 说 PR 在 A，可我的工作区是 B」
3. gh pr view <number> --json state,headRefName,headRefOid,mergeCommit,mergeStateStatus
4. state == MERGED  → 返回 already_merged（幂等回落，见下）
   state == CLOSED  → 拒（被人关掉了）
   state == OPEN    → 继续
5. headRefOid != 工作区 git rev-parse HEAD  ⇒ 拒
   headRefName != 当前分支                    ⇒ 拒（报错里两个都打出来）
6. mergeStateStatus == CONFLICTING ⇒ 拒（「主干已动，需人工 rebase」）
7. gh api -X PUT /repos/{o}/{r}/pulls/{n}/merge -f merge_method=squash
   → {"merged": true, "sha": "<主干上那个合并提交>"}
```

第 1–2 与第 5 步是关键：**用 commit 给的 url 去定位，但要求它落在我自己的仓库 +
我自己的分支 + 我自己那个 commit 上**。三者任一不符就停。

⟹ 顺带白拿一个好处：这让 `commit` 那句"我开了 PR"变成**可核对的**，而不只是被信。
与 §3.3「**声称改了 ≠ 真改了**」同族，判据从"沙箱里有没有那次成功写入"换成
"远端那个 PR 的头是不是我这个 commit"。

**用 `gh api` 而不是 `gh pr merge`**：`PUT .../pulls/{n}/merge` 一次调用就返回
`{"merged": true, "sha": "<合并提交>"}` —— 那个 sha 就是 D7 里比树、给镜像打 tag 要用的。
`gh pr merge` 只打印一段人话，还得再补一次 `gh pr view --json mergeCommit`：多一次调用、
多一个竞态窗口，且那段话要正则解析。

**两个"不要"**：

- **不要 `--delete-branch`**：它连**本地**工作区分支一起删，而 `ws_git` 后面还要在这个分支上
  `rev-parse`。（远端分支可能被仓库的 auto-delete 设置删掉，那不影响我们，本地那份不动。）
- **`_GIT_ALLOWED` 里不要加 `merge`**：我们合的是远端 PR，不是本地分支。
  留一行注释说明，否则后来人会以为 merge "坏了"。

**合并方式不暴露成参数**，工具内固定 `squash`。让模型选 `--merge/--rebase` 等于让它选
不可逆历史的形状，与"让它挑 base"是同一件事。squash 还有个好处：主干线性，
且那个 squash 提交的**树 == PR 头的树**，D7 的比树判据在常见情形下是精确的。

**幂等两层，第二层才是真正救命的**：

1. `merger` 进 `SIDE_EFFECT_AGENTS` ⇒ `external_operation_id = run_id:node_id`；
   resume 时节点是 `FAILED`（`dag.py:63` 的 `TERMINAL` 不含 FAILED）⇒ 会重跑 ⇒
   `execute_with_idempotency` 命中上次成功记录就直接返回缓存输出。
2. 但缓存**经常命中不了**：`action()` 是整个 runner 调用（agent + JSON 解析）。若 agent
   **已经合成功了**却在最后一步没吐出合法 JSON（`AgentOutputError`，本仓高频故障），
   那条 attempt 记的是 `failed` —— 缓存不命中，**工具会被真真切切重跑一次**。
   所以第 4 步的 `state == MERGED` 回落是**承重的**：没有它，重跑会报"PR 找不到" →
   `on_failure: abort` → **把已经发生的合并报成失败**。

判据一句话：**这个工具重跑一次，外部世界不该再多一次可见的变化，也不该把一个已经发生的动作报成失败。**

**节点姿态**：`on_failure: abort`；**不加 `retry`**（重试解决不了冲突/权限，还要多烧一轮 LLM；
修完 resume 时上面两层兜着）；**没有自己的门**，靠上游 `approve-commit` ——
所以 D5.2 那条必须落实：那张卡片的文案要从「通过后才提交 PR」改成
「通过后才提交 PR **并合并到主干**」。

> **已知未解**：合并是这条链第一个不可逆动作，而它在 `ci` 之前。一旦后面构建或部署失败，
> 主干上已经躺着这次改动了，平台**没有任何补偿路径**（没有 revert 节点、工单也收不到回音）。
> 这是既定顺序带来的固有代价，不是实现问题。收口的最小改法是让 `ci` 的失败路径带一个
> revert 动作 —— 需要再想一层，本轮不做，记在案。

**工具注册有三处，缺一即静默失效**（详见 §6.2）。

### D4. 副作用清单与结论字段

**`SIDE_EFFECT_AGENTS`**（`dag_executor.py:66`，判据是「这个 agent 一旦重跑，外部世界会不会
多一次可见的变化」）：

- `merger` ✅ 进（动主干，不可逆）
- `deployer` ✅ 进（滚 Deployment）
- `ci-builder` ❌ **不进** —— 它只在本地产出一个镜像 tag，没改外部可见状态。
  进了会接上 `run_id:node_id` 缓存，让"同 run 内 ci 重跑"直接返回缓存输出，
  而那时镜像可能已经不在了 —— 缓存让节点报成功。
- `smoke-tester` ❌ 不进（只发 HTTP GET）

**`VERDICT_FIELDS`**（`dag_executor.py:96`）加**三条，不只 smoke-tester**：

```python
VERDICT_FIELDS = {
    "tester": "passed", "reviewer": "approved", "ticket-done": "delivered",
    "ci-builder": "built", "deployer": "deployed", "smoke-tester": "passed",
}
```

理由：`deployer` 的输出如果是 `{"status": "deployed"}`，它**完全可以一次工具都没调就编出来**
（实测形态见 §3.3：fix agent 曾手工编一份 diff 报 done）。判据一句话：**加一个 agent 就是加一条映射**。
`tests/test_executor.py:458` 参数化在映射表上，加一条自动多一个用例，不必改测试。

### D5. `approve-deploy` 的超时写字面量

审批元数据从 **raw params** 读（`_process_approvals`，`dag_executor.py:628-634`；
`dag.py:222-228` 已把 `timeout` 折成 int 进 params）。写 `timeout: 7200`，
**不要**写 `$.` 引用。

审批卡上显示的是**上游节点的 output**（`api/app.py:1374-1387`），不是 params。
⇒ **`ci-builder` 的输出必须带 `image_tag` / 提交 sha / 构建结论**，否则人是在盲批。

### D5.1 ⚠️ 这道门是本仓第一道"压在不可逆动作之后"的门 —— 驳回语义要重新想

`approve-plan` 与 `approve-commit` 都在不可逆动作**之前**，所以它们驳回时
"下游没发生"是成立的，`continue` + 驳回边 → recap 也诚实。

`approve-deploy` **不是**：走到它时 `commit` 已经推了 PR、`merge` 已经把改动合进主干。
此时若用 `on_reject: continue` + 驳回边 → `recap`，结果是：
**run 判 `done`（绿）、工单永远停在"处理中"（ticket-done 是 SKIPPED）、没有任何补偿动作。**
这正是本仓自己反复写的「**绿着一条没交付的 run 比红着更危险**」（§3.2）。

加载期不会拦（`_check_on_reject_consistency` 只查反方向），运行期顺序也对
（`run()` 的不动点循环保证 `recap` 起来时下游已全部 SKIPPED）—— **它是"能跑但语义是绿的谎"**，
比报错难查。

**建议**：照 `approve-plan` 的形态 —— `on_reject: abort` 且**不写驳回边**。
驳回 = 这次发布不做，整条 run 判 `failed`，图上那 4 个未跑的节点全 SKIPPED。
代价：没有 recap（与 `approve-plan` 同款同因）。

> 更好的收口（本次不做，记下来）：驳回边指向 `ticket-done` 并让它报 `failed`
> —— 它的状态枚举里本来就有 `failed`，原系统也就知道"修了但没发"。
> 那需要同时改 `ticket-done` 的提示词（它是自定义 agent，传播见 §6.1）。

### D5.2 ⚠️ merge 没有自己的门 —— `approve-commit` 的语义被这次改动**改掉了**

按既定顺序，`merge` 紧跟 `approve-commit` 且无人拍板。而「合并到主干」比
「推分支 + 开 PR」强得多（不可逆、动 main、且会让其余 8 个在跑的 PR 冲突）。
`approve-commit` 的卡片今天写的是**「审核 diff + 测试证据，通过后才提交 PR」**——
加了 merge 之后，同一次点击的含义变成「同意**合并到主干**」。

⇒ **必须同步改它的 `name` / `description`**。不改的形态是最坏的：
审批人以为只开 PR，实际合了主干，而图上一切正常。

> 与 `docs/TODO.md` §25（`ws_git` 白名单允许 `push` ⇒「建 commit」与「推远端」之间
> 没有权限边界，只有提示词）同族：**权限边界要落在节点/审批上，不能只落在提示词里。**

### D6. `image_tag` 逐节点往下传；namespace **不许**来自工单

`image_tag` 必须逐节点传递、不许各自算：
`ci.image_tag` → `deploy.params.image_tag` → `deploy.output.image_tag` → `verify-deploy.params.image_tag`，
两边都 `require: [service, image_tag]`。让每个 agent 自己推一个 tag，就是给"部署 A、验证 B"留门。

⚠️ **`namespace` 不能取自 `$.inputs.bug_report.cmdb_ci.namespace`** —— 实测工单有两种形态，
而**本流程的真入口（APM 告警 → problem-log-diagnose → 建单）那一种没有 `namespace` 键**。
按它写 `require` 会让新链在真实路径上 100% fail-fast。

⇒ **namespace / 端口 / 探针路径 / deployment 与 container 名，一律来自部署配置**
（`AGENTFLOW_DEPLOY_TARGETS`，由**工具**按 `service` 解析，模型不传参）。
这与 `test_cmd_for` 是同一条判据：「可执行的那组东西在部署时定死，模型只说要做什么」。

> 顺带：这条也把"模型报错命名空间"整个消掉了 —— 工具内部**没有**接受 namespace 入参的路径，
> 于是越界在**构造上**不可能（§7 多租户：「隔离由构造保证」）。
> 这与 `ActionExecutor._check_ns` 是同一姿态；`ws_rollout` 不在它的四个白名单动作里
> （§10.3「新增动作需评审」），所以由 release 层自带同等守卫。
>
> **现存的相关静默缺陷**（本次不顺手改，但要记）：`remediate` 的 `params.namespace`
> 已经写的是 `$.inputs.bug_report.cmdb_ci.namespace`，而它 `on_failure: continue`
> ⇒ APM 建的单永远解析成 None 且**一路无声**。补进 `docs/TODO.md` §22。

### D7. 「部署的东西 == 主干上的东西」只能**比树**，不能比提交

`squash` 合并会在主干上**新建**一个提交，`gh pr view --json headRefOid` 给的是**分支头**
（squash 之后它在主干上是个孤儿）。所以 `headRefOid` 相等只证明"镜像建自 PR 头"，
**不证明"部署的 == 主干上的"**。

正确的判据（本仓约束下唯一可行的）：

```
工作区   git rev-parse HEAD^{tree}
主干     gh api repos/{o}/{r}/commits/{merge_sha} --jq .commit.tree.sha
两者相等 ⇔ 主干上那个提交的内容 == 我这棵树
```

只能走 `gh api`：`_GIT_ALLOWED` 没有 `fetch`（`workspace_tools.py:43`），
`WorkspaceManager` 明确不提供 pull（§8）。取主干那边的对象只能经 gh —— 这正是
`ws_open_pr` 已有的形态（它用 `gh repo view` 取 base）。

**并且镜像 tag 用主干那个合并提交的 sha12**，别用工作区 HEAD 的 sha12：
前者本身即取证，且工单/复盘/审批卡上显示的 tag 与主干一一对应；后者是主干上不存在的提交。

**这条要做成 `ws_build_image` 的前置检查**（不满足就 raise），别让模型自己判断。

---

## 4. 改动清单

### 4.1 新工具模块 `agentflow/agents/release_tools.py`（新文件）

照 `workspace_tools.py` 的形状（工具实现 + 注册元数据）。**必须复用而不是重写**：

- `_run` / `_subprocess_env`（`workspace_tools.py:348,563`）—— §9.6 的挂起保险
  （`GIT_TERMINAL_PROMPT=0` / DEVNULL / 超时 / stderr 分开收）是踩过「节点永久 running」
  才有的，各写一份必然漂移。⚠️ `_run` **当前没有 timeout 形参**，走模块常量
  `_SUBPROC_TIMEOUT_SEC = 120`（:50）—— 而 `minikube image load` / `kubectl wait --timeout=300s`
  都是分钟级。**必须先给 `_run` 加 `timeout` 形参**，否则报错方向完全指错
  （文案会说"多半停在交互式输入上"，实际是正常等待超时）。
- **超时值三处对齐**：函数默认值 / `ToolSpec.timeout` / 子进程墙钟 ——
  小的那个会**静默覆盖**大的（`workspace_tools.py:36-40` 记着这个疤）。
  沙箱侧 `SBX_MAX_EXEC_SECONDS` 默认 300 是硬顶。
- `_truncate`（保留头部）与 `_test_result`（保留尾部）的既有截断规约（:316,:334）——
  gradle 结论在**末尾**，与 tester 同因。
- `_verify_visible_to_worker`（:188-215）的复读思路 —— 见 §6.3。

### 4.2 工具注册（**三处，缺一即静默失效**）

1. `agentflow/agents/tools.py` 的 `TOOL_REGISTRY` 加 5 条 `ToolSpec`，`agents=[...]` 写对应 agent 名。
2. **让它们真被组装成 `FunctionTool`**。`agents/mcp.py:_build_function_tools`（:48-80）只调
   `build_local_tools` / `build_workspace_tools` / `build_l2_tools`，而 `build_l2_tools` 是
   **白名单式**的（只认 sandbox_run_* 与 scale/restart/patch）。新工具若不接进去：
   `build_permission_context` 因为只看注册表**照样发 allow 规则**，但模型**看不到工具** ——
   症状是烧完轮次报"未输出合法 JSON"，与工具根本不存在一模一样。
   二选一：把实现塞进 `WORKSPACE_TOOLS`（`workspace_tools.py:615`），或在 `_build_function_tools`
   里显式加一行 `build_release_tools`。**未接线时照 `_fail_closed_ws_tool` 报错，不静默降级。**
3. **补一条不变量测试**：`TOOL_REGISTRY` 里每个 spec 都能被某个 builder 组装出来。
   今天**没有任何测试守这条**（`grep -rn TOOL_REGISTRY tests/` 无输出）—— 这正是上面那个坑的成因。

> `workspace_tools.py:606` 的 `WORKSPACE_TOOL_AGENTS` **全仓零消费方**，不要往里加。

### 4.3 新 agent（4 个）

- `agentflow/agents/registry.py`：`FIX_AGENTS` 加 `merger` / `ci-builder` / `deployer` / `smoke-tester`；
  `AGENT_STAGES`（deliver ×3、verify ×1）；`AGENT_DESCRIPTIONS` 各一条。
- `agentflow/agents/prompts.py`：`SYSTEM_PROMPTS` + `AGENT_SCHEMAS` 各加 4 条。
  ⚠️ **两个本仓专属事实**：
  1. **`AGENT_SCHEMAS` 不进任何提示词**（全仓只有 registry / agent_config / agent_store 读它）。
     它只是元数据 + 测试锚点。**输出契约只活在 `SYSTEM_PROMPTS` 的内联 JSON 模板里** ——
     所以"加了 schema 就够"是错的。
  2. schema 里的字段名必须与 workflow YAML 里 `$.nodes.<id>.output.<field>` **逐字一致** ——
     对不上不会报错，只会**恒解析成 None**（`tests/test_seed_defaults.py:113` 是这道网）。
     建议字段：`merged/merge_commit/commit_sha`、`built/image_tag/log_tail`、
     `deployed/image_tag/ready`、`passed/probes/failed`。
- `agentflow/agents/runner.py:31` 的 `_MAX_ITERS`：**不调**（默认 10 足够，工具只有一个）。
- `agentflow/service.py:64-69` 的 `WORKSPACE_AGENTS`：`merger` / `ci-builder` / `deployer`
  都要碰工作区，**要加进去**。当前不加不会立刻坏（图里已有 `fix-implementer` / `committer`
  兜着早退判断），所以**守护测试也不会红** —— 是个静默缺口，别漏。

### 4.4 配置（`agentflow/config.py` + `.env` / `.env.example` / `.env.dev`）

- `AGENTFLOW_BUILD_CMDS`（JSON，形状照 `test_cmds`）：
  `{"order-service": "./gradlew clean bootJar --no-daemon -q", ...}`
  （用 `bootJar` 而不是 `build`：Dockerfile 要的就是 `build/libs/<svc>-0.0.1-SNAPSHOT.jar`，
  而 `build` 会**把 tester 刚跑过的测试再跑一遍**，白烧一次 300s 预算。）
- `AGENTFLOW_DEPLOY_TARGETS`（JSON，按服务给出全部部署事实）：

  ```json
  {"order-service": {"namespace": "order", "deployment": "order-service",
                     "container": "order-service", "port": 8080,
                     "probes": [{"path": "/actuator/health", "expect": 200},
                                {"path": "/quotation?orderId=ORD001", "expect": 200}]}}
  ```

  （与 `agentflow-testbed` 的 manifest 对齐；探针期望码按 `REPRODUCE.md` §4.2 的真实判据取
  —— 故障态 500、修好后 200。）
- 两个都**不给默认值**：配漏了 fail-closed 报错，不是静默跑一条"看起来对"的命令、
  或落到别的命名空间（§9.6：配漏的后果是整条 run 挂掉而症状指向别处）。

### 4.5 执行器常量

- `agentflow/executor/dag_executor.py:66` `SIDE_EFFECT_AGENTS` += `merger` / `deployer`
- 同文件 `:96` `VERDICT_FIELDS` += `merger: merged` / `ci-builder: built` / `deployer: deployed` /
  `smoke-tester: passed`
  （`merger: merged` 与 `deployer: deployed` 同理：工具返回 `merged: false` 而 agent 如实照报时，
  没有这条映射节点照样是 DONE，下游会继续去构建、去部署一个**根本没合进去的东西**）

### 4.6 种子 `agentflow/seed/workflows/problem-diagnose-fix.yaml`

- 新增 5 个节点，边改成 `commit → merge → ci → approve-deploy → deploy → verify-deploy → ticket-done`；
  **删掉** `commit → ticket-done` 那条。
- `verify-deploy → ticket-done` 用 `when: $.nodes.verify-deploy.output.passed == true`
  （与 `test → review` 同款：VERDICT_FIELDS 兜底 + 边显式表达意图）。
- **`merge` 节点的 `params` / `require` 按 D3.1 写**（输入取自 `$.nodes.commit.output.pr_url`，
  `require: [service, pr_url]`）。
- `merge` / `ci` / `deploy` / `verify-deploy` 显式写 `on_failure: abort`（默认虽是 abort，
  但本图里 `plan` / `fix` / `ticket-done` 都是显式写的）。`retry` 不加 ——
  失败原因多半是环境/凭证，重试烧的是 LLM 与时间。
- `approve-deploy`：见 D5 / D5.1（`timeout` 写字面量；驳回语义建议 `abort` 无驳回边）。
- `approve-commit` 的 `name` / `description` 按 D5.2 改。
- `ticket-done` / `recap` 的 `params` 增补 `merge` / `ci` / `deploy` / `verify-deploy` 的输出，
  使「已解决」有部署证据；文件头注释同步更新。
- 注释按本仓风格写**为什么**（尤其 D2 的 `docker build` 归属、D5.1 的驳回语义、D6 的 namespace）。

### 4.7 其它

- `scripts/doctor.py` + `tenantctl._env_preflight`：新增前置（gh 能 merge、docker daemon、
  minikube、kubectl、两个新 JSON 配置）。这些都是"缺了不报错"那类 ——
  正是 doctor 存在的理由。照 `sandbox_image_preflight` 的**三态写法**："判不了就不报"。
- `.importlinter`：`agentflow.agents` 不得 import `api/service/worker`（契约 `no-reverse-dependency`）。
  `release_tools.py` 读配置走 `..config`（照 `workspace_tools.py:53-57` 的 `_workspace_root()`），
  别 import service。
- 文档：`docs/constraints/09.6-sandbox.md`（新增编译/打包路径）、`09.7-pr-gh.md`（merge 能力）、
  `CLAUDE.md`（「结构速览」的 `16-agent 编队` → 20-agent；`registry.py:1` 的 docstring 同改）、
  `docs/design-v5.8.md` §4.15 补一段发布链、`docs/TODO.md`（D2 的接受风险 + §22 的 namespace）。
- `toolchain.toml`：若要给 docker/kubectl/minikube 也立版本基线，加在这里（当前只有
  python/ruff/gh/jq/podman/node）。

---

## 5. ⚠️ 部署形态：这条链只在**本地裸进程**形态成立

`docker/Dockerfile.worker` 只装了 `git`；`deploy/worker-deployment.yaml` 的 worker 容器
**没有 docker CLI / kubectl / minikube，也没有 docker socket 挂载**，`serviceAccountName`
指向的 Role / RoleBinding 在 `deploy/` 下**根本不存在**。

⇒ 容器形态下这三个工具会 `FileNotFoundError`（`_run` 不 catch，裸异常冒到 runner）。
"由 worker 宿主机跑"这句话，**只在 compose + 本地进程的形态下为真**。

**本次不解决它，但必须让它响亮地失败**：`ws_build_image` / `ws_rollout` 起头先
`shutil.which("docker")` / `which("kubectl")` / `which("minikube")`，缺了直接报
「本形态下没有 X —— 发布链只在本地进程形态可用（见 RELEASE_CHAIN_PLAN_zh-CN.md §5）」，
而不是把一个 `FileNotFoundError` 冒到 runner 变成"节点失败"。
这条与 `docs/TODO.md` §22（ActionExecutor 没接线）是同一类账，记进 TODO。

---

## 6. 四个"改了不会报错"的点

### 6.1 `ticket-done` 的提示词：`sync-agents` **更新不了它**

`ticket-done` 是自定义 agent，提示词的真源就是 `agentflow/seed/agents/ticket-done.yaml`。
它的状态判据现在写的是「`commit.pr_url` 非空 ⇒ `resolved`」—— 加了发布链之后应改成
「部署 + 冒烟过了才 `resolved`」。

**但**：seed 是"空表才播、绝不覆盖"，而 `scripts/push_seed_agents.py` 的语义是
**「缺行才建、已有行不动」**（prompt 是本地可改的）。⇒ **即使跑了 `make sync-agents`，
已存在的 `ticket-done` 行的 prompt 也不会被更新。** 已开通租户只能
`PUT /agent-configs/ticket-done`（或在 UI 上改）。

这是全案最容易"改完没效果、零提示"的地方。

### 6.2 `sync-workflows` 是必须的，且会带上一个副作用

workflow 的真源是数据库（§6.0）。不改库，已开通租户跑的**还是 10 节点旧图**，
5 个新节点永远不出现。改完 YAML 要：

```bash
DRY=1 make sync-workflows TENANT=otr     # 先看一遍
make sync-workflows TENANT=otr
```

⚠️ **`default` 与 `local` 两个库里存的是旧变体**（顶层 `inputs.rca`、`plan.require: [rca]`，
实测）。`TENANT=otr` **不会碰它们** —— `push_seed_workflows.py --tenant otr` 只写 otr 的库。
但它们迟早要对齐，届时那是**一次输入契约的变更**（`$.inputs.rca` → `$.inputs.bug_report.diagnosis`），
不是一次例行同步：调用的那一侧也得跟着改。

### 6.3 沙箱编译打包：三件必须先验的事

1. **300s 硬顶**：`SBX_MAX_EXEC_SECONDS=300` 是沙箱侧的 `min(timeout, MAX)` 硬上限，
   客户端传多大都没用。
2. **gradle 缓存是给 `test` 烤的**：`Dockerfile.java21` 里的预热项目跑的是
   `./gradlew --no-daemon test`，而 `clean bootJar` 多出 `compileJava` + `jar`。
   §9.6 那条既有验证配方（干净副本 + `--network none` + `podman run`）**只覆盖 test**。
   ⇒ **动手前先把它换成 `clean bootJar` 跑一遍**：`--network none` 下能过，
   才说明打包所需插件/依赖也烤进去了；过不去就是运行时下载，而沙箱**不该有凭据、
   也不保证出网** —— 等于把 CI 的正确性押在沙箱能上网。
3. **产物必须落回 worker 看得见的那份工作区**：`ws_write_file_sandboxed` 为此专门做了
   "沙箱写完在 worker 侧复读"（`workspace_tools.py:194-236`），doctor 也有往返探针。
   构建产物是整条链里最不该静默丢的东西 —— `ws_build_artifact` **必须做同样的复读**，
   复读不到就报"两侧挂的不是同一个工作区卷"，绝不报成功。
   附带一条已知坑：`exec_service` 的超时只杀直接子 `/bin/sh`，`./gradlew` 拉起的 **java
   不被杀**，会继续持有构建目录锁 —— 同工作区下一次构建可能卡在 "Timeout waiting to lock"。

### 6.4 另一条：resume 之后的产物可能已经不在了

`from_checkpoint` **只把非终态节点重置为 pending**（DONE ∈ TERMINAL，不重跑）。
所以"`ci` 成功、`deploy` 失败后 resume"这条路上 **`ci` 压根不会被执行**，
`execute_with_idempotency` 的缓存分支根本走不到 —— 幂等键在这条路径上不承担任何作用，
全部押在"镜像还在本机"这个**没有校验的假设**上。而工作区在 K8s 形态下是 `emptyDir`，
Pod 一重起就没了；`minikube stop/start` 后镜像也不保证还在。

⇒ **`ws_rollout` 起头必须显式校验产物存在**（`docker image inspect <tag>` /
与 `ci` 的输出比对），缺失就报「产物不在，需重跑 ci」，而不是让 `kubectl` 报
`ImagePullBackOff` 再等 300 秒。

---

## 7. 验证（交产物，不自述）

1. **基线**：`make test` 必须仍是 `0 failed / 503+ passed`（新用例另计）。
2. **静态拦网要看见它红**（变异验证，附"变异真的生效"的证据）：
   - 故意把某个 `$.nodes.ci.output.<field>` 写成不存在的字段名 →
     `test_seed_params_reference_real_schema_fields` 必须红。
   - 故意从节点集合里删 `verify-deploy` → `test_problem_diagnose_fix_workflow.py` 必须红。
   - 往 `VERDICT_FIELDS` 加一条但不给覆盖 → 确认参数化用例数 +1。
3. **单测层**（照 `tests/test_problem_diagnose_fix_workflow.py` 的脚本化 runner）：
   - `OK_OUTPUTS` 补 4 条；happy path 现在要**停放三次**（`approve-plan` → `approve-commit`
     → `approve-deploy`），既有断言写的是两次。
   - 新增：部署门驳回 → run 的状态符合 D5.1 的选择、`deploy` / `verify-deploy` /
     `ticket-done` 全 SKIPPED。
   - 新增：`smoke-tester` 返回 `passed: false` → 节点 FAILED、run failed、`ticket-done` 不执行。
4. **沙箱离线打包**：见 §6.3 第 2 条，先于一切联调跑。
5. **工具层直跑**（先不烧 LLM token）：`scripts/` 下临时脚本直接调
   `ws_merge_pr` / `ws_build_image` / `ws_rollout` / `ws_smoke_probe`，对着**已跑着的
   minikube + order namespace** 各跑一遍，核对：镜像 tag 进了 minikube（`minikube image ls`）、
   Deployment 的 image 变了、pod 滚动完成、探针 200。
6. **端到端**：重启 API 与 worker（prompt/schema 改动不重启就是静默跑旧的）后，
   用 `scripts/watch_run.py --recent --traces` 跑一张带 `bug_report.diagnosis` 的工单，
   走到工单回传。检查 `GET /runs/{id}` 的图上 5 个新节点都是 DONE，
   且 `ticket-done` 的 payload 状态与冒烟结果一致。

---

## 8. 收尾（本仓硬性流程）

一个 task 一个 commit，提交信息写**为什么**（尤其 D1「为什么不加 kind」、
D2「docker build 为什么不放 ci」、D5.1「驳回为什么不能是 continue」）与验证方式。
安全清单重点核对：新工具是否有静默降级、argv 是否 list 形式、模型给的值会不会被当旗标解析、
kubectl/gh 子进程是否都有超时与 DEVNULL（§9.6 的挂起族）。

---

## 9. 需要你拍板的三件事

1. **D5.1 部署门驳回的语义**：`abort` + 无驳回边（推荐，与 `approve-plan` 同款）
   还是 `continue` + 驳回边 → `recap`（会绿着一条没交付的 run）？
   还是更进一步的「驳回 → `ticket-done` 报 `failed`」（最好的闭环，但要一并改自定义 agent 的提示词）。
2. **D5.2 是否接受 `approve-commit` 语义变成"同意合并到主干"**，
   还是把 merge 挪到 `approve-deploy` 之后（`ci` 只编译并产出 tag → 审批 → `deploy` 里
   依次 merge + 建镜像 + 滚动）。
3. **D2 的归属**：`docker build` 放 `deploy`（推荐，压在人工门之后）还是放 `ci`
   （更贴"CI 就是编译打包"的字面，但会在任何审批之前执行仓库的 Dockerfile）。
