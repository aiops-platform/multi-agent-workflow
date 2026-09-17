# 端到端验证手册（PG + Kafka 生产形态）

> 目标：从**抓到一条 ticket** 开始，走完 **诊断 → 修复 → 审批 → 提交** 的完整流程，
> 并在前端看到每一步。本手册的每一步都在 2026-09-14 实机验证过。

## 一、架构与端口

```
┌ 前端 Vite :5173 ──┐
│                   │ /api/sip-aiops → 无（本流程不涉及）
│                   │ /agentflow      → :8000（剥前缀）
└───────────────────┘
        │
┌ API :8000 ────────┐  只发布命令，不执行（RUN_MODE=queue）
│  PostgreSQL :5432 │  管理库 agentflow（tenants/schema_versions）
│                   │  + **每租户独立库** agentflow-{tenant}（运行期表 + 控制面配置）
│  Redis      :6379 │  分布式锁（可选）
└───────────────────┘
        │ publish run.trigger.{tenant} / run.command.{tenant}
┌ Kafka :19092 ─────┐  双 listener：9092（容器内）/ 19092（宿主）
└───────────────────┘
        │ consume
┌ Worker（host 进程）┐  每租户一个：python -m agentflow.worker --tenant <id>
└───────────────────┘
        │ agent 取数经 MCP
┌ aiops-datasource-mcp-server :8300 ┐  7 个只读工具
└───────────────────────────────────┘
        │ 需要（本手册的完整诊断依赖）
┌ 测试床：ES :19200 · Prometheus :19090 · K8s(需 minikube) ┐
```

**为什么必须 PG+Kafka 才能验证完整流程**：`RUN_MODE=queue` 下 API 只发布、执行在 Worker。
用 sqlite+memory 虽然也能跑，但那样是多租户链路之外的单进程形态，验证不到
「API 发布 → Kafka → Worker 消费 → 落库」这条真实拓扑。

### 租户库隔离（2026-09-14 修复）

**每个租户一个独立 database**，命名 `{基础库}-{tenant_id}`（如 `agentflow-otr`），
管理库 `agentflow` 只放 `tenants` / `schema_versions`。

```
agentflow                 ← 管理库：tenants, schema_versions
agentflow-{tenant}        ← 每个租户一份：runs/nodes/workflows/mcp_servers/agent_configs/...
```

这条**不是**可有可无的整洁性要求：`workflows` / `mcp_servers` / `agent_configs`
三张控制面表**没有 `tenant_id` 列**（设计上靠"每租户一个库"的物理隔离）。库一旦共享，
这三张表就成了跨租户共享 —— 修复前实测：**一个从未开通的租户 id 能列出别人的 workflow**。

> 修复前 `_default_db_ref` 的 postgres 分支返回共享 DSN（且有三份副本），
> 只在 sqlite 下真正每租户一份文件。现已合并为一份实现，PG 对齐 sqlite 语义。

**本机现状**（2026-09-14）：租户为 **`otr`**，修复前开通的 `local` / `team-*` 已全部注销、
数据已清。当前只有两个库：

```bash
$PY -c "
import psycopg
with psycopg.connect('postgresql://localhost:5432/agentflow?user=agentflow&password=agentflow') as c:
    print([r[0] for r in c.execute('SELECT datname FROM pg_database WHERE datistemplate=false ORDER BY datname')])
"   # → ['agentflow', 'agentflow-otr', 'postgres']
```

> **对比存量租户**：若你手上还有修复**之前**开通的租户（db_ref 指向共享库 `agentflow`），
> 它们自己尚未被隔离 —— 需 `provision <tenant> --force` 重绑到独立库，
> 且**旧数据不自动搬迁**（见 §6.7）。本机已用「新建 `otr` + 注销旧租户」的方式规避。

## 二、启动顺序（严格按序）

### 0) 依赖（一次性）

```bash
PY=/opt/miniconda3/bin/python        # 本机后端用的解释器（3.12）
$PY -m pip install 'psycopg[binary]' kafka-python
```

> `psycopg` / `kafka-python` **不在** pyproject 的 dependencies 里，postgres/kafka 模式必须手动装。

### 1) 中间件（podman compose）

```bash
podman machine start podman-machine-v5
cd <repo>/backend
export DOCKER_HOST=unix:///var/run/docker.sock     # docker CLI 走 podman socket
docker-compose up -d postgres kafka redis
```

验证：

```bash
docker ps --format "{{.Names}} {{.Status}}"       # 三个 Up
$PY -c "import psycopg;psycopg.connect('postgresql://agentflow:agentflow@localhost:5432/agentflow');print('PG ok')"
```

### 2) 租户开通（幂等，已开过可跳过）

```bash
$PY -m agentflow.tenantctl provision otr
```

**首次开通**的输出（会真的建库，`CREATE DATABASE … TEMPLATE template0`）：

```
🆕 已创建租户库 postgresql://localhost:5432/agentflow-otr?user=…&password=…
✅ provision otr: isolation=standard db=postgresql://…/agentflow-otr namespace=agentflow-otr schema=2026-09-09.1
```

**已存在且 status=active 时幂等跳过**（不加 `--force` 不会改 db_ref）：

```
[tenantctl] 租户 otr 已存在（status=active），幂等跳过（--force 重写）
```

> 本机的 `otr` 是**修复后**开通的，已在自己的独立库里。上面的命令是幂等跳过；
> 若库里没有它会真的建库。

验证库是否建出来了：

```bash
$PY -c "
import psycopg
with psycopg.connect('postgresql://localhost:5432/agentflow?user=agentflow&password=agentflow') as c:
    print([r[0] for r in c.execute('SELECT datname FROM pg_database WHERE datistemplate=false ORDER BY datname')])
"
```

> 本机租户名是 **`otr`**。dev 模式（`AGENTFLOW_JWT_SECRET` 为空）下 API 从
> `X-Tenant-ID` 头取租户；**前端已恒带该头**（`src/api/agentflow.js`）。
> 后端缺省回退到 `"local"`（`api/auth.py:61`）—— 那会触发**给未注册租户自动建空库**，
> 所以手工 curl 时**别漏这个头**（见 §6.10）。

**`--force` 会重新绑定 db_ref**。对**已存在**的租户执行时，若目标库与旧库不同，
会打印警告 —— **旧库数据不会自动搬迁**，仍留在原处（只是不再被读）。见 §6.7。

**`--db-dsn`**：strong 隔离要指向**独立实例**时显式指定（默认两种隔离都落在
同一个 PG 实例的不同 database 上）：

```bash
$PY -m agentflow.tenantctl provision team-acme --isolation strong \
    --branch tenant/team-acme --db-dsn 'postgresql://other-host:5432/agentflow?user=…&password=…'
```

### 3) MCP server

```bash
cd <workspace>/aiops-mcp-servers/servers/aiops-datasource-mcp-server
uv run python -m aiops_datasource_mcp_server      # :8300
```

验证：日志出现 `registered 7 tools: query_logs, ...`。

> **必须先起它**。7 个 agent（triage / log-analyst / trace-analyst / infra-locator /
> root-cause / metrics-analyst / code-locator）在库里已绑定该 server；
> server 不起 → `list_tools 失败` → **agent 节点直接执行失败**。

### 4) API

```bash
cd <repo>/backend
$PY -m uvicorn agentflow.api.app:app --port 8000
```

验证：`curl localhost:8000/health` → `{"status":"ok",...}`

> `.env` 已含 `STATE_STORE=postgres` / `QUEUE=kafka` / `RUN_MODE=queue` / `KAFKA_BOOTSTRAP=localhost:19092`，无需额外覆盖。

### 5) Worker（每租户一个）

```bash
cd <repo>/backend
$PY -m agentflow.worker --tenant otr
```

验证日志：

```
Worker(tenant=otr)：消费 run.trigger.otr / run.command.otr
kafka.net.connection ... Connected
```

### 6) 前端

```bash
cd <workspace>/service-intelligence-platform-ui
npm run dev                                        # :5173
```

## 三、完整验证步骤

### 3.1 准备：入库一个 workflow

```bash
cd <repo>/backend
$PY -c "
import json,pathlib,urllib.request
y=pathlib.Path('<workflow 的 YAML 文本文件>.yaml').read_text()  # 仓库 workflows/ 已删（2026-09-16），YAML 文本请自备或从 GET /workflows 取
req=urllib.request.Request('http://localhost:8000/workflows',
  data=json.dumps({'name':'bug-fix-scenario2','yaml':y}).encode(),
  headers={'Content-Type':'application/json','X-Tenant-ID':'otr'})
print(json.load(urllib.request.urlopen(req)))
"
```

### 3.2 界面操作（推荐路径）

1. 打开 **http://localhost:5173/tickets**
2. 点右上 **+ New Ticket**，填：
   - 摘要：`订单服务结账无响应`
   - 服务：`order-service`，namespace：`order`
   - 时间窗：保持默认（最近 1 小时）
3. 点 **创建** → 列表出现该工单（状态 `new`）
4. 对该行点 **发起诊断**
   - 前端调 `POST /tickets/{tid}/run` → 建 run → 跳 `/runs/{runId}`
   - 此时状态为 `queued`（**API 只发布了消息**）
5. 观察 Run 详情页自动刷新（2 秒轮询）：
   - `queued` → `running`：**Worker 接单了**（Kafka 生效）
   - DAG 节点逐个变绿；右侧详情自动跟随当前执行节点
   - 底部节点清单累积耗时 / tokens / cost
6. 流程跑到 **`waiting_approval`**，右侧出现**待审批卡片**（含上游 `review` 的输出）
7. 点 **通过** → 审批经 Kafka 命令通道回传给 Worker → `commit` 节点执行
8. 最终 **`success`**，15 个节点全部 done

### 3.3 命令行等价路径（便于脚本化）

```bash
# 建工单（T 变量 = 租户头，见下）
T='X-Tenant-ID: otr'
tid=$(curl -s -X POST localhost:8000/tickets -H 'Content-Type: application/json' -H "$T" -d '{
  "title":"订单服务结账无响应",
  "bug_report":{"number":"INC0012345","short_description":"订单服务结账无响应",
                "cmdb_ci":{"name":"order-service","namespace":"order"}},
  "window_start":"2026-08-19T14:00:00","window_end":"2026-08-19T15:00:00"
}' | $PY -c "import sys,json;print(json.load(sys.stdin)['id'])")

# 发起（未指定 workflow_id → 用库里第一个）
rid=$(curl -s -X POST localhost:8000/tickets/$tid/run -H 'Content-Type: application/json' -H "$T" -d '{}' \
  | $PY -c "import sys,json;print(json.load(sys.stdin)['run_id'])")

# 轮询
watch -n3 "curl -s -H \"$T\" localhost:8000/runs/$rid | $PY -c \"
import sys,json;d=json.load(sys.stdin);n=d['nodes']
print(d['status'], len(n), sum(1 for x in n.values() if x['status']=='done'))\""

# 审批
curl -s -X POST localhost:8000/runs/$rid/approve -H 'Content-Type: application/json' -H "$T" \
  -d '{"node_id":"approve-commit","by":"lead-engineer","comment":"ok"}'
```

## 四、验收点（逐条对照）

| # | 验收点 | 期望 | 怎么查 |
|---|---|---|---|
| 1 | Kafka 真的在用 | Worker 日志有 `Worker 接单（trigger）` | `grep 接单 /tmp/worker.log` |
| 2 | 落库是 PG | `runs` 表有该 run | 见下方 SQL |
| 3 | 取数走 MCP | Worker 日志无 `list_tools 失败` | `grep list_tools` |
| 4 | 审批链路 | 通过后 `commit` 节点执行 | 前端 / `GET /runs/{id}` |
| 5 | 全流程成功 | `status=success`，15/15 done | 同上 |
| 6 | 界面完整 | 见下表 | 前端 |
| 7 | **租户库隔离** | 每个租户只看得到自己的配置 | 见下 |

**工单存储位置验收**（2026-09-14 修复项）：

工单必须和 run 一样落在**租户库**，不能落管理库。

```bash
$PY -c "
import psycopg
for db in ['agentflow','agentflow-otr']:
    with psycopg.connect(f'postgresql://localhost:5432/{db}?user=agentflow&password=agentflow') as c:
        has = c.execute(\"SELECT count(*) FROM pg_tables WHERE schemaname='public' AND tablename='tickets'\").fetchone()[0]
        rows = list(c.execute('SELECT tenant_id,title FROM tickets')) if has else []
        print(f'  {db:16} {rows}')
"
```

期望：`agentflow`（管理库）**为空**，`agentflow-otr` 有该工单。

> 修复前 `otr` 的 run 落 `agentflow-otr` 而**工单落到 `agentflow`** —— `ticket_store`
> 曾是模块全局、不在 `TenantStores` bundle 里，所以永远不被租户路由。

**租户隔离验收**（§一「租户库隔离」）：

```bash
# 用两个不同租户 id 列控制面配置 —— 必须互不可见
for t in otr some-unprovisioned-id; do
  echo -n "  $t: "
  curl -s -H "X-Tenant-ID: $t" localhost:8000/workflows \
    | $PY -c "import sys,json;d=json.load(sys.stdin);print(f'workflows={len(d)}', [x['name'] for x in d])"
done
```

期望：`otr` 有它自己的 workflow；`some-unprovisioned-id` **为 0**
（该 id 会得到一个空的独立库，而不是看到 `otr` 的数据）。
修复前这里两边会返回**同一份列表**。

**参考实现度量**（2026-09-14 实测，供对照）：

```
status=success   节点 15/15
tokens 334,847   cost $0.0306   节点耗时合计 157.7s
commit  → base_sha=635244123e014a38db892b16f958e5bda5081eed
```

查 PG：

```bash
$PY -c "
import psycopg
with psycopg.connect('postgresql://agentflow:agentflow@localhost:5432/agentflow') as c:
    for r in c.execute('SELECT run_id,status,tenant_id FROM runs ORDER BY created_at DESC LIMIT 5'):
        print(r)
"
```

## 五、界面上的验收点

| 页面 | 路径 | 看什么 |
|---|---|---|
| Ticket Inbox | `/tickets` | KPI 计数、状态筛选、发起诊断按钮 |
| Run 详情 | `/runs/{id}` | 15 节点 DAG、Captured Ticket 卡、节点清单的**耗时/tokens/重试次数/错误**、待审批卡片 |
| Agent 目录 | `/agent-registry` | 周期表 15 格、tier 汇总 9/2/2/2、Inspector 的**工具徽章**（MCP 生效后 triage 有 8 个） |

## 六、已知问题与排障

### 6.1 租户的 db_ref 解不开（噪音，不阻断其它租户）

`tenants` 表里**任何一个**租户的 `db_ref_enc` 若与当前 `AGENTFLOW_SECRET_KEY` 不匹配
（换过密钥 / 在别的环境创建），API 与 Sweeper 遍历到它时就会报：

```
ValueError: db_ref 解密失败（密钥不匹配？）
```

**只影响日志与那个租户**：其余租户完全正常，可继续验证流程。

> 本机曾出现 4 个这样的租户（team-alpha / team-a / team-b / team-x，早于每租户建库的
> 修复），**已于 2026-09-14 全部注销并清理**。保留本条是因为换密钥是常见操作，
> 换完就会重现 —— 成因见 §8.5。

清掉（幂等，逐个）：

```bash
$PY -m agentflow.tenantctl deprovision <tenant> --confirm-delete
# 若解密失败导致 deprovision 也报错，直接删管理库里那一行：
#   DELETE FROM tenants WHERE tenant_id IN (...)
#   DELETE FROM schema_versions WHERE tenant_id IN (...)
```

> 这些租户同时是**旧库绑定**（db_ref 指向共享库 `agentflow`，见 §6.7）。
> 若还想用它们，`provision <t> --force` 会重绑到各自的独立库（旧数据不迁）。

### 6.2 节点 `执行失败（重试耗尽）: unhandled errors in a TaskGroup`

**几乎总是 MCP server 没起**。这条错误经过 anyio TaskGroup 包装，看不出根因；
Worker 日志里紧邻的一行才是真因：

```
agentflow.mcp_manager WARNING MCP[aiops-datasource] list_tools 失败，跳过 allow 生成: ...
```

检查：`lsof -i:8300` + 冒烟 `curl -s localhost:8300/mcp`。

### 6.3 `ModuleNotFoundError: No module named 'kafka'` / `'psycopg'`

没装生产依赖。见 §二.0。

### 6.4 run 一直 `queued` 不前进

Worker 没在跑，或没消费对租户：

```bash
pgrep -f agentflow.worker
grep "消费 run.trigger" /tmp/worker.log     # 确认租户名一致
```

### 6.5 改完 agent 配置 / 绑了 MCP server / **改了 `prompts.py`** 后不生效

Worker 是独立进程。配置热载走库内指纹（`AGENTFLOW_CONFIG_REFRESH_SEC`，默认 5s），
但 **MCP 连接本身**在进程内缓存 → **重启 Worker**。

#### ⚠️ 改了代码里的 `SYSTEM_PROMPTS` / `AGENT_SCHEMAS` **必须重启 Worker**

**热载只看数据库的行，看不到代码。** `config_sync.py` 的指纹是
`(行数, MAX(updated_at))`——**改 `prompts.py` 不会让它变化**，于是：

- Worker 不重建 resolver
- 而 `SYSTEM_PROMPTS` 是 **import 时的模块级字典**，进程不重启就不会更新

**症状极隐蔽**：API 侧（`uvicorn --reload`）会重新 import，所以
`GET /agents/{name}` **能看到新 prompt**——你以为改对了；而 Worker 里跑的还是**旧 prompt**，
run 的行为完全没变。**两侧不一致，但没有任何报错。**

> **实测踩过（2026-09-17）**：给 `service-scoper` 加了四个输出字段并改了两轮 prompt，
> 跑两个场景都发现"字段没输出"。查 trace 里发给模型的 system prompt 才发现——Worker 里
> 的还是 **1728 字符的旧版**（连 `business_paths` 这个词都没有），而新版是 3427 字符。
> 字段从来没被要求过，当然不会出现。

**判据**：看 trace 里 `kind=llm_call` 的 `payload.messages[0].content[0].text`，
与 `GET /agents/{name}` 返回的 `system_prompt` **逐字对比**。不一致就是 Worker 旧了。

```bash
pkill -f "agentflow.worker"
cd <backend> && nohup ./venv/bin/python -m agentflow.worker --tenant otr > /tmp/worker.log 2>&1 &
```

### 6.6 `pr_url` 为空

`commit` 节点产出了 `base_sha` 但 `pr_url=""` —— 当前工作区没有配置可推送的远端，
PR 是 stub。这是设计内的（见 TODO「审批通知/沙箱等生产化」），**不影响流程验收**。

### 6.7 `--force` 重新开通后旧数据"不见了"

`provision --force` 会按新规则重新绑定 db_ref。对**早于每租户建库**的租户
（它们的 db_ref 指向共享库 `agentflow`）执行时，会切到自己的新库 —— 新库是空的，
**旧库里的 run/workflow 不会自动搬迁**：

```
[tenantctl] ⚠ 租户 otr 的库引用已变更：
            旧 → postgresql://…/agentflow?…
            新 → postgresql://…/agentflow-otr?…
            旧库数据**不会**自动搬迁，仍保留在原处。
```

数据没丢，还在 `agentflow` 库里。要保留历史，先手工搬：

```bash
# 例：把某租户的运行历史从共享库搬到它自己的库
pg_dump --data-only --table=runs --table=nodes --table=approvals \
        --table=workflow_snapshots --table=node_traces --table=audit_logs \
  'postgresql://…/agentflow' | psql 'postgresql://…/agentflow-otr'
```

> 注意 `workflows` / `mcp_servers` / `agent_configs` 在旧共享库里**没有 tenant_id 列**，
> 无法区分归属，搬迁时需自行判断哪些属于本租户。

### 6.8 建库报 `ObjectInUse: source database "template1" is being accessed by other users`

`CREATE DATABASE` 默认从 `template1` 克隆，而 **template1 允许连接** ——
只要有人（哪怕是个忘关的 `psql`）连着它就会失败。

本仓已用 `TEMPLATE template0` 规避（template0 从不接受连接）。若你在别处手写
`CREATE DATABASE` 遇到它，同样是加 `TEMPLATE template0`。

排查谁占着：

```bash
$PY -c "
import psycopg
with psycopg.connect('postgresql://…/postgres') as c:
    for r in c.execute(\"SELECT pid,usename,application_name FROM pg_stat_activity WHERE datname='template1'\"):
        print(r)
"
```

### 6.9 手拼 DSN 报 `password authentication failed`（即使密码是对的）

本仓的 PG DSN 把凭据放在 **query** 里：
`postgresql://host:5432/agentflow?user=…&password=…`。

用字符串拼接换库名（`prefix + "/" + dbname`）会把库名拼到 query **之后**，
得到畸形串、整串被当作密码解析 → 报密码错，**排查方向完全指错**。

一律用 `agentflow.tenants.dsn_with_db()` / `db_name_of()`（内部走 `urlsplit/urlunsplit`）。

### 6.10 未注册的租户 id 会自动建库

若 API 收到库里没有的 `X-Tenant-ID`，会**为它自动建一个空库**（与 sqlite 自动建文件一致）。
开发方便，但意味着任意 header 值都会产生数据库。

生产用 JWT 模式（`AGENTFLOW_JWT_SECRET` 非空）时租户来自签名 claim，不受影响。
若仍想收紧成"未注册即拒绝"，改 `router._resolve_ref` 即可。

### 6.11 ⚠ podman VM 时钟漂移 → 全链路时间戳偏移（**静默毁掉诊断**）

> 2026-09-14 实测：VM 比宿主机慢 **整 1 小时**。这是**最难联想的坑**——没有任何报错，
> 表现只是"查不到数据"。

**成因**：minikube 跑在 podman machine 里（docker driver 用 podman socket），
macOS 宿主休眠/唤醒后 VM 时钟可能不跟随，而 VM 内 `chronyd` 状态是 `active` 却
`System clock synchronized: no`（看着正常，实际没同步）。

**影响面（全链路）**：

| 层 | 症状 |
|---|---|
| Prometheus | 所有样本时间戳落后 1 小时 → Grafana「最近 15 分钟」**全空**（但 instant 查询能查到，极易误判成"数据源坏了"） |
| Elasticsearch | 日志 `app.@timestamp` 同样落后 → **诊断 agent 按窗口取数取不到故障日志** |
| K8s | Pod 时间戳、事件时间全部偏移 |

**症状之所以像"没问题"**：数据都在，只是**在错误的时刻**。Grafana 面板显示 No data，
但直接 curl Prometheus 的 instant 接口能拿到值——因为它取的是"最后一个点"，不看窗口。

**诊断（一条命令）**：

```bash
printf "  host: %s\n" "$(date -u '+%F %T')"
printf "  vm  : %s\n" "$(podman machine ssh podman-machine-v5 'date -u "+%F %T"' | tr -d '\r')"
```

两者不一致就是它。

**修复**：

```bash
podman machine ssh podman-machine-v5 "sudo date -s '$(date '+%Y-%m-%d %H:%M:%S')'"
```

> VM 的时区是 **UTC**（`timedatectl` 显示 `Time zone: n/a (UTC, +0000)`），但传本地时间
> 字符串给 `date -s` 也能正确落位（实测同步后双方 `date -u` 一致）。同步完等 ~20s
> 让 Prometheus 采到新样本。

**预防**：宿主长休眠后、做 E2E 之前，先跑一次上面那条诊断。**别等"查不到数据"再回头找**
——届时你会先怀疑数据源、查询语句、时间窗参数，最后才想到时钟。

**与代码无关**：这是环境问题，仓库里没有任何东西能修它。

## 七、一键冒烟脚本

```bash
#!/usr/bin/env bash
set -e
PY=/opt/miniconda3/bin/python
B=http://localhost:8000

for p in 5173 8000 8300 5432 6379 19092; do
  lsof -nP -iTCP:$p -sTCP:LISTEN >/dev/null || { echo "✗ 端口 $p 未监听"; exit 1; }
done
pgrep -f agentflow.worker >/dev/null || { echo "✗ Worker 未运行"; exit 1; }
echo "✓ 依赖服务齐备"

# 时钟一致性（§6.11）：VM 落后会让 Prometheus/ES 的时间戳整体偏移、
# 窗口查询静默取不到数据。放在最前面，避免跑到最后才发现"全空"。
if command -v podman >/dev/null; then
  h=$(date -u +%s); v=$(podman machine ssh podman-machine-v5 'date -u +%s' 2>/dev/null | tr -d '\r')
  if [ -n "$v" ]; then
    d=$((h - v)); d=${d#-}
    [ "$d" -gt 60 ] && { echo "✗ 时钟不一致：host 与 podman VM 相差 ${d}s（见 §6.11）"; exit 1; }
    echo "✓ 时钟一致（偏差 ${d}s）"
  fi
fi

curl -sf $B/health >/dev/null && echo "✓ API"
curl -sf $B/workflows | grep -q '\[.\]' || { echo "✗ 库里没有 workflow"; exit 1; }
echo "✓ workflow 已入库"

# 租户库隔离：未注册的租户 id 不能看到 otr 的配置
mine=$(curl -s $B/workflows | $PY -c "import sys,json;print(len(json.load(sys.stdin)))")
theirs=$(curl -s -H 'X-Tenant-ID: smoke-probe-unprovisioned' $B/workflows \
  | $PY -c "import sys,json;print(len(json.load(sys.stdin)))")
[ "$theirs" = "0" ] || { echo "✗ 租户隔离失败：外部租户看到 $theirs 条 workflow"; exit 1; }
echo "✓ 租户库隔离（otr=$mine, 探针租户=0）"

tid=$(curl -s -X POST $B/tickets -H 'Content-Type: application/json' -d '{
  "title":"smoke","bug_report":{"number":"SMOKE-1","short_description":"smoke",
  "cmdb_ci":{"name":"order-service"}},
  "window_start":"2026-08-19T14:00:00","window_end":"2026-08-19T15:00:00"}' \
  | $PY -c "import sys,json;print(json.load(sys.stdin)['id'])")
rid=$(curl -s -X POST $B/tickets/$tid/run -H 'Content-Type: application/json' -d '{}' \
  | $PY -c "import sys,json;print(json.load(sys.stdin)['run_id'])")
echo "✓ 已发起 run: $rid"

for i in $(seq 1 40); do
  s=$(curl -s $B/runs/$rid | $PY -c "import sys,json;print(json.load(sys.stdin)['status'])")
  echo "  [$i] $s"
  [ "$s" = "success" ] && { echo "✓ 全流程成功"; exit 0; }
  [ "$s" = "failed" ]  && { echo "✗ run 失败，查 Worker 日志"; exit 1; }
  [ "$s" = "waiting_approval" ] && \
    curl -s -X POST $B/runs/$rid/approve -H 'Content-Type: application/json' \
      -d '{"node_id":"approve-commit","by":"smoke"}' >/dev/null
  sleep 10
done
echo "✗ 超时"; exit 1
```

## 八、JWT 模式（鉴权与租户身份）

### 8.1 只有两种鉴权模式

`auth_mode` 全仓只有两个取值（`api/auth.py`）：

| 模式 | 触发 | 租户来自 | 身份来自 |
|---|---|---|---|
| **dev** | `AGENTFLOW_JWT_SECRET` **为空** | `X-Tenant-ID` 头（缺省 `local`） | body 的 `by` |
| **jwt** | `AGENTFLOW_JWT_SECRET` **非空** | token claim | token 的 `sub` |

没有 API Key / OAuth / cookie 模式。（`AGENTFLOW_DEEPSEEK_API_KEY`、`OPEN_SANDBOX_API_KEY`、
MCP 的 `AUTH_TOKEN` 分别是**其他服务**的凭据，不是 agentflow 的请求鉴权。）

**平台没有用户表**——它只认 `tenant_id`（决定数据落到哪个库）与 `sub`（审批人标识）。
「用户属于哪个租户」由**签发方**保证，平台不做映射也不校验合理性。

### 8.2 租户 claim 派生（优先级）

`tenant_id` > `org_id` > `org`。实测行为（2026-09-14）：

| 场景 | 结果 |
|---|---|
| `tenant_id=local` | ✅ 200，用 `local` |
| 只有 `org_id=local` | ✅ 200，用 `local` |
| 只有 `org=local` | ✅ 200，用 `local` |
| `tenant_id=local` + `org_id=OTHER` | ✅ 200，**`tenant_id` 胜出** |
| 无任何租户 claim | ❌ 401 `JWT 缺少租户 claim（需 tenant_id / org_id / org 之一）` |
| 已过期 | ❌ 401 `JWT 已过期` |
| 错误密钥签发 | ❌ 401 `JWT 无效: Signature verification failed` |
| 无 token | ❌ 401 `缺少 Authorization: Bearer <JWT>` |

**客户端提交一律被忽略**（design §9.1）——拿 `tenant_id=local` 的 token 再伪造
`X-Tenant-ID: team-a`，仍解析为 `local`。审批人同理：JWT 模式下 `by` 取 `sub`，
body 传什么都不生效（`app.py:1025`/`1048`）。

### 8.3 签发：本仓不做，需外部提供

```bash
$ grep -rn "jwt.encode" agentflow/     # → 无命中，只有 pyjwt.decode
```

联调时手动签一个：

```python
import jwt, datetime
token = jwt.encode({
    "sub": "zhangsan",              # 审批人身份
    "tenant_id": "local",           # 决定租户
    "exp": datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=8),
}, SECRET, algorithm="HS256")       # RS256 时 SECRET=私钥 PEM
```

### 8.4 RS256 已可用（"暂缓"指的不是算法）

`jwt_algorithm` 是算法无关的 `pyjwt.decode`，**RS256 配置即可用**（实测通过）：

```bash
export AGENTFLOW_JWT_SECRET="$(cat jwt_public.pem)"   # ← 公钥
export AGENTFLOW_JWT_ALGORITHM=RS256
```

**收益**：HS256 下控制面既能验签又能"伪签发"（谁拿到 secret 就能伪造任意租户 + 任意审批人）；
换 RS256 后签发方持私钥、控制面只持公钥，**这个洞用配置就能关掉**。

**尚未实现**（这才是"暂缓"的部分，v5.3 §12 待办 2）：JWKS 自动取钥/按 `kid` 匹配/轮换、
签发侧、以及 PEM 从文件装载（现在只能 `"$(cat …)"` 塞环境变量）。
详见 `DEPLOYMENT_zh-CN.md` §5.1。

### 8.5 ⚠ 切 JWT 前必须确认：`AGENTFLOW_SECRET_KEY` 稳定

```python
# api/management_store.py: derive_secret_key
if settings.secret_key:  return settings.secret_key.encode()          # 显式 → 用它
if settings.jwt_secret:  return b64(sha256(jwt_secret))               # 缺省 → 从 jwt_secret 派生
```

`SECRET_KEY` 负责 Fernet 加密租户库的 `db_ref`。**若它没显式设置，就会从 `jwt_secret`
派生 —— 那么改一次 `jwt_secret`，所有租户的 `db_ref` 全部解不开。**

本机 `tenants` 表里 `team-alpha`/`team-a`/`team-b`/`team-x` 报
`db_ref 解密失败（密钥不匹配？）` 就是这个原因：它们在**另一个 jwt_secret** 的环境下创建。

**结论**：切 JWT 模式前，先确认 `AGENTFLOW_SECRET_KEY` **已显式设置且永不更改**（进
K8s Secret / Vault，不入 git）。本机 `.env` 已满足。

### 8.6 切换步骤

```bash
# 1) 确认 SECRET_KEY 显式且稳定（见 §8.5）
grep AGENTFLOW_SECRET_KEY .env

# 2) 配 JWT（HS256 最简）
export AGENTFLOW_JWT_SECRET="$(python -c 'import secrets;print(secrets.token_urlsafe(48))')"

# 3) 重启 API —— 启动时不再打 dev 告警
# 4) 冒烟：无 token 必须 401
curl -s -o /dev/null -w "%{http_code}\n" localhost:8000/workflows     # → 401

# 5) 前端接入 Bearer 头（当前前端不发，见 src/api/http.js 的 headers 支持）
```

> **建议**：先完成存量租户的库迁移（§一「存量租户需迁移」）再切 JWT。
> 否则"哪个租户"同时受「旧库绑定」与「claim 派生」两层影响，排查会很难。

---

## 九、双场景 agent 行为验证（2026-09-17）

> 目的：不只看"run 是否 success"，而是**逐节点核对它有没有按设计的方式工作**。
> 两个场景连跑，每次记录每个节点的行为与判定。

**跑法**：工单从 `POST /tickets` 建 → `POST /tickets/{tid}/run` 触发 → 读
`GET /runs/{id}/traces` 逐节点核对（`kind=node` 的 `payload` 里有 `input`/`output`/
`tool_steps`，`kind` 其它的是工具调用记录）。

### 9.1 数据形状决定了考什么

| | 场景 1 | 场景 2 |
|---|---|---|
| 故障 | 报价单打印失败（单服务基础设施） | 结账无响应（**跨服务代码故障**） |
| ES 里谁报错 | order-service 16 条 | **warranty-service 2 条** |
| **症状服务自己报错吗** | 报 | **不报**（order-service 挂起，零 ERROR） |
| 工单 `cmdb_ci` | **空**（考 scope 的图定位） | `order-service`（症状服务，**考交叉判断**） |

> ⚠️ **切换场景前必须清 ES 窗口**（`curl -X DELETE :19200/app-logs`），
> 否则上一次的日志会污染这一次——两个场景的症状完全不同，混在一起没法判断谁是谁。

### 9.2 结果：**全部关键路径按预期工作**

| 节点 | 验证点 | 场景 1 | 场景 2 |
|---|---|---|---|
| `triage` | 零工具调用、`summary` 不含服务名/根因 | ✅ `tool_steps: 0` | ✅ |
| `scope` | 定位到正确服务 | ✅ 靠 CMDB 关键词「报价」命中（工单没给服务名） | ✅ 用 `cmdb_ci`，且**只调图工具** |
| `log-analyst` | 挑对错误（根因类，非症状） | ✅ `IOException: No space left on device` | ✅ **跨服务找到根因**：逐个查候选服务，在 warranty-service 找到 `IllegalArgumentException: 必填参数 fin 没有传` |
| `trace-analyst` | 用工单的 `trace_id` | ✅ | ✅ 且写明「feign 超时只是表面症状」 |
| `rca` | 交叉判断 `scope_primary` vs `failing_service` | ✅ 明写「**两者一致，相互印证**」 | ✅ 明写「**症状服务定位分歧**」——正确定根因在 warranty-service |
| 输出契约 | 13 个节点对各自 schema | ✅ 12 个合规 | ⚠️ 同左（只有 `scope` 违反） |

**最有价值的一条**：场景 2 里 `order-service` **自己零错误日志**，
`log-analyst` 是靠"逐个查上游给的候选服务"把 warranty-service 的那条稀有根因捞出来的；
`rca` 随后明确识别出「症状服务 ≠ 根因服务」。**这是 design-v5.7 §6 想要的交叉验证，
实测成立。**

### 9.3 ⚠️ agent 越界调用工具 —— **结论已更正**

> ⚠️ **本节首版结论是错的**。当时把下面这些"越界"当成"prompt 禁止不住行为"的证据，
> 但后来发现 **Worker 里跑的是旧 prompt**（见 §6.5）——那些行为其实是**在正确执行旧 prompt**。
>
> **重启 Worker 后重跑，两个越界都消失了**：
>
> | 节点 | 旧 prompt 下 | 新 prompt 下 |
> |---|---|---|
> | `log-analyst` | 调 `get_trace`（越界） | ✅ 只有 2 次**按服务**的 `query_logs` |
> | `trace-analyst` | 先调**无 service 的宽** `query_logs` | ✅ 只有 1 次 `get_trace`（用工单 trace_id） |
>
> **所以"prompt 禁止不住行为"这条结论不成立**（证据全部来自旧 prompt）。
> 下面这段保留作记录，但**不要据此下结论**。
>
> **连带影响**：`TODO.md` §16（拆 MCP server）**因此降级为「先不做」**——
> 它的实测依据被推翻，且 `mcp_servers` 表已有的 `enable_tools` 列**本就能实现工具级过滤**
> （见该节）。

prompt 里写了"不要做 X"，模型可能照样做 X。

| 场景 | 越界行为 |
|---|---|
| 1 | `trace-analyst` 调 `query_metrics`（那是 `metrics-analyst` 的职责） |
| 2 | `log-analyst` 调 `get_trace`（那是 `trace-analyst` 的职责） |
| 2 | `trace-analyst` 仍先调 `query_logs`（prompt 明写"不要为了找 trace_id 先查日志"），且是**不带 service 的宽查询** |

**对照实验**（同一批改动里）：

| | 结果 |
|---|---|
| `triage` 禁止查数据 | ✅ **禁住了**——因为它 **`mcp_server_ids` 被解绑**，手上没有工具 |
| `trace-analyst` 禁止先查日志 | ❌ **没禁住**——它手上有全部 9 个工具 |

> **结论：靠 prompt 禁止一个行为是无效的，得靠工具面。**
> 根因是 MCP 工具绑定**只有 server 级粒度**，而 9 个工具全在同一个 server 上——
> 要么全给、要么全不给。详见 `TODO.md` §16。

### 9.4 ~~`scope` 不输出新契约字段~~ —— **已解决，真因见 §6.5**

> ⚠️ 首版诊断（"prompt 太长把新字段淹没"）**是错的**。
> 真因是 **Worker 未重启**，新 prompt 从来没送达模型——
> Worker 里跑的是 **1728 字符的旧版**（连 `business_paths` 这个词都没有）。
> **重启 Worker 后重跑，四个字段全部正常输出。**
> 详见 `TODO.md` §17 与本文档 §6.5。

### 9.4b 旧记录（作废）


`CandidateServicesSchema` 新增的 `evidence_source`（**必填**）、`business_paths`、
`matched_domains`、`ambiguous`，**两个场景都没输出**：

```
evidence_source : None      ← 必填却缺
business_paths  : 无
matched_domains : null
```

而 **13 个节点里 12 个合规，只有 `scope` 违反**——所以不是系统性问题，也不是配置没生效
（`GET /agents/service-scoper` 能查到新 prompt 与新 schema）。

**推测**：scope 的 prompt 已经很长（3609 字符 / 6 条规则 / 规则 3 有 8 个子项），
新字段埋在规则 3 的第 7 个子项里，被淹没了。

> ⚠️ schema 只是塞进 prompt 的**提示**，`scopes.py` 的 `run_agent` 只做 `json.loads`，
> **没有 schema 硬校验**。所以"字段是必填的"只对模型构成请求，不构成约束。

### 9.5 偶发的 LLM API 连接失败（非 agent 问题）

场景 2 的前两次 run 都失败，但**错误各不相同**，且都是连接层：

```
run_44de89c97d  scope  [SSL: DECRYPTION_FAILED_OR_BAD_RECORD_MAC] ...
run_f7f1801d95  triage Request timed out.（3 次重试全超时）
```

判定依据：**DeepSeek API 直连测 3/3 成功（~1s）**，且场景 1 那次（带全部改动）成功过。
第 3 次 run 就通了，两个场景的其余节点全部正常。

**排障提示**：见到这类错误先直连测一次 API，别急着怀疑 agent 逻辑——
`grep "执行失败" <worker 日志>` 能直接看到是哪个节点、什么错。
