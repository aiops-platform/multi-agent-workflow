# deploy 节点：走 MCP 的路线图

> **谁在什么时刻读它**：**执行者**（动手前通读，按 §3 顺序做）+ 评审者
> （看 §2「拓扑约束」与 §1「为什么不能加在 aiops-datasource 上」时）。
>
> 上游见 `RELEASE_CHAIN_PLAN_zh-CN.md`（五节点全貌）、`CI_NODE_PLAN_zh-CN.md`（第三步）。
> merge / ci / approve-deploy 已落地（`git log`：`cbf6bd5` / `e732e1d` / `f427db8`）。

---

## 0. 目标与形状

```
现在:  … → ci → approve-deploy → ticket-done → recap
本轮:  … → ci → approve-deploy → deploy → ticket-done → recap
以后:  … → ci → approve-deploy → deploy → verify-deploy → ticket-done → recap
```

`deploy` 是这条链上**第一个真的改集群**的节点（`merge` 改 git、`ci` 只产出镜像、
`approve-deploy` 只是门）。

```
deployer agent（内置，**本地工具 0 个**）
   │  经 agent_configs.mcp_server_ids 绑定 deploy-ops
   ↓
deploy-ops MCP server（新起的一台；**唯一绑定方就是 deployer**）
   └ rollout_deployment(service, image)
        ① minikube image load <image>      ← 宿主 store → 节点 containerd（不经 registry）
        ② kubectl -n <ns> set image …
        ③ kubectl -n <ns> rollout status …
        ④ 回读 pod 的 .spec.containers[0].image 与 image 比对
   ↓
kubectl / minikube 子进程 → minikube 集群
```

**跨两个仓**（比上一版少一个）：

```
aiops-mcp-servers   新起 deploy-ops（一个写工具 + 一个只读工具）
multi-agent-workflow  deployer agent、server 注册与绑定、deploy 节点与边
agentflow-testbed    **完全不动**（imagePullPolicy: Never 保持不变）
```

---

## 1. 为什么不能把工具加在 `aiops-datasource` 上

实测原文：

| 位置 | 原文 |
|---|---|
| `backends/k8s.py` 模块 docstring | 「安全约定：命令**白名单**（只 get/describe pod，**不提供 apply/delete/exec 等写操作**）」 |
| `tools/ticket.py` docstring | 「`returnApmTicketStatus` —— **本 server 唯一的对外写面**」 |
| `server.py:63` | 「**唯一的写工具是 `returnApmTicketStatus`**」 |

再加**绑定粒度**：`agent_configs.mcp_server_ids` 是 **server 级**、不是工具级
（`docs/TODO.md` §16）。`aiops-datasource` 已被 **9 个诊断 agent** 绑着 ——
往它上面加 `rollout_deployment`，等于把这把枪发给所有绑它的 agent。

⇒ **必须另起一台 server**，只绑给 `deployer`。

> 顺带记：那台 server 的 K8s 后端**本来就是 kubectl 子进程**（`_kubectl(*args)`，
> `create_subprocess_exec` 非 shell，带超时与错误归一）。新 server 照抄即可 ——
> "用 kubectl 还是 python client"这条在本仓早就定了。

---

## 2. ⭐ 镜像**不经 registry** 进集群 —— 代价是一条显式拓扑约束

**已拍板（2026-09-28）**：`deploy-ops` 自己执行 `minikube image load`，
**不引入 registry**、**不改 testbed 的 `imagePullPolicy`**。

### 为什么可行

真正的约束不是"MCP 能不能碰集群"，而是「**镜像在谁手上**」：

```
ci 把镜像 build 在**宿主的 podman store** 里（docker CLI → context=default → podman 6.0.2）
要让集群"不经 pull"拿到它，搬运工就必须读得到那个 store
⇒ **搬运工必须与 docker/podman daemon 同机**
```

而 `deploy-ops` 是本地进程（同机）—— 它满足这条。

### ⚠️ 于是这条拓扑约束是**承重的**，必须显式化

> **`deploy-ops` 必须跑在"有 docker/podman daemon 且镜像就 build 在那台机器上"的主机上。**

**反例（会静默失效）**：有人把它搬进集群（很自然的下一步）—— 它看不到宿主 store，
`minikube image load` 会失败，而报错离原因很远。

⇒ **必须做启动自检**（照 `tenantctl._env_preflight` 的三态写法：判不了就不报）：

```
启动时探两件事：
  ① `docker image inspect <任意一个已知 testbed 镜像>`（或列一下 store）→ daemon 可达吗
  ② `minikube status` → 集群可达吗
探不到 ⇒ **拒绝启动**并打一句能操作的错：明确写出"本 server 必须与 docker daemon 同机"，
          而不是等某次 run 滚到一半才炸
```

### 如实记：这条路的**已知代价**

1. **`minikube` 是"本地开发集群"的概念 —— 真集群没有它。** 把 `minikube` 这个 CLI
   放进一台"集群运维"的 server，等于**把 testbed 形态焊进产品件**。
   当前的定位就是 testbed 运维（整个栈都是本地 minikube），可以接受；将来要跑真集群，
   就得回到 registry 那条路（见 §6「以后」）。
2. **平台侧看不到"镜像怎么进的集群"**（与 §4 同一条代价：细节都留在 server 侧）。

### 顺带省掉的三件事

- 不用在 compose 加 registry、不用 `--insecure-registry`、**不用重启 minikube**；
- **testbed 完全不动**（`imagePullPolicy: Never` 保持不变，跨仓从两个变一个）；
- **`image_tag` 不变**（仍是 `<svc>:<sha12>`，不带 registry 前缀）——
  于是 `ci` 的输出、`approve-deploy` 的卡片、`image_tag_for()` 全都不用改。

---

## 3. 批次（只剩两个）

### 批 1：`deploy-ops` MCP server（另一仓 `aiops-mcp-servers`）

照 `aiops-datasource-mcp-server` 的骨架起一台新 server：

| 工具 | 做什么 | read_only |
|---|---|---|
| `rollout_deployment(service, image)` | 查表 → ① image load ② set image ③ rollout status ④ **回读** | **False** |
| `get_deployment_status(service)` | 回读该服务 pod 的镜像与 phase | True |

⭐ **签名只收 `service`、不收 namespace** —— 见 §4。

**四条步骤与各自的失败姿态**（这条是前一版设计的落点，逐条都要实现）：

```
① minikube image load <image>
     rc≠0 ⇒ 停在这里，**不做 set image**     ← 索引失败还去 set，等于让集群白等一轮超时
② kubectl -n <ns> set image deploy/<d> <container>=<image>
③ kubectl -n <ns> rollout status deploy/<d> --timeout=<N>s
     失败/超时 ⇒ 错误文案里**带 rollback 命令**（kubectl -n <ns> rollout undo deploy/<d>）
                 —— 给人信息，不替人做自动回滚（理由见 §5 D4）
④ 回读 pod 的 .spec.containers[0].image，与传入的 image 比对，不一致 ⇒ 报错
```

**① 的跳过优化（2026-09-28 加）**：`minikube image ls` 里已经有这个 tag ⇒ 不灌。
tag 是**内容寻址**的（`<svc>:<主干 sha12>`），**同名即同物**。⚠️ 名字要归一
（节点列的是 `docker.io/library/order-service:t`，调用方给的是 `order-service:t`），
而归一化**刻意只认两种众所周知的补全**（`docker.io/library/`、`docker.io/`）：
认不出来就原样比较 ⇒ 比较不上 ⇒ **照常灌**。判据的方向是**故意**的 ——
**慢一次可以接受，漏一次不行**（漏了的症状是 `ImagePullBackOff`，离原因很远）。
`minikube image ls` 失败/超时同样回退到"灌"。

**第 ④ 步不是形式**：`rollout status` 只证明"Deployment 的 rollout 完成了"，
**不证明"跑着的是我要的镜像"** —— `replicas: 0` 时 `set image` 会"成功"而**没有任何 pod
起来**，`rollout status` 也可能立刻返回。回读是唯一能抓住它的判据
（§3.3「**声称改了 ≠ 真改了**」的 deploy 版）。

**必须照抄的既有件**（都在 datasource server 里，别另写一份）：

- `backends/k8s.py` 的 `_kubectl(*args)` —— `create_subprocess_exec` + 超时 + 错误归一为 `AppError`；
- `_check_name()` 的字符校验；
- **命令白名单**：这台 server 只放 `minikube image load` / `set image` / `rollout status` / `get`，
  **不提供** `apply` / `delete` / `exec` / `patch`；
- 错误走 `errors.AppError(ErrorCode...)`，**不抛裸异常**。

**它自己的身份说明**（写进 `server.py`，与 datasource 那句对称）：
「**本 server 只做一件事：把某个 Deployment 滚到指定镜像，并如实回报结果**」，
外加上面那条拓扑约束。

**它要自带的那张表**（`service → {namespace, deployment, container}`），
放 server 自己的配置，**缺条即 fail-closed**，不猜一个默认命名空间。当前值（实测）：

```yaml
order-service:    {namespace: order, deployment: order-service,    container: order-service}
warranty-service: {namespace: order, deployment: warranty-service, container: warranty-service}
gateway-service:  {namespace: order, deployment: gateway-service,  container: gateway-service}
```

**真机验证**（先手工，不烧 LLM token）：MCP inspector 或直接打 `/mcp`，
对着 `order` 命名空间滚一个 tag，核 `minikube image ls` / Deployment 的 image / pod 真的换了。

---

### 批 2：agentflow 接线（本仓）

1. **`deployer` agent**：内置（`registry.FIX_AGENTS` + `prompts.SYSTEM_PROMPTS` +
   `AGENT_SCHEMAS`），**一个本地工具都不给** —— 工具全部来自 MCP 绑定。
   ⚠️ 这是本仓**第一个"工具全靠 MCP"的**执行型**内置** agent（`ticket-done` 也是全靠 MCP，
   但它是**自定义** agent）。
2. **注册 + 绑定**：
   - `agentflow/seed/dataplane.yaml` 的 `servers:` 加一条
     （`name: deploy-ops`，`transport: http`，`url_setting: mcp_deploy_url`）；
   - 同文件 `bindings:` 加 `deployer: [deploy-ops]`；
   - `agentflow/config.py` 加 `mcp_deploy_url`；`.env*` 加 `AGENTFLOW_MCP_DEPLOY_URL`。
   ⚠️ 已开通租户（otr）**种子无效**，要 `make sync-agents TENANT=otr`；
   跑完**回读 `GET /agents` 的 `tool_count`** —— 绑定缺行的症状是"零工具且静默"
   （我们刚在 `remediation-planning-analyst` 上连挂 5 次）。
3. **节点**（`agentflow/seed/workflows/problem-diagnose-fix.yaml`）：

   ```yaml
     # ===== 部署（本图第一个真的改集群的节点）=====
     # 不构建：产物在 ci 就定型了，approve-deploy 批的就是那个 tag（build once, deploy many）。
     # 部署目标（namespace/deployment/container）由 deploy-ops 自己持有 —— 平台与模型都不传。
     deploy:
       agent: deployer
       description: "部署：把 ci 构建的镜像装进集群并滚动到该服务上"
       require: [service, image_tag]
       params:
         image_tag: "$.nodes.ci.output.image_tag"
         ci: "$.nodes.ci.output"
         service: "$.inputs.bug_report.cmdb_ci.name"
       on_failure: abort
   ```

   边（删掉现在的 `approve-deploy → ticket-done`）：

   ```yaml
   - { from: approve-deploy, to: deploy, when: "$.nodes.approve-deploy.output.approved == true" }
   - { from: deploy, to: ticket-done, when: "$.nodes.deploy.output.deployed == true" }
   ```

   `ticket-done` / `recap` 的 params 各补一条 `deploy: "$.nodes.deploy.output"`；
   文件头注释与 description 同步。

4. **执行器两个常量**：`SIDE_EFFECT_AGENTS` += `deployer`；
   `VERDICT_FIELDS` += `deployer: deployed`（判据同 `built` / `merged`：
   工具返回 `deployed: false` 而 agent 如实照报时，节点必须**判红**）。
5. **`WORKSPACE_AGENTS` 不加 `deployer`** —— 它不碰工作区。这是 MCP 路线的附带好处：
   少一条"漏加名单"的静默缺口（前一版计划里它必须加，因为 `_run` 要 cwd）。
6. **测试**：工作流级（节点集合、`ticket-done` 入边、`deploy` 的 require/params、
   happy path 仍是**三次停放**）；变异验证（去掉 `require` 里的 `image_tag` → fail-fast 用例必须红）。

---

## 4. ✅ 已定：部署目标**由 MCP server 自己持有**

> **2026-09-28 拍板**：在「server 侧持表」与「agentflow 侧配、传进去」之间选了**前者**。

- 背景：工单里**没有** namespace（实测 `otr` 最近 6 条 run 的 `cmdb_ci` 全是
  `{"name":"order-service","service":"application"}` —— 只有 name 与 service）。
- 前一版计划是 agentflow 侧配 `AGENTFLOW_DEPLOY_TARGETS`；**本版弃用**。

**结论**：工具签名 `rollout_deployment(service, image)` / `get_deployment_status(service)`
—— 只收 `service`，namespace / deployment / container 全在 server 侧那张表里（§3 批 1）。

三条理由：

1. **那是集群侧的事实**，不是平台的事实。平台存一份就等于开第二个真源。
2. **租户边界本来就在集群侧** —— 与 `action_executor._check_ns` 同一判据：
   namespace 该由"持有集群凭证的那一方"裁决，而不是让平台传一个进来。
3. **少一个静默失效面**：表在 server 侧，平台与模型**都没有传错的机会**。

**代价**：集群细节平台不可见，排查时要去看 server 的配置。真形态里应由 CMDB 提供这张映射
（本仓 `locate_repo` 已经走 MCP 了，同一条路）。

---

## 5. 仍然成立的设计决定

**D1. `deploy` 不构建任何东西。**
只做"搬运 + 滚动"。构建一次、到处部署。构建已在 `ci` 做完、人已在 `approve-deploy`
看过那个 tag —— 再构建一次就等于"批的和发的可能不是同一个东西"。

**D2. `deployed` 要进 `VERDICT_FIELDS`。** 见 §3 批 2 第 4 条。

**D3. 幂等性由构造保证。**
同一个 tag 重跑 ⇒ `set image` 是 no-op（pod template 没变）⇒ `rollout status` 立即返回
⇒ 回读仍通过。**不需要**额外的回落分支 —— 与 `ws_merge_pr` 那个"已经合过了"的回落
不是同一类问题（那边没有构造保证，所以必须显式写）。

**D4. 不做自动回滚（已拍板）。**
1. 它是又一个不可逆动作，而这条链上每个不可逆动作前面都有门 —— 自动回滚**没有**；
2. 它**掩盖失败**：run 报 failed，而线上其实已被悄悄换回旧版本，人不知道新镜像没上；
3. 本仓取向是「宁可红着说失败，不要绿着或悄悄补偿」。
**替代**：把回滚命令写进错误文案。

**D5. 碰集群用 kubectl 子进程（已拍板，且与 MCP server 现状一致）。**

---

## 6. 风险 / 未解

| # | 风险 | 影响 |
|---|---|---|
| 1 | **拓扑约束是承重的**（§2）：`deploy-ops` 必须与 docker daemon 同机 | 搬进集群会**静默失效**（症状 `ImagePullBackOff`）。必须做启动自检 |
| 2 | **`minikube` CLI 是 testbed 概念** | 这台 server 将来跑真集群时得回到 registry 那条路。那时要补的正是本文删掉的批 0/批 1 |
| 3 | **绑定缺行 = 零工具且静默** | 刚在 `remediation-planning-analyst` 上踩过（连挂 5 次）。`sync-agents` 后要回读 `GET /agents` 的 `tool_count` |
| 4 | ~~`rollout status` 的 `--timeout` 值没实测过~~ | ✅ **已实测（2026-09-28）**：`minikube image load`（镜像**已存在**也要）**144s** / `kubectl set image` **0s** / `rollout status` **12s**。当前配置 `image_load=900s`、`rollout=300s`、子进程 `360s` —— 余量够，但**成本全在 image load 那 144 秒** |
| 5 | ~~`minikube image load` 每次都付 ~144s~~ | ✅ **已做（2026-09-28）**：`rollout_deployment` 先 `minikube image ls`，在节点里就**跳过 load**。真机实测 **140s → 12s**（~10×），返回里带 `image_reused`。判据**刻意保守**（见下） |
| 6 | ⭐ **终止中的 pod 仍然报 `phase: Running`**（真机踩出来） | 第一版回读只看 phase、还取列表第一个 ⇒ 滚动**已经成功**了却报**假失败**。实测（2026-09-28 本机 testbed）：`rollout status` 已报 successfully rolled out、新 pod 在跑新镜像，而回读拿到的是正在终止的旧 pod。**修法**：过滤 `metadata.deletionTimestamp`，且**全部在服务的 pod 都要对**（只挑一个对的会漏掉"还有一部分是旧的"）。回归用例 `test_terminating_old_pod_is_not_mistaken_for_the_deployed_one` |

---

## 7. 相邻的一条债（本轮不做，但要说）

`ticket-done` 是**自定义 agent**，提示词把判据写成「`commit.pr_url` 非空 ⇒ `resolved`」。
有了 `deploy` 之后，那句应当改成「**部署并验证过了**才 `resolved`」。
它的真源在 `agentflow/seed/agents/ticket-done.yaml`，且 **`sync-agents` 对已有的行不动**
（`scripts/push_seed_agents.py` 的语义是"缺行才建"）—— 已开通租户只能
`PUT /agent-configs/ticket-done`。`RELEASE_CHAIN_PLAN_zh-CN.md` §6.1 记过同一条。

---

## 8. 与前面两版的关系（都已被本版取代）

| 项 | v1（worker 子进程） | v2（MCP + registry） | **v3（本版：MCP + image load）** |
|---|---|---|---|
| 谁碰集群 | worker 进程 | MCP server | **MCP server**（同 v2） |
| 镜像怎么进 | `minikube image load` | registry + 集群 pull | **`minikube image load`**（同 v1） |
| testbed 改动 | 无 | `Never` → `IfNotPresent` | **无** |
| 批次数 | 1（本仓） | 4（跨三仓） | **2（跨两仓）** |

**v3 = v1 的"镜像路径" + v2 的"执行体"**。取两者各自对的那一半：
镜像那条 v1 对（不经 registry 更省），执行体那条 v2 对（凭证与租户边界该在集群侧）。
