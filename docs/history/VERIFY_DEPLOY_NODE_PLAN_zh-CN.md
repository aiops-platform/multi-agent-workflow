# verify-deploy 节点设计（发布链最后一步）

> **谁在什么时刻读它**：实施者（动手前通读）+ 评审者（看 §2 D4「探针**不由人配**」与 D6 时）。
> 上游见 `RELEASE_CHAIN_PLAN_zh-CN.md`、`DEPLOY_NODE_PLAN_zh-CN.md`。
> merge / ci / approve-deploy / deploy 都已落地、真机验过。

---

## 0. 位置：做完这条链就齐了

```
现在:  … → approve-deploy → deploy → ticket-done → recap
本轮:  … → approve-deploy → deploy → verify-deploy → ticket-done → recap
```

前面所有节点只证明"东西备好了 / 滚上去了"，**只有它回答「跑起来真的能响应吗」**。
它也是 `ticket-done` 之前最后一道关 —— 冒烟没过就不该回传「已解决」。

### 0.1 流程总览（先看这一节）

```
① 探针从哪来 —— 两条来源，都不经人手

   健康层（路径 + 端口）
     Deployment 自己声明的 readinessProbe.httpGet
       {"path":"/actuator/health", "port":8080}
     ★ 那句探针的语义就是"这容器能不能接流量"，与冒烟同义
       ⇒ 平台不抄第二份（抄了就是第二个真源，必然漂移）

   业务层（这次故障打到的那条链路）
     本图 `plan` 节点（fix-planner）产出 → plan.steps[].probe
       {path, expect, broken_expect}
     ★ 它拿的是工单里那份诊断（rca 的 summary 里就有 "GET /quotation/exception"）
     ★ 三条约束见 §2 D5 —— 没有它们，这条路就退化成"让模型编 URL"

② 节点流程

   agentflow 进程：DAG 调度到 verify-deploy
     resolve_params
       ├ service   ← 工单的 cmdb_ci.name
       ├ image_tag ← $.nodes.ci.output.image_tag     ← 要验**哪个镜像**
       └ probe     ← $.nodes.plan.output.…probe      ← 探**哪条业务链路**（可能为空）
     require 预检：service / image_tag 缺一即 fail-fast
     → LLM（smoke-tester）→ 调 MCP 工具
       probe_service(service, image, path, expect, broken_expect)

          │  HTTP
          ▼
   deploy-ops 进程（另一个进程，持 kubeconfig 与部署表）
     ① 查表 service → {namespace, deployment, container}   缺条 ⇒ fail-closed
     ② 读 Deployment 的 readinessProbe → port + 健康探针路径
          读不到 ⇒ deployment_probe_missing（**不猜 8080**）
     ③ 取「正在跑 <image> 的非终止 Running pod」
          取不到 ⇒ pod_not_found，**一次 HTTP 都不发**（不去打旧 pod）
     ④ 挑空闲端口 → kubectl port-forward pod/<pod> <local>:<port>
          等就绪（读 stdout 的 "Forwarding from"）
     ⑤ 探健康层（+ 业务探针，若给了）→ 逐条**精确**比对期望码
     ⑥ finally：kill 掉 forward —— **无论成败**

          │
          ▼
   回到 agentflow
     passed: false ⇒ VERDICT_FIELDS 判节点 **FAILED** ⇒ run failed ⇒ **工单不回传**
     passed: true  ⇒ ticket-done → 回传 resolved
     附 coverage: "business" | "health_only"

③ 为什么"只探跑着那个 image 的 pod"是承重的

   deploy 证明的       ：滚上去了
   verify-deploy 要证的 ：**验的就是那份部署**

   三种假绿形态，都返回 200：
     ├ 滚动还没完成
     ├ 被回滚了
     └ 探到了另一个旧 pod  ← 本机那 7 个 port-forward 里，order-service 那个（18080）
                              在滚动换过 pod 之后**打不通了**（`curl` 0.011s 失败，实测）
```

---

## 1. 已核实的前提（实测，不要重新假设）

| 事实 | 怎么核实的 |
|---|---|
| ⭐ **Deployment 自己声明了探针路径与端口** | `kubectl -n order get deploy order-service -o jsonpath='{.spec.template.spec.containers[0].livenessProbe.httpGet}'` → `{"path":"/actuator/health","port":8080,"scheme":"HTTP"}`；warranty-service 同。**这两个值不用人配**（见 D4） |
| **"该探哪条业务路径"有讲究** | 实测同一 pod 上：`/actuator/health` → **200**（恒 200，**无鉴别力**）、`/quotation?orderId=ORD001` → **200**、`/quotation/exception` → 400、`/quotation` → 400、`/nope` → 404 |
| **为什么 `/quotation/exception` 不能当探针**<br>⚠️ **2026-09-28 订正：原判据的*理由*是错的，结论仍成立** | 结论：它**没有鉴别力**，别用。原先的理由写成「修复把它从裸 NPE 500 变成了受控业务异常 400」——**实测不成立**。真相是：<br>· `/quotation/exception`（**不带**参数）那个 400 是 **Spring 的「缺必填参数」**（`exception(@RequestParam String orderId)`），**修复前后都是 400**，与修复无关；<br>· **带上**参数后，修复前后**都是 500**：NPE → 受控 `QuotationException`，而 `GlobalExceptionHandler` 把两者都 `sendError(500)`（fix #14 的 commit 明写「不降级 4xx」）。<br>⇒ 这次修复改的是**日志签名**（NPE 堆栈 → `ERROR 报价单模板缺失 orderId=… traceId=…`），**状态码一个都没变** ⇒ 这一类修复**根本没有可用的状态码探针**，正确输出是 `verification_probe: null`（见 D5 第 3 条 / D6）。<br>（代价记在这里：这条错理由一度抄进了 `fix-planner` 的提示词当例子，模型照抄产出 `expect: 400` ⇒ 交付链**假红**。实测 run_f2ce1b59c9。） |
| **`kubectl port-forward` 可用、秒级就绪** | 起临时 forward 后日志出现 `Forwarding from 127.0.0.1:PORT -> 8080`，4 秒内可打 |
| ⭐ **port-forward 绑的是 pod，不是 service** | 本机 testbed 启动脚本起了 7 个 `svc/` 的 port-forward。**2026-09-28 订正**：原记录写的是"全部打不通"，实测只有 **order-service 那个（18080）打不通**（`curl` 0.011s 失败，因为它绑的 pod 早被滚动换掉了）；另两个服务没滚过，转发还活着。**这条直接决定 D3**（判据不变，反而更贴：进程在、服务名没变，**只有 pod 换了**） |
| **本图 `plan` 的 schema 现在没有 verification** | `FixPlanSchema` = `plan.steps[] = {type, target, action, expected}` —— 要加（D4/D5） |
| 诊断侧 `plan` 有 verification，但是散文 | `remediation-planning-analyst` 的 `steps[].verification`，实测形如 `"并发压测 200 次同接口，AIOOBE 不再出现"` |
| `deploy-ops` 已在跑 | `127.0.0.1:8400`，启动自检通过；`DEPLOY_OPS_TARGETS` 目前只有 `namespace/deployment/container` |

---

## 2. 设计决定

### D1. 探针放 **`deploy-ops` 的第三个工具**，不是 worker 侧

判据与 `deploy` 同：**集群细节与凭证留在集群侧**。
`RELEASE_CHAIN_PLAN` D3 表里那行 `ws_smoke_probe` 是按 worker 侧写的 —— **本计划取代它**。

### D2. ⭐ 只探「**正在跑 `image` 的那个 pod**」

本节点唯一的承重判据：打旧 pod 得到的 `200` **什么都不证明**。

```
deploy 证明的      ：滚上去了
verify-deploy 要证的：**验的就是那份部署**
```

三种假绿形态（滚动没完成 / 被回滚了 / 探到另一个旧 pod）都返回 200，而 §1 那条实测
（7 个 forward 全绑着死 pod）说明**这不是理论风险**。实现上复用 `deploy-ops` 已有的
`_read_running_pods()`（它已处理"终止中的 pod 仍报 Running"那个真机踩出来的坑）。

### D3. 探针**自己起 port-forward、绑刚验过的 pod、用完在 `finally` 里 kill**

不复用任何长期 forward。`port-forward` 是**长驻子进程**，漏 kill 就是每验一次泄漏一个。

```
① 取"在跑 <image> 的非终止 Running pod" —— 取不到就直接失败，不去探
② 挑空闲端口 → kubectl -n <ns> port-forward pod/<pod> <local>:<port>
③ 等就绪（读 stdout 的 `Forwarding from`）
④ 逐条探针 GET、比对期望码
⑤ finally：kill —— 无论成败
```

### D4. ⭐⭐ **探针不由人配** —— 健康层从 Deployment 读、业务层由 `plan` 产出

**原来那版是错的**：它让人在 `DEPLOY_OPS_TARGETS` 里写 `port` 与 `probes`。
而这两样东西链上都有人知道，**不该落给人**：

| 层 | 内容 | 来源 | 依据 |
|---|---|---|---|
| **健康层** | 路径 + 端口 | **Deployment 自己的 `readinessProbe.httpGet`**（退 `livenessProbe` / `startupProbe`） | 那句探针的语义**就是**"这容器能不能接流量"，与冒烟同义。平台再抄一份 = 第二个真源，必然漂移 |
| **业务层** | 这次故障影响到的那条链路 | **本图 `plan` 节点（`fix-planner`）** 产出，走 `plan.steps[].probe` | 它拿的是工单里那份诊断（rca 的 summary 里**就有** `GET /quotation/exception` 这类路径），而且**最接近"这次改了什么"** |

⇒ **`DEPLOY_OPS_TARGETS` 因此缩到只剩 `namespace/deployment/container`**，
`port` 与健康探针**从集群读**，业务探针**从节点传**（工具签名见 §4）。

### D5. 业务探针的三条约束（**没有这三条，B 就会变成"让模型编 URL"**）

`plan` 产出的是一个结构化对象：

```yaml
probe:
  path: "/quotation?orderId=ORD001"    # 服务内相对路径
  expect: 200                           # 修好之后该是什么码
  broken_expect: 500                    # **故障态**是什么码（必填）
```

| # | 约束 | 为什么 |
|---|---|---|
| 1 | 路径**只能取自入参**（工单 / rca / 诊断 plan 里**出现过**的接口） | 同 `service-scoper` 那条已写死的判据「**不要自己添加工具没返回的候选**」 |
| 1b | **状态码要真的变**：这次修复只改日志/异常类型（状态码不变）⇒ 填 `null` | 实测教训（2026-09-28 run_f2ce1b59c9）：`/quotation/exception?orderId=…` 修复前后**都是 500**，模型给出 `expect: 400` ⇒ 探针一跑就**假红**。§1 那条订正就是这条约束的由来 —— **"能鉴别"不能只看路径像不像，要看状态码变没变** |
| 2 | **必须声明 `broken_expect`，且与 `expect` 不同** | 把"这条探针有没有鉴别力"从**判断题**变成**声明 + 校验**：相同 ⇒ 直接拒（同 `deploy-ops` 里那条 timeout 关系校验）。顺带改善报错：真返回 `broken_expect` 时能说「**这条路径还是坏的**」 |
| 3 | **给不出就明确不给** | 写进 `open_questions`，而不是编一条。编出来的必然 404 ⇒ 响亮地红，但那是噪声 |

### D6. `coverage: health_only | business` —— **不判红**

诊断/计划**给不出业务探针**是合法情况（CPU 打满那类故障**没有 HTTP 链路**，
修复是扩容不是改接口）。那种情况：

- **只探健康层**（从 Deployment 读的那条）；
- 输出里**如实带** `coverage: "health_only"`，run 照常绿。

理由：**红只会制造噪声、让人去关掉它；可见会让人去看**。而那类故障本就该用指标/日志验 ——
那是另一个手段，不该由这个节点假装自己有。

### D7. 判据是「**期望码的精确比对**」

`expect: 200` 精确比对，**不放宽到 2xx**：`/quotation` 故障态 500、修好后 200，
放宽就证明不了修复。不校验响应体（那是另一件事，见 §6）。

### D8. **失败不重试、不自动回滚** —— 如实红

与 `deploy` 同一条取向（`DEPLOY_NODE_PLAN` D8）：探针不过 ⇒ `passed: false` ⇒
**节点判红** ⇒ run failed ⇒ **不回传工单**。不给探针加重试：一过性抖动与"服务真的起不来"
混在一起，重试只会把后者也洗成绿。

### D9. `smoke-tester: passed` 进 `VERDICT_FIELDS`；**不进** `SIDE_EFFECT_AGENTS`

只发 GET，重跑不产生外部可见变化。

### D10. 路径**只收相对路径**（不含 `://`）

`path` 必须以 `/` 开头且不含 `://` —— 挡住"让探针去打别的主机"。这条加上 D2/D3
（forward 绑 pod、端口来自 Deployment），面就收在**被部署的那个服务**上了。

> **接受的风险（如实记）**：业务探针的路径**由模型产出后经节点传进来**，所以理论上
> 它能打到**那个服务上的任意 GET 路径**（如 `/actuator/env`）。我们只比对状态码、不落响应体，
> 所以信息不落地；而"打自己服务的某个接口"本来就是冒烟该有的能力。**不构成越界到别的主机。**

---

## 3. 批次

### 批 1：`deploy-ops` 加第三个工具（另一仓 `aiops-mcp-servers`）

```
probe_service(service, image, path="", expect=0, broken_expect=0)
    path 空 ⇒ 只探健康层（coverage=health_only）
    path 非空 ⇒ 健康层 + 这条业务探针（coverage=business）
```

| 步骤 | 做什么 |
|---|---|
| ① | 查表 `service → {namespace, deployment, container}`；缺条 ⇒ fail-closed |
| ② | `kubectl get deploy -o json` 读 **`readinessProbe.httpGet`**（退 liveness/startup）⇒ `port` + 健康探针路径。**读不到 ⇒ 报错**（不猜 8080） |
| ③ | 取"在跑 `<image>` 的非终止 Running pod"；取不到 ⇒ `pod_not_found`，**一次 HTTP 都不发** |
| ④ | 挑空闲端口 → 起 forward（绑那个 pod）→ 等就绪 |
| ⑤ | 探健康层（+ 业务探针，若给了），逐条比对期望码 |
| ⑥ | `finally` kill forward |

返回：

```python
{"passed": True, "coverage": "business", "service": "order-service",
 "image": "order-service:211eae4e2518", "pod": "order-service-7d9f…", "port": 8080,
 "probes": [{"path": "/actuator/health", "expect": 200, "status": 200, "ok": True, "ms": 9, "source": "deployment"},
            {"path": "/quotation?orderId=ORD001", "expect": 200, "status": 200, "ok": True, "ms": 94, "source": "plan"}],
 "failed": [], "summary": "2/2 探针通过（业务链路已验）"}
```

失败时 `passed: false` + `failed: [...]`，并指明卡在哪一层：
`pod_not_found` / `deployment_probe_missing` / `forward_failed` / `probe_failed`。

### 批 2：agentflow 接线（本仓）

| # | 文件 | 改什么 |
|---|---|---|
| 1 | `agentflow/agents/schemas.py` | **`FixPlanSchema` 的 `plan.steps[]` 加 `probe`**（`path`/`expect`/`broken_expect`） |
| 2 | `agentflow/agents/prompts.py` | `fix-planner` 的提示词加 D5 那三条约束（**这是 B 的关键一步**：不给约束就是让模型编 URL）；`SYSTEM_PROMPTS["smoke-tester"]` + `AGENT_SCHEMAS["smoke-tester"]` |
| 3 | `agentflow/agents/registry.py` | `FIX_AGENTS` += `smoke-tester`；`AGENT_STAGES`（verify）；`AGENT_DESCRIPTIONS`；docstring 19→20 |
| 4 | `agentflow/agents/schemas.py` | `SmokeResultSchema` |
| 5 | `agentflow/seed/dataplane.yaml` | `bindings:` 加 `smoke-tester: [deploy-ops]` |
| 6 | `agentflow/executor/dag_executor.py` | `VERDICT_FIELDS` += `smoke-tester: passed` |
| 7 | `agentflow/seed/workflows/problem-diagnose-fix.yaml` | `verify-deploy` 节点 + 两条边 + `ticket-done`/`recap` params |
| 8 | 测试 | 见 §5 |

> `WORKSPACE_AGENTS` 不加；`TOOL_REGISTRY` 不加（工具全在 MCP）。
> ⚠️ 改了 `fix-planner` 的提示词/schema ⇒ **必须重启 API 与 worker**（否则静默跑旧的）。

---

## 4. 形状

```yaml
  # ===== 部署后验证（本图最后一道关）=====
  # 前面所有节点只证明"东西备好了 / 滚上去了"，只有这里回答"跑起来真的能响应吗"。
  # **探针不由人配**：
  #   · 健康层（路径 + 端口）→ 从 Deployment 自己的 readinessProbe 读；
  #   · 业务层 → 由本图 `plan` 节点产出（`plan.steps[].probe`，含 broken_expect）。
  # 工具全在 deploy-ops 上（本节点本地零工具）。
  #
  # ⚠️ 它**只探正在跑 `image_tag` 的那个 pod** —— 打旧 pod 得到的 200 什么都不证明。
  # 判据是**期望码的精确比对**，`passed: false` 进 VERDICT_FIELDS ⇒ 节点判红 ⇒
  # run failed ⇒ **工单不回传**。
  # 计划没给出业务探针时只探健康层、输出带 `coverage: health_only`（**不判红** ——
  # CPU 打满那类故障没有 HTTP 链路，那是另一个手段的事）。
  verify-deploy:
    agent: smoke-tester
    description: "部署后冒烟：对刚滚上去的 pod 打探针与业务接口"
    require: [service, image_tag]
    params:
      image_tag: "$.nodes.ci.output.image_tag"
      deploy: "$.nodes.deploy.output"
      # 业务探针整条从 plan 来（可能为 null ⇒ 只探健康层）
      probe: "$.nodes.plan.output.plan.steps[0].probe"
      service: "$.inputs.bug_report.cmdb_ci.name"
    on_failure: abort
```

⚠️ `probe` 那条路径要**现场核**：`FixPlanSchema` 的实际嵌套是 `plan.steps[]`，
而"steps 里哪一条的 probe 该用"（可能有止血步 + 根治步两条）**要在实施时定** ——
若不好选，就让 `fix-planner` 在 `plan` 顶层另给一个 `verification_probe`（**更推荐**：
避开"M 条步骤取哪条的 probe"这个问题）。**这条是本计划里唯一需要实施时定稿的地方。**

边：

```yaml
- { from: deploy, to: verify-deploy, when: "$.nodes.deploy.output.deployed == true" }
- { from: verify-deploy, to: ticket-done, when: "$.nodes.verify-deploy.output.passed == true" }
```

`ticket-done` 的入边**第四次往右挪**（`commit` → `merge` → `ci` → `approve-deploy` → `deploy` → `verify-deploy`），
判据始终是同一条：**回传的判据是"有没有真的交付"，不是"流程跑到哪了"**。

---

## 5. 验证

### 5.1 `deploy-ops` 的单测（`tests/test_probe.py`）

| 用例 | 断言 |
|---|---|
| 正常路径（给业务探针） | 读 Deployment 的探针 → 读 pod → 起 forward → 探两条 → `passed=True`、`coverage=business`；**forward 被 kill** |
| **不给业务探针** | 只探健康层、`coverage=health_only`、`passed=True` |
| **没有在跑该 image 的 pod** | `pod_not_found`，**一次 HTTP 都不发** |
| pod 在跑**别的** image | 同上（D2 正反两面） |
| **Deployment 没有探针声明** | `deployment_probe_missing`，**不猜 8080** |
| 业务探针返回 `broken_expect` | `passed=False`，错误文案能说「**这条路径还是坏的**」 |
| 码不对（expect 200 得 500） | `passed=False`、`failed` 指到那条、其余照样跑完 |
| forward 起不来 / 探针超时 | `passed=False` 且 `finally` 仍 kill |
| **路径含 `://` 或不以 `/` 开头** | 拒（D10） |
| ⭐ **forward 一定被 kill**（成功 / 失败各一条） | 唯一会泄漏进程的地方 |

### 5.2 工作流级

- 节点集合加 `"verify-deploy"`；`ticket-done` 入边从 `["deploy"]` 改成 `["verify-deploy"]`
- `OK_OUTPUTS` 加 `verify-deploy`
- 新增：`require`/`on_failure`/出边带 `passed == true`
- `VERDICT_FIELDS` 参数化用例自动 +1

### 5.3 `fix-planner` 那一侧（**B 的关键，必须有**）

- **schema 断言**：`FixPlanSchema` 里 `steps[].probe` 存在且 `broken_expect` 是 required
- **提示词断言**（照 `test_remediation_plan_prompt_has_direction_contract` 的写法）：
  三条约束的关键句在提示词里（"只取自入参" / "broken_expect 必填且与 expect 不同" / "给不出就不给"）
- **变异验证**（附 `grep -c` 归零 / diff 的证据）：
  - 去掉"只取自入参"那句 → 提示词断言必须红
  - 去掉 D2（不校验 pod 的 image）→ "pod 在跑别的 image"用例必须红
  - 去掉 `finally` 的 kill → "forward 被 kill"用例必须红
  - 把 `expect` 放宽成 `status < 400` → "码不对"用例必须红

### 5.4 真机

1. **先手工**（不烧 LLM token）：`probe_service` 直调一次，对着刚滚完的 order-service
   探健康层 + `/quotation?orderId=ORD001` → 应 `passed: true, coverage: business`；
   再传一条错的 `path` → 应 `passed: false` 且 `failed` 指对。
2. **顺手量**：forward 从起到可用多久、两条探针各多久（同 `deploy` 那次"别拍脑袋定超时"）。
3. 再走一次真实 run：批准 `approve-deploy` 后 `deploy` → `verify-deploy` 都绿、
   `ticket-done` 回传 `resolved`；**并且核 `plan` 产出的 `probe` 是不是真的取自工单**（B 的成败就在这）。

---

## 6. 本轮明确**不做**

- **不给探针加重试**（D8）；
- **不校验响应体内容**（`expect` 只管状态码；"返回的报价单是不是对的"需要业务断言，另行设计）；
- **不做指标/日志类验证**（CPU 打满那类故障的正确验法 —— D6 只保证"这次没验业务链路"是**可见的**，
  不等于我们验了它）；
- **不做延迟/吞吐 SLO**（那要压测）；
- **不改 testbed**（探针只读）。

---

## 7. 做完之后

五节点齐了。三份计划文档按各自开头写的"实施完成后结论应沉进 `docs/`"收口：

- `docs/constraints/09.6-sandbox.md`（构建/打包/部署/冒烟这条链的执行边界）；
- `docs/design-v5.8.md` §4.15（发布链的整段结构，含"探针**不由人配**"这条）；
- `docs/TODO.md`（`deploy-ops` 的拓扑约束与 registry 那两笔债 —— 已有 §34）。

⚠️ 还差一条**跨节点**的债：`ticket-done` 的提示词（自定义 agent，真源在
`seed/agents/ticket-done.yaml`）现在写的是「`commit.pr_url` 非空 ⇒ `resolved`」，
应改成「**部署并验证过了**才 `resolved`」。而 `sync-agents` **对已有的行不动** ⇒
已开通租户只能 `PUT /agent-configs/ticket-done`。`RELEASE_CHAIN_PLAN` §6.1 记过两次。
