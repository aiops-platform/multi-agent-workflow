# agentflow 部署指南（design-v5.3 §6/§9/§10）

> 本文是多租户生产部署的约束与操作手册。设计依据见 `design-v5.3.md`，
> 租户生命周期操作见 `python -m agentflow.tenantctl --help`。

## 1. 部署矩阵（先读：约束必须遵守）

| 后端组合 | 副本数 | 说明 |
|---|---|---|
| `state_store=sqlite` + `queue=memory` | **1**（单进程） | 本地 MVP 默认；sqlite 多进程写会损坏 |
| `state_store=postgres` + `queue=memory` | **1** | memory 队列不跨进程：多副本时 resume 命令会沉没（run 永卡审批） |
| `state_store=postgres` + `queue=kafka` | API 多副本 + **每租户 Worker** | 生产形态（§6.2） |

- `AGENTFLOW_RUN_MODE=queue` 时 API 只发布消息，执行在 Worker；
  `queue=memory` 下 Worker 以进程内 WorkerPool 运行（单进程形态）；
  `queue=kafka` 下用独立进程 `python -m agentflow.worker`（每租户一个 Deployment）。

## 2. 生产部署清单（K8s）

```
┌ API Deployment（main 分支镜像，≥2 副本）── 只做路由/JWT/管理库/发布命令
├ 每租户 Worker Deployment（租户分支镜像 {tenant}-{sha}，1 个副本起）
│   └─ 运行在租户 namespace（agentflow-{tenant}，ResourceQuota + NetworkPolicy + SA）
├ Kafka（SASL + ACL：租户 principal 只读写 run.trigger.{tenant}/run.command.{tenant}）
├ PostgreSQL：管理库（management.db 对应的库）+ 每租户库（strong=独立实例，
│   standard=共享实例独立 database/schema）
└ Redis（可选）：分布式锁（配额临界区 / sweeper 多副本互斥）
```

### 2.1 租户开通

```bash
export AGENTFLOW_STATE_STORE=postgres AGENTFLOW_QUEUE=kafka AGENTFLOW_JWT_SECRET=...
python -m agentflow.tenantctl provision team-a \
    --isolation strong --branch tenant/team-a --quota 5 --workers 2 \
    --k8s            # best-effort 建 namespace/ResourceQuota/NetworkPolicy/SA
```

- `strong` 租户才允许专属分支（§9.2 规则 4）；`standard` 固定 `code_branch=main`。
- 租户库 schema 随首连自动幂等建立；`tenantctl migrate` 扇出后续结构变更。

### 2.2 每租户 Worker Deployment

镜像按租户分支构建：`registry/agentflow:{tenant}-{sha}`，消费且仅消费
`run.trigger.{tenant}` / `run.command.{tenant}`。`tenantctl deploy --sha` 在管理库
记录 pin SHA / image_tag / deployed_at（§9.2 规则 2：审计不可变），并打印 kubectl
提示；实际滚动由 CI/CD 执行：

```bash
kubectl -n agentflow-team-a set image deployment/agentflow-worker \
    agentflow=registry/agentflow:team-a-aa11bb22cc33
kubectl -n agentflow-team-a rollout status deployment/agentflow-worker
```

### 2.3 本地实操：minikube 里的每租户 Worker Deployment（podman + compose，已验证）

本地拓扑：中间件 PG/Kafka 走 podman compose，沙箱/Worker 走 minikube（kicbase 并入
compose 网络），host venv 跑 API。**已验证端到端**：API publish → kafka 租户 topic →
k8s Worker 消费 → 执行 → PG 落库。

```bash
# 0) 前置
podman machine start podman-machine-v5
docker-compose up -d postgres kafka redis      # 镜像全 docker.io（mirror 曾 403）
# minikube 并入 compose 网络（供 K8s Pod 经 hostNetwork 访问 PG/Kafka）
podman network connect backend_default minikube

# 1) 装配（管理库 + 租户）——team-alpha 的 PG 管理库注册 + 租户 namespace/配额/NP/SA
python -m agentflow.tenantctl provision team-alpha --force --k8s

# 2) kafka 双 listener（compose 已配）：
#    PLAINTEXT  10.89.0.9:9092（静态 IP，advertised 同，供 pod）
#    EXTERNAL   localhost:19092（宿主 mac：.env 设 AGENTFLOW_KAFKA_BOOTSTRAP=localhost:19092）

# 3) Worker 镜像 + load
docker build -t agentflow-worker:local -f docker/Dockerfile.worker .
minikube image load agentflow-worker:local

# 4) 部署（deploy/worker-deployment.yaml：租户 ns + hostNetwork + RQ 显式资源 + --tenant/--dsn）
kubectl apply -f deploy/worker-deployment.yaml
kubectl logs deployment/agentflow-worker-team-alpha -n agentflow-team-alpha  # 见接单日志

# 5) 触发验证：POST /run（dev 模式 X-Tenant-ID: team-alpha）→ k8s Worker 消费 → done
```

**关键约束**：
- **kafka advertised 必须匹配静态 IP**：compose 给 kafka `ipv4_address: 10.89.0.9`（顶层
  `networks.ipam`），advertised 不再随 recreate 漂移——否则 broker 自连/客户端全断。
- **Worker 容器内不用管理库 db_ref**（provision 时记录的是 `localhost` DSN，容器不可达）：
  以 `--tenant team-alpha --dsn postgresql://…@10.89.0.2:5432/agentflow` **直连共享库**单租户消费。
- Deployment 在租户 ns 需**显式 resources**（RQ 强制，否则创建被拒）；`hostNetwork: true`
  走 kicbase 网络栈访问 compose（生产去掉，用同 ns Service/ClusterIP）。
- 宿主 API 连 kafka 走 EXTERNAL：`.env` 设 `AGENTFLOW_KAFKA_BOOTSTRAP=localhost:19092`。

## 3. Kafka 安全（P2 的信任边界）

- **SASL**：每租户一个 principal（如 `agentflow-team-a`）。
- **ACL**：principal 只允许对 `run.trigger.{tenant}` / `run.command.{tenant}` 的
  READ/WRITE；管理端 topic（`__consumer_offsets` 等）仅平台账号。
- 应用层**不做**消息签名——伪造 trigger/resume 在 broker ACL 处被拒。
- Worker consumer group：每租户独立 group（`agentflow-worker-{tenant}`）。

## 4. 租户 topic 与 Worker 对应关系（本地/单进程形态）

`run_mode=queue + memory` 时 `WorkerPool` 按管理库 active 租户清单为每租户起
消费循环（30s 重扫热接入新租户），并保留一个全局 Worker 兜底未注册租户（dev）。

## 5. 密钥与加密

| 密钥 | 用途 | 缺省 |
|---|---|---|
| `AGENTFLOW_JWT_SECRET` | JWT HS256 验签 + dev 模式开关 | 空 = dev（显式传参，告警） |
| `AGENTFLOW_SECRET_KEY` | 租户库 db_ref / MCP 凭证 Fernet 加密 | 从 `jwt_secret` SHA256 派生（告警） |

生产必须显式配置两者（K8s Secret / Vault 注入，不入镜像不入 git）。
已知风险：HS256 为对称密钥，控制面可"伪签发"——生产上线前需 Gateway 签发 +
RS256 只验签（v5.3 §12 待办 2）。

## 6. 分支治理（P5，CI 需落卡点）

1. 租户分支 = main + 薄覆盖；长期能力一律 main PR。
2. 部署记录 pin SHA（`tenantctl deploy`），分支名仅作引用。
3. CI 漂移监测：租户分支落后 main > 50 commits / 14 天 → 告警（阈值可按仓库调整）。
4. 每分支独立 CI：pytest + 按需镜像构建（tag = `{tenant}-{sha}`）。

## 7. 故障与恢复

- Worker 崩溃：消息 at-least-once 重投 → 接单 CAS（`queued→running`）保证恰一个
  Worker 接单；副作用幂等键兜底（§8.4）。
- 审批超时：sweeper（API 进程内）逐租户扫描 → CAS TIMED_OUT → resume 命令。
- 数据删除：`tenantctl deprovision --confirm-delete`（sqlite 删文件 / PG 手工 DROP）。
