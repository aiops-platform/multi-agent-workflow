# agentflow 服务启动指南

> 目标：把 agentflow 后端服务跑起来（API 控制面 / 脚本化 demo / 测试），
> 以及（可选）接上真实 testbed 跑诊断场景。命令都在仓库根目录执行。

---

## 一、概览：这台服务能起什么

| 模式 | 命令 | 用途 |
|---|---|---|
| 安装依赖 | `make install` | 首次必须 |
| 跑测试（72 个） | `make test` | 验证安装正确 |
| 脚本化 demo | `make demo` | 无 LLM 跑通 bug-fix-pipeline 全链路（create_run → 审批 → done） |
| **API 控制面** | `make api` | 起 FastAPI 服务，`http://localhost:8000/docs` |
| 真实诊断（可选） | `scripts/diagnose_scenario1.py` / `diagnose_scenario2.py` | 真实 DeepSeek + 真实数据源 |

---

## 二、前置条件

- **Python ≥ 3.12**（`python3 --version` 确认）
- `make`（macOS 自带）
- git
- 沙箱 / testbed（M4 联调才需要）：podman-machine（8C/12G/60G）、minikube —— **只跑 API/demo/test 不需要**

## 三、首次安装

```bash
cd /Users/h.a.hu/accenture/accenture_aiops_platform/acc-aiops-platform-zjb/multi-agent-workflow

# 创建 venv + 安装依赖（含 dev 依赖）
make install
```

验证安装：

```bash
./venv/bin/python --version                  # 应为 3.12+
./venv/bin/pip list 2>/dev/null | grep -E "agentscope|fastapi|pydantic"
```

> 等价手动安装：`python3 -m venv venv && ./venv/bin/pip install -e ".[dev]"`
> 依赖锁点：**AgentScope 2.0.3**（升级前必须重跑 S-001/S-011，见 CLAUDE.md 约束 1）。

## 四、配置 `.env`

```bash
cp .env.example .env
# 编辑 .env，填入 DeepSeek Key：
#   DEEPSEEK_API_KEY=sk-xxx
```

说明：

- **DEEPSEEK_API_KEY 只在"真实 LLM 推理"时需要**（诊断脚本 / 节点契约测试）。
- 不填也能跑：`make demo` / `make test` / API 控制面都能起，
  `build_model()`（`agents/scopes.py`）会回退到 `ScriptedJsonModel`（确定性输出，供无 Key / CI）。
- 其余配置默认即本地 MVP：`AGENTFLOW_STATE_STORE=sqlite`、`AGENTFLOW_QUEUE=memory`、
  `AGENTFLOW_LOCK=memory`。SQLite 库会自动建到 `data/agentflow.db`（目录自动创建）。
- 切生产后端：`AGENTFLOW_STATE_STORE=postgres` / `AGENTFLOW_QUEUE=kafka` /
  `AGENTFLOW_LOCK=redis`（M6 适配器，需要对应中间件）。

## 五、快速验证（不需要 API）

```bash
# 跑全部测试（M0-M7 语义，约 72 个）
make test

# 脚本化 demo：诊断 → 修复 → 合并审批 → 提交，全部用确定性输出
make demo
```

看到 `✅ 全链路完成: DONE` 说明核心编排可用。

## 六、启动 API 控制面

```bash
make api
# = uvicorn agentflow.api.app:app --reload --port 8000
```

启动日志应看到 FastAPI + `Uvicorn running on http://0.0.0.0:8000`。

### 验证 + 冒烟

```bash
# 健康检查
curl -s http://localhost:8000/health
# → {"status": "ok", "service": "agentflow-control-plane"}

# 冒烟：POST /run 创建一次 Run（无审批的最小 workflow，默认 runner）
curl -s -X POST http://localhost:8000/run -H 'Content-Type: application/json' -d '{
  "tenant_id": "local",
  "inputs": {"bug_report": {"number": "INC1", "short_description": "smoke"}},
  "workflow_yaml": "name: smoke\nversion: \"1.0.0\"\nnodes:\n  triage:\n    agent: triage\n"
}'

# 查审计 / 查 Run（用上一步返回的 run_id）
curl -s "http://localhost:8000/audit?limit=20"
curl -s "http://localhost:8000/runs/{run_id}"
```

API 端点一览（`/docs` 有 Swagger）：

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/run` | 创建并执行一次 Run（body 含 workflow_yaml + inputs + tenant_id） |
| GET | `/runs/{run_id}` | 查 Run |
| POST | `/runs/{run_id}/approve?node_id=...` | 审批（body: approved / by / comment） |
| GET | `/audit` | 审计日志查询 |
| GET | `/health` | 健康检查 |

> 注：审批端点需要 workflow 里有 `kind: approval` 节点才会触发 `WAITING_APPROVAL`
> （参考 `workflows/bug-fix-pipeline.yaml` 的 `approve-changes`）。M0-M2 形态是**进程内直接执行**，
> 审批返回后同进程继续跑。

## 七、（可选）真实诊断场景：接 testbed

你的场景（服务报错 → trace → 根因）走这条链路。需要先部署 testbed 并端口转发：

```bash
# 1. 部署 testbed（services + ES/Prometheus + configmaps）并转发端口
cd ../../agentflow-testbed && bash scripts/port-forward-all.sh

# 2. 准备 DeepSeek Key（spike/.env 里已有）
cd ../backend && source ../spike/.env
```

场景 1（磁盘打满 → 期望 `infra_issue`）：

```bash
bash fault-inject/scenario1.sh                     # 注入故障
./venv/bin/python scripts/diagnose_scenario1.py    # 真实诊断
# → root_cause_type: infra_issue（磁盘 EmptyDir 写满）
bash fault-inject/scenario1-recover.sh             # 恢复
```

场景 2（warranty fin 缺参 → 期望 `code_bug`）：

```bash
bash fault-inject/scenario2.sh
curl -s --max-time 8 -X POST "http://localhost:18080/checkout?orderId=ORD20260819001"  # 触发挂起
./venv/bin/python scripts/diagnose_scenario2.py
# → root_cause_type: code_bug（warranty-service fin 缺参）
bash fault-inject/scenario2-recover.sh
```

> ⚠️ 每个场景需要**干净日志窗口**：连续跑两场景会互相污染（场景 1 残留干扰场景 2 定位）。
> 切换前清 ES：`curl -X DELETE :19200/app-logs`。
> 数据源细节：ES index `app-logs`（字段是驼峰 `app.traceId`）；Prometheus 走 cAdvisor
> `container_*`；kubectl namespace `order`。

---

## 八、常见问题（FAQ）

| 现象 | 原因 / 处理 |
|---|---|
| `make install` 报版本错 | Python 版本 < 3.12，或网络问题重试；`pyproject.toml` 要求 `>=3.12` |
| 没填 Key，诊断脚本报"缺 DEEPSEEK_API_KEY" | `scripts/*` 强制要求；demo/test 不需要 |
| 端口 8000 被占用 | `lsof -i :8000` 找到占用进程，或改 `make api` 里的 `--port` |
| `data/agentflow.db` 哪来的 | SQLite StateStore 自动创建（本地默认后端），可删了重建 |
| trace-analyst 返回 `{}` | `max_iters` 需 ≥ 12（2 工具 + 链合成）；默认 6 会迭代耗尽 |
| 两个场景结果互相污染 | 场景切换前 `curl -X DELETE :19200/app-logs` 清窗 |
| 想用内存状态跑（不落盘） | `.env` 设 `AGENTFLOW_STATE_STORE=memory` |
| 审批端点返回 400 | 审批只能作用于 `kind: approval` 节点，且节点需处于 `WAITING_APPROVAL` |

---

## 九、下一步建议

1. 先 `make test` + `make demo` + `make api` 确认基础可用；
2. 按 `DIAGNOSE_TEST_GUIDE.md` 建 `workflows/diagnose-only.yaml` 和三层测试骨架；
3. 接 testbed 跑场景 1/2 验证真实诊断链路。
