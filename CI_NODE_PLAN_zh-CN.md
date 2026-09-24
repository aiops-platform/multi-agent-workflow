# ci 节点设计（发布链第二步）

> **谁在什么时刻读它**：实施者（动手前通读）+ 评审者（看 §3 的设计决定时）。
> 上游背景见 `RELEASE_CHAIN_PLAN_zh-CN.md`（五节点全貌）、`MERGE_NODE_PLAN_zh-CN.md`（第一步）。
> 实现完成、结论沉进 `docs/constraints/09.6-sandbox.md` 之后，本文即可删除。

---

## 0. 位置与现状

```
现在:  commit → merge → ticket-done → recap
本轮:  commit → merge → ci → ticket-done → recap
以后:  commit → merge → ci → approve-deploy → deploy → verify-deploy → ticket-done → recap
```

`merge` 已经真机跑通（`run_e12a47ed4d`：PR #12 合并，`merge_commit = 211eae4e2518…`，
主干 HEAD 就是它）。`ci` 接在它后面。

**本轮先接回 `ticket-done`** —— 和当初先做 merge 是同一个理由：不留半截图，
单独插进去就是一条能跑完、能验证、能回滚的完整链。

---

## 1. 已核实的前提（实测，不要重新假设）

| 事实 | 怎么核实的 |
|---|---|
| **沙箱能离线编译打包** | `podman run --rm --network none … ./gradlew clean bootJar --no-daemon -q` → **8.472 s**，产出 `build/libs/order-service-0.0.1-SNAPSHOT.jar`（35 MB）。当初的预热缓存只烤过 `test`，`bootJar` 是**这次才验的** |
| **产物路径正是 Dockerfile 要的** | 服务仓的 `Dockerfile` 写死 `COPY build/libs/order-service-0.0.1-SNAPSHOT.jar app.jar` |
| **沙箱在跑、镜像在位** | `curl :44772/health` → `{"status":"ok","limits":{"max_execution_seconds":300,…}}`；`localhost/agentflow-sandbox-java21:local`（867 MB） |
| **沙箱与 worker 同一个卷** | 沙箱 `writable_allowlist` 含 `/Users/h.a.hu/agentflow-workspace`；工作区根就在它下面 |
| **`test` 命令的配置形状** | `.env:114` `AGENTFLOW_TEST_CMDS={"order-service": "./gradlew test --no-daemon -q", …}` |
| **与主干的一致性只能比树** | `run_e12a47ed4d` 实测：工作区 HEAD `137a75df…`、主干 squash 提交 `211eae4e…`（**两个 SHA 不同**），但两者的 **tree 都是 `123c2d683b04bf8c59cefe9240c6c551be77b3eb`** |
| **三个服务的 Dockerfile 都没有 `RUN`** | 只有 `FROM eclipse-temurin:21-jre` + `COPY build/libs/*.jar` ⇒ **今天** `docker build` 不执行任何仓库代码。这是 D2 那条债今天没炸的唯一原因 —— 而它来自**被构建的仓库**，不来自平台 |
| **`docker build` 进不了沙箱** | 沙箱容器没有 docker daemon / socket 接线；把 socket 交给它 = 交出宿主 root。所以这一步**只能在宿主**跑（`docker build` 与放哪个节点无关） |

最后一条是本次设计的支点：**squash 之后分支头在主干上是孤儿**，所以"要构建的 == 主干上的"
只能靠 `HEAD^{tree}` 与主干提交的 `tree` 比对，不能比 SHA。

---

## 2. 沙箱的余量

| 项 | 实测 | 上限 | 余量 |
|---|---|---|---|
| 构建耗时 | 8.5 s | 沙箱 `SBX_MAX_EXEC_SECONDS = 300`（硬顶） | 35× |
| 产物 | 35 MB | — | — |

`300` 是沙箱侧的硬顶（`exec_service` 取 `min(timeout, MAX_EXECUTION_SECONDS)`），
客户端传多大都没用 —— 所以工具的超时声明不要超过它，否则是自欺。

---

## 3. 设计决定

### D1. 构建在**沙箱**里跑，不是 worker

判据还是 §9.6 那句：**谁持有密钥，谁不执行不可信代码**。

编译执行的是仓库里的 `build.gradle` —— 那是一个**脚本**，和 `.git/hooks/pre-commit`
是同一类东西（本仓为后者专门加过 `core.hooksPath=/dev/null`）。而 worker 进程持有
DeepSeek key / DB DSN / git 凭证。

机制上没有任何新东西：`ws_run_tests_sandboxed` 就是 `sandbox.run_shell(cmd, cwd=repo)`，
换个命令而已。**未接线沙箱时 fail-closed 报错，绝不回退本地执行**（照既有约定）。

### D2. 命令来自**部署配置**，不接受调用方传参

新增 `AGENTFLOW_BUILD_CMDS`（JSON），形状照 `AGENTFLOW_TEST_CMDS`：

```json
{"order-service": "./gradlew clean bootJar --no-daemon -q",
 "warranty-service": "./gradlew clean bootJar --no-daemon -q",
 "gateway-service":  "./gradlew clean bootJar --no-daemon -q"}
```

配套一个 `build_cmd_for(service)`，**未配置即 raise**（fail-closed），不给默认值。

> 依据是 `test_cmd_for` 的判据（`workspace_tools.py:242`）：旧实现让 LLM 传自由命令、
> 再用前缀白名单去猜安不安全，而白名单里含 `"bash "` —— **等于没有白名单**。
> 改成"可执行命令的集合在部署时定死"之后，"白名单"这件事就不存在了。

### D3. `clean bootJar`，不是 `clean build`

`bootJar` **不依赖 `test`**（`test` 只是 `build` 的依赖）。用 `build` 会把 `test` 节点
刚跑过的那一遍再跑一次，白烧预算。

### D4. 产物留在工作区；`image_tag` 用**主干的** merge_commit

```
产物     : <workspace>/build/libs/order-service-0.0.1-SNAPSHOT.jar
image_tag: order-service:211eae4e2518        ← 主干那个 squash 提交的前 12 位
```

**不用工作区 HEAD 的 sha12** —— 它在 squash 之后是主干上**不存在的提交**，
拿它当 tag 等于给镜像挂一个查无此人的名字。用主干的 sha，tag 本身就是取证：
"构建的 == 合并的 == 部署的"，三者靠同一个字符串串起来。

工作区按 run 保留（`~/agentflow-workspace/<tenant>/<run>/`），不会被自动清理，产物留在那儿。

### D5. 构建前**比树**（这条要在工具里做，不能让模型自己判断）

```
工作区   git rev-parse HEAD^{tree}
主干     gh api repos/{o}/{r}/commits/{merge_commit} --jq .commit.tree.sha
不等  ⇒  raise：「要构建的与主干上的不是同一份代码」
```

只能走 `gh api`：`_GIT_ALLOWED` 没有 `fetch`（§8 版本冻结），取主干那边的对象只能经 gh ——
这正是 `ws_open_pr` 已有的形态。

> 这条**放在 ci 而不是 deploy**：构建与打包都发生在 ci，deploy 只是把产物搬进集群。
> 在最靠近"读代码"的那一步验一次，后面就不必重复。

### D6. 产物要在 **worker 侧复读**

沙箱写完，工具**在本进程里再读一次**，看不见就报错、绝不报成功。

这不是洁癖：§9.6 记着 `ws_write_file` 的那个形态 —— **沙箱把文件写进了容器自己的文件系统，
宿主侧什么都没变，而调用方收到一句"成功"**（`AGENTFLOW_WORKSPACE_ROOT` 与沙箱挂载卷不一致时）。
构建产物是整条链里最不该静默丢的东西，判据与 `_verify_visible_to_worker` 完全一致。

### D7. `ci-builder` **不进** `SIDE_EFFECT_AGENTS`；但 `built` 要进 `VERDICT_FIELDS`

- **不进副作用清单**：判据是 §5 那句「这个 agent 一旦重跑，外部世界会不会多一次可见的变化」。
  构建只在工作区产出一个文件，**对外不可见**。（对比：`deployer` 会滚 Deployment，
  `merger` 会动主干 —— 那才要。）重跑一次构建 8.5 秒，幂等键在这儿没有价值。
- **进结论字段**：`VERDICT_FIELDS += {"ci-builder": "built"}`。
  否则工具返回 `built: false` 而 agent 如实照报时，节点照样是 **DONE**，
  下游会继续拿着一个**根本没构建出来的产物**去部署。**加一个 agent 就是加一条映射。**

### D8. 两个工具分开放：**能不能进沙箱**决定它归哪个模块

`ci` 节点有两个工具，分属两个模块 —— 判据是 **"它能不能进沙箱"**：

| 工具 | 放哪 | 为什么 |
|---|---|---|
| `ws_build_artifact` | **`workspace_tools.py`** | 它和 `ws_run_tests` 是同一类东西：**在工作区里执行一条由部署配置给定的命令**。放进这儿才能顺理成章地拿到 `WORKSPACE_SANDBOXED` 那套接线（`build_workspace_tools` 注入 `sandbox_client` 时自动切到 `_sandboxed` 变体） |
| `ws_build_image` | **`release_tools.py`** | **它进不了沙箱** —— `docker build` 要 docker/podman daemon 与 socket，而"把 socket 交给沙箱"等于把宿主 root 交出去。所以它只能在宿主跑，与 `ws_merge_pr` 同属**发布链的宿主侧动作** |

`release_tools.py` 的定位因此从"对外动作"扩成"**发布链上、只能在宿主执行的动作**"——
模块 docstring 要跟着改。它俩都**不碰沙箱**，但 `ws_build_image` 走 `_run()`（宿主子进程 +
超时 + `stdin=DEVNULL`），`ws_build_artifact` 走 `sandbox.run_shell()`。

---

## 4. 形状

### 4.1 节点

```yaml
  # ===== CI：编译打包 → 构建镜像（本图第一次产出"可部署的物"）=====
  # ① 编译在**沙箱**里跑：执行的是仓库的 build.gradle（脚本），而 worker 持有全部密钥。
  # ② 构建镜像在**宿主**跑：docker build 要 daemon/socket，进不了沙箱（见 D8）。
  # 构建前先比树（工作区 HEAD^{tree} vs 主干那个提交的 tree）—— 证明"要构建的
  # == 主干上的"。squash 之后两个 SHA 不同，只比 SHA 会误判。
  #
  # 产物在**门之前**就定型了：approve-deploy 批的是"要把这个镜像滚上去"，
  # deploy 只搬运与滚动、不构建任何东西（build once, deploy many）。
  ci:
    agent: ci-builder
    description: "CI：编译打包 + 构建镜像，产出与主干一致的可部署产物"
    require: [service, merge_commit]
    params:
      merge: "$.nodes.merge.output"
      merge_commit: "$.nodes.merge.output.merge_commit"
      service: "$.inputs.bug_report.cmdb_ci.name"
    on_failure: abort
```

边：`merge → ci` + `ci → ticket-done`（删掉 `merge → ticket-done`）。

`require: [service, merge_commit]` 是标量锚点（同 `merge` 的 `pr_url`）：
`merge_commit` 解不出来就 fail-fast，而不是把一个 None 喂给工具去拼一个不存在的 tag。

### 4.2 工具 A：`ws_build_artifact(service, merge_commit)` —— 编译打包

三步，每步都在自己的位置 raise：

```
1. 比树      git rev-parse HEAD^{tree}  vs  gh api .../commits/{merge_commit} --jq .commit.tree.sha
             不等 ⇒ 「要构建的与主干上的不是同一份代码，拒绝」
2. 构建      沙箱里跑 AGENTFLOW_BUILD_CMDS[service]（未配置 ⇒ fail-closed）
             超时 / 非零退出 ⇒ 带 **尾部** 日志（gradle 的结论在末尾，同 _test_result 的规约）
3. 复读      在 worker 侧确认 build/libs/*.jar 真的存在 ⇒ 否则报
             「沙箱说构建成功，但 worker 侧看不到 —— 两侧挂的不是同一个工作区卷」
```

### 4.3 工具 B：`ws_build_image(service, merge_commit)` —— 构建镜像

```
1. 断言 jar 在（上一步的产物；不在 ⇒ 「产物不在，先跑 ws_build_artifact」）
2. docker build -t <service>:<merge_commit 前 12 位> <repo>       ← cwd = 那个仓，上下文就是它
3. 复读       docker image inspect <tag> 确认镜像真的在，回读它的 Id 与 Size
```

**两个工具都收 `(service, merge_commit)`、各自确定性地算出同一个 tag** ——
不需要模型在工具之间传 tag。让模型转述一个字符串（`image_tag`）是没必要的风险面：
它拼错一个字符，`deploy` 就会去找一个不存在的镜像。

`--network=none`：**待实施**（见 §3 D2 那条债的表）。

### 4.4 两个工具共同的返回契约

字段名要与 `AGENT_SCHEMAS["ci-builder"]` 和 YAML 里的引用**逐字一致**：

```python
# ws_build_artifact
{"built": True, "artifact": "build/libs/order-service-0.0.1-SNAPSHOT.jar",
 "artifact_bytes": 35285684, "merge_commit": "211eae4e251878…", "rc": 0,
 "log_tail": "...", "summary": "..."}

# ws_build_image
{"image_built": True, "image_tag": "order-service:211eae4e2518",
 "image_id": "sha256:…", "image_bytes": 35285684, "merge_commit": "211eae4e251878…",
 "summary": "..."}
```

节点输出（agent 汇总）：`image_tag` 是**下游唯一要用的字段**（D6 逐节点往下传）。

### 4.5 agent `ci-builder`

- **内置**（`registry.FIX_AGENTS` + `prompts.SYSTEM_PROMPTS` + `AGENT_SCHEMAS`），
  不做自定义 agent —— 与 `merger` 同一条理由（自定义 agent 的 prompt/schema 只在种子里，
  已开通租户要走 `sync-agents`，而那个脚本对**已有的行不动**）。
- `AGENT_STAGES["ci-builder"] = "deliver"`（与 `committer`/`merger` 同段）。
- 给**两个**工具，顺序固定：**先 `ws_build_artifact`，成功了再 `ws_build_image`**。
  提示词按 `ticket-done` / `merger` 的形状写死：「调工具 → 原样报回 → 最后只输出 JSON」；
  `built` / `image_built` 必须来自工具的真实返回，**不许因为"上一步成功了"就报 true**。
- `_MAX_ITERS` **不调**（两个工具、最多 3 轮，轮次不是瓶颈）。

---

## 5. 改动清单

| # | 文件 | 改什么 |
|---|---|---|
| 1 | `agentflow/config.py` | `build_cmds: str = ""`（照 `test_cmds` 的形状，**不给默认值**） |
| 2 | `.env` / `.env.example` / `.env.dev` | `AGENTFLOW_BUILD_CMDS` 三个服务的实际值 + 注释 |
| 3 | `agentflow/agents/workspace_tools.py` | `build_cmd_for()`；`ws_build_artifact` + `ws_build_artifact_sandboxed`；进 `WORKSPACE_TOOLS` |
| 4 | `agentflow/agents/release_tools.py` | `ws_build_image`（宿主 `docker build` + `docker image inspect`）；进 `RELEASE_TOOLS`；模块 docstring 从"对外动作"改成"发布链上只能在宿主执行的动作" |
| 5 | `agentflow/agents/tools.py` | `TOOL_REGISTRY` 加 `ws_build_artifact`（`agents:["ci-builder"]`）；`WORKSPACE_SANDBOXED` 加它；`build_workspace_tools` 的 sandboxed 映射加它；`TOOL_REGISTRY` 另加 `ws_build_image` |
| 6 | `agentflow/agents/registry.py` | `FIX_AGENTS` 加 `ci-builder`；`AGENT_STAGES`；`AGENT_DESCRIPTIONS`；docstring 17→18 |
| 7 | `agentflow/agents/prompts.py` | `SYSTEM_PROMPTS["ci-builder"]` + `AGENT_SCHEMAS["ci-builder"]` |
| 8 | `agentflow/agents/schemas.py` | `BuildResultSchema`（含 `image_tag` / `image_id`） |
| 9 | `agentflow/executor/dag_executor.py` | `VERDICT_FIELDS` += `ci-builder: built`（**判据取自 `ws_build_artifact`**；`image_built` 也由它兜着，见下） |
| 10 | `agentflow/service.py` | `WORKSPACE_AGENTS` += `ci-builder` |
| 11 | `agentflow/seed/workflows/problem-diagnose-fix.yaml` | `ci` 节点 + 两条边 + `ticket-done`/`recap` 的 params 加 `ci` |
| 12 | 测试 | 见 §6 |

> `agentflow/agents/mcp.py` **本来要改一处、但不用了**：`ws_build_image` 走 `release_tools`
> 那条线，而 `build_release_tools` 是 2026-09-24 为 `ws_merge_pr` 加进
> `_build_function_tools` 的 —— 它按 `tools_for_agent` 取，`ws_build_image` 只要进了
> `TOOL_REGISTRY` 就自动被组装。**如果 `ws_merge_pr` 那次没有接线，这里就也得改** ——
> `tests/test_agents.py::test_every_registered_tool_is_reachable_in_the_toolkit` 会抓住。
>
> ⚠️ **`VERDICT_FIELDS` 是 `agent → 单个字段` 的映射**（`{"tester": "passed", …}`），
> 一个 agent 放不下两个结论字段。所以 `ci` 节点的结论字段统一叫 **`built`**，
> 语义定义为「**产物就绪** = 编译打包**且**镜像构建都成功」：
>
> - `ws_build_artifact` 的 `built`（jar 成了没有）
> - `ws_build_image` 的 `image_built`（镜像成了没有）
> - 节点输出的顶层 `built` = **两者都为 true**
>
> 提示词把这条写死。这样 `built: false` 一条映射就兜住了两半，
> 不必让工具"抛错代替返回 false" —— §3.2 的判据要求结论判定在 `on_failure` **之外**，
> 靠抛错会被 `on_failure: continue` 吞成负证据、节点照样 DONE。

---

## 6. 验证

### 6.1 单元测试（放 `tests/test_workspace.py`，夹具都在那儿）

| 用例 | 断言 |
|---|---|
| 未配置 `AGENTFLOW_BUILD_CMDS` | fail-closed 报错（**不给默认命令**） |
| `service` 不在配置里 | 报错里列出已配置的服务 |
| 正常构建 | 沙箱被调用、`built=True`、产物路径对 |
| **树不匹配** | 在比树那一步就 raise，**沙箱一次都没被调** |
| 沙箱不可达 | 报错且**不回退本地执行**（照 `test_unreachable_sandbox_does_not_fall_back`） |
| 沙箱说成功、worker 侧看不到 | 报错（照 `test_sandbox_write_invisible_to_worker_is_an_error`） |
| 构建非零退出 | `built=False`、`log_tail` 保**尾部** |
| `ws_build_image` 正常 | `docker build` 的 argv 里 **tag = `<svc>:<merge_commit 前 12 位>`**、cwd = 那个仓 |
| `ws_build_image` 时 jar 不在 | raise「产物不在，先跑 ws_build_artifact」，**docker 一次都没被调** |
| `docker image inspect` 说镜像不在 | raise（同"复读"那条判据） |
| 两个工具算出的 tag 相同 | 对同一组 `(service, merge_commit)` **逐字相等** —— 这条锁的是"模型不必在工具之间传 tag"那个设计 |

### 6.2 工作流级（`tests/test_problem_diagnose_fix_workflow.py`）

- 节点集合加 `"ci"`；`ticket-done` 的入边从 `["merge"]` 改成 `["ci"]`
- `OK_OUTPUTS` 加 `"ci"`
- `ci` 的 `require == {"service", "merge_commit"}` 且 `merge_commit` 指向
  `$.nodes.merge.output.merge_commit`
- `VERDICT_FIELDS` 参数化用例自动 +1（`tests/test_executor.py:458` 跑在映射表上）

### 6.3 变异验证（附"变异真的生效"的证据：`grep -c` 归零 / diff）

- 去掉 `require` 里的 `merge_commit` → 6.2 的 fail-fast 用例必须红
- 把比树那步注释掉 → 6.1 的"树不匹配"用例必须红
- 把复读那步注释掉 → 6.1 的"worker 侧看不到"用例必须红

### 6.4 真机（先用干净副本，不烧 LLM）

照 §1 的配方再跑一次 `--network none`，确认**在 `AGENTFLOW_BUILD_CMDS` 那条命令下**同样能出 jar；
然后再走一次真实 run，看 `GET /runs/{id}` 上 `ci` 是不是 DONE、`image_tag` 是不是主干的 sha12、
jar 在不在工作区。

---

## 7. 本轮明确**不做**

- `approve-deploy` / `deploy` / `verify-deploy` 三个节点；
- **`minikube image load` / `kubectl set image`** —— 那两条**碰集群**，归 `deploy`
  （`ci` 的产物只到"宿主上的一个镜像"为止，`ci` 不碰集群）；
- **镜像仓库（registry）** —— 本轮 `docker build` 出来的镜像只留在宿主本地的
  docker/podman store 里，由 `minikube image load` 搬进集群。
  真形态应当 `docker push` 到 registry、集群自己 pull；那要先把 registry 和 k8s 侧凭证
  理清，是另一件事（也是 D2 那条债的一部分）；
- `docker build --network=none`（**待实施**，见 §3 D2 的表）；
- 多服务矩阵构建（本轮 `AGENTFLOW_BUILD_CMDS` 里三个服务各一条，够用）；
- **构建产物的保留策略** —— 现在跟着工作区一直留着，将来要不要清理是另一件事。
- **独立 CI runner**（那条债的正解）—— 见 `RELEASE_CHAIN_PLAN_zh-CN.md` D2 与 `docs/TODO.md`。
