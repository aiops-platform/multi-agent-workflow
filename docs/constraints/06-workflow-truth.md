# §6 工作流的真源 + 版本冻结 + 双队列

> **本文从 `CLAUDE.md` 拆出（2026-09-23）** —— 它此前**每个 session 都全量加载**，
> 而这份只在下面这个时机才需要。
>
> **触发条件**：改 workflow（含 seed）、动 resume、或动 Worker 队列模式时
>
> 索引在 `CLAUDE.md` 的「约束索引」；那里还有每条的一句话判据。
> ⚠️ 正文里的 `§N` 编号是**稳定标识**，全仓（README / 测试 / design / 源码注释）
> 有 14 处按它引用 —— 拆文件**不改编号**，正是为了不让那些引用断掉。

---

6. **版本冻结**（`core/workflow.py`）：Run 用 `workflow_hash` 复用 snapshot，Resume 只读原 snapshot。

6.1 ⚠️ **静态校验只拦「编写路径」，绝不拦「加载冻结数据」**（2026-09-20 血的教训）
   `DAG.build(..., strict=)` / `Workflow.load_yaml(..., strict=)`：`strict=True`（默认）
   跑全部静态校验，`strict=False` **只跑结构性校验**（环 / 悬空 / join），跳过
   `_check_on_reject_consistency` 这类**内容规则**。

   **三处加载冻结数据的地方必须传 `strict=False`**：
   `api/app.py` 的 run 列表（取 workflow 名）、run 详情（重建图）、
   **`executor/resume.py`（从 checkpoint 恢复）**。

   为什么是硬规则而不是风格：snapshot 是**冻结数据、永远不能被重新编辑**。
   加一条新校验却让它作用到快照上 = **用新规则判旧数据有罪**，后果是
   已经跑过的 run **永久读不出来**。实测代价（加 `on_reject` 校验后）：
   `GET /runs/{id}` 35 个 run 里 **30 个图变空白**，`resume` 直接抛异常。
   而 API 那处 `except: graph = {}` **把异常吞了**，症状表现为"前端图空白"，
   排查方向完全指错（已加 `log.warning(exc_info=True)`）。

   **判据**：这条校验拦的是「**还没保存的图**」还是「**已经跑过的 run**」？
   后者一律 `strict=False`。

6.0 ⚠️ **工作流的真源是数据库，不是仓库文件**（2026-09-16 起明确）
   - **存**：`POST /workflows` → `workflow_store.py` 的 `INSERT INTO workflows(id,name,yaml,created_at)`
   - **用**：run 时从库读——`api/app.py:697` `cs.workflow.list()` → `:704` `get(wid)`
     → `:398` `Workflow.load_yaml(wf_row["yaml"])`
   - **仓库里的 `workflows/*.yaml` 已于 2026-09-16 删除**，`agentflow/demo.py` 与
     `make demo` 一并删除——它们读的是仓库文件，**会让人以为改 YAML 就生效**。
     实际改了仓库 YAML 而没同步到库时，run 跑的还是旧流程，**且没有任何提示**。
   - **要改 workflow**：`PUT /workflows/{wid}`（或 `POST /workflows` 新建），改完立即生效
     （已发起的 run 不受影响——它们用 snapshot 冻结）。
   - ⭐ **改完 `seed/workflows/*.yaml` 之后，把改动同步到已开通租户：**

     ```bash
     make sync-workflows TENANT=otr          # 先 --dry-run 也行：DRY=1
     ```

     它按**名字**配对逐条推（命中 PUT 保 id、未命中 POST），推完**回读逐字节校验**，
     不一致就非零退出。**为什么需要这条**：seed 是「空表才播、绝不覆盖」，
     所以 `git pull` 对**已开通租户毫无效果、也没有任何提示**（§6.0 开头那条）。
     它**只同步 workflow** —— 数据面走下面那条。

   - ⭐ **数据面（MCP server 注册 + agent 绑定 + 自定义 agent）同理，走 `sync-agents`：**

     ```bash
     make sync-agents TENANT=otr             # 先 --dry-run 也行：DRY=1
     ```

     **语义是「并集，只加不删」**——租户自己加的 server 绑定会被保留（实测 otr 的
     `code-locator` 多绑了一个 `git-server`，脚本原样留着）。自定义 agent 则**缺行才建**，
     已有行不碰（prompt 是本地可改的）。
     ⚠️ **它挡的那类缺陷最隐蔽**：绑定缺行 ⇒ `mcp_server_ids` 为 NULL ⇒ 该 agent
     **零工具**（`GET /agents` 的 `tool_count` 直接看得出来），而它的提示词照旧点名要调
     MCP 工具——模型于是把工具调用写成**纯文本**，最终表现为「agent 未输出合法 JSON」，
     **中间没有任何一步会报"绑定缺失"**。实测 2026-09-22（`run_62f21fa82f`）：otr 缺 4 行，
     `service-scoper` 零工具 → `scope` 节点失败 → `on_failure: abort` **整条 run 中止**。
     判据一句话：**agent 的工具来自绑定，而绑定不在代码里。**
   - 原设计的 DAG 形态（节点类型 / when / join / 审批门禁）见 `design-v5.2.md` §8.1（**仓库上一级目录**，不在 `backend/docs/`——v5.6 §8 是「残余风险」不是这个）；
     当前流程的节点结构见 `docs/design-v5.8.md` §4（种子里的三条：scenario1 / scenario2 /
     problem-diagnose-fix，末者见 §4.15）。
   - **新租户的默认数据 = `agentflow/seed/`（种子，2026-09-18 起）**：租户库建好时，
     `TenantStoresRouter._build()` 会往**三张表**写默认数据，让新租户开箱可用——
     `workflows` + `mcp_servers` + `agent_configs`（agent↔server 绑定）。
     只播 workflow 不够：绑定为空 ⇒ **每个 agent 零工具**，run 会跑完但全在空转。
     - **语义：空表才播、绝不覆盖**（每张表各自判断）。所以**改了 seed 文件对已存在的租户
       没有任何效果**——改已开通租户仍走 `PUT /workflows/{wid}`。
     - id 一律 `seed-` 前缀（`save()` 产出 12 位 hex，永不撞），一眼可辨来源。
     - 逃生阀 `AGENTFLOW_SEED_DEFAULTS=0`；数据面 server 地址走
       `AGENTFLOW_MCP_DATASOURCE_URL`（URL 环境相关，不在种子里写死）。
     - 详见 `agentflow/seed/README.md`、`docs/TODO.md` §13。

6.1 **Worker/双队列**（§6/§8.6，`AGENTFLOW_RUN_MODE`）：`inline`（默认，进程内直跑）|
   `queue`（API 只发布 run.trigger.{tenant} / run.command.{tenant}，Worker 消费；
   memory=进程内 WorkerPool 自动接 active 租户，kafka=`python -m agentflow.worker`
   [--tenant <id>] [--dsn postgres://…]）。**--dsn**：容器/共享库直连单租户（管理库
   db_ref 的 localhost DSN 在 k8s 容器不可达）；**--tenant**：只消费该租户 topic。
   ⚠️ **`--dsn` 换的不只是 StateStore**：该 DSN 即 `mcp_servers`/`agent_configs` 所在
   的库，node_runner 要按**同一个 DSN** 把整套 bundle 建出来（`build_tenant_stores_at_dsn`）。
   只换 StateStore 会让 MCP 绑定与 agent 配置一起消失 → agent 零工具。两条路径共用
   `worker.build_node_runner`——**别让任何分支在它之前 return**（曾如此：容器形态下
   每个节点落到 `_default_runner`，不调 LLM、不调工具，而 run 照样报 done）。
   executor 一律经 `resume_executor` checkpoint 重建（`load_snapshot_workflow` 的
   `await` 不可删——曾缺失导致 resume 全挂）。queue 模式 approve 只做 CAS+发命令，
   零进程内 executor 依赖。pause=波间暂停。Worker 镜像 `docker/Dockerfile.worker` +
   manifest `deploy/worker-deployment.yaml`（本地 minikube 实操见 docs/DEPLOYMENT §2.3）。
