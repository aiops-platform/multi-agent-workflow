# 复现指南：AIOps 排查全流程（场景1 + 场景2）

> 目标：从零重现「故障注入 → 真实数据源采集 → AI 诊断链 → 根因输出」全过程。
> 对应 `design-v5.2.md` + `SCENARIOS.md`，诊断链为真实 DeepSeek + 真实 testbed 数据源。
> **两场景均已用 `../agentflow-testbed` 实测通过**（场景1 → infra_issue 0.88；场景2 → code_bug 0.95）。

> ⚠️ **重要：每场景要在「干净日志窗口」运行**。诊断链查询 ES 最近 15 分钟日志，
> 连续跑两个场景会互相污染（场景1 的 IOException 残留会干扰场景2 的定位）。
> 场景切换前清空 ES index：`curl -X DELETE http://localhost:19200/app-logs`（filebeat 会自动重建）。

**目录约定**：
- `BACKEND` = `/Users/bo.gong/Documents/accenture/workspace/multi-agent-workflow/backend`
- `TESTBED` = `/Users/bo.gong/Documents/accenture/workspace/agentflow-testbed`（即 `../agentflow-testbed`，独立仓库）
- 数据源端点：ES `:19200`、Prometheus `:19090`、order `:18080`、warranty `:18081`、gateway `:18082`

---

## 0. 一次性准备（首次）

```bash
# 0.1 本机工具（已验证）：podman 5.8 / minikube / kubectl / JDK 21
# 0.2 后端 venv + 依赖
cd $BACKEND && make install            # 或 ./venv/bin/pip install -e ".[dev]"

# 0.3 DeepSeek Key（模型 deepseek-v4-flash）
cp $BACKEND/.env.example $BACKEND/.env   # 填 DEEPSEEK_API_KEY
# 或复用 spike 的 key：
source $BACKEND/../spike/.env            # 会话内注入 DEEPSEEK_API_KEY
```

## 1. 启动基础设施（日常，重启后）

```bash
# 1.1 podman machine（已调至 8C/12G/60G）
podman machine start podman-machine-v5

# 1.2 minikube（docker driver + --force：podman socket + 绕过版本校验）
minikube start --driver=docker --force --cpus=6 --memory=9216
minikube addons enable metrics-server    # 首次

# 1.3 服务镜像（首次 / 重建集群后）：把 podman 里的镜像 load 进 minikube
minikube image load order-service:latest warranty-service:v1 gateway-service:v1
```

## 2. 部署 testbed（首次 / 重建集群后）

```bash
cd $TESTBED
# 2.1 命名空间 + configmaps
kubectl create namespace order
kubectl -n order create configmap prometheus-config --from-file=prometheus.yml=manifests/prometheus/prometheus.yml
kubectl -n order create configmap grafana-datasource --from-file=prometheus.yaml=manifests/grafana/provisioning/datasources/prometheus.yaml
kubectl -n order create configmap grafana-dashboards-provider --from-file=dashboards.yaml=manifests/grafana/provisioning/dashboards/dashboards.yaml
kubectl -n order create configmap grafana-dashboard-json --from-file=service-resources.json=manifests/grafana/dashboards/service-resources.json
kubectl -n order create configmap filebeat-config --from-file=filebeat.yml=manifests/filebeat/filebeat.yml

# 2.2 部署服务 + 数据源
kubectl apply -f manifests/order-service/ -f manifests/warranty-service/ -f manifests/gateway-service/
kubectl apply -f manifests/elasticsearch/ -f manifests/prometheus/ -f manifests/grafana/ -f manifests/kibana/ -f manifests/filebeat/daemonset.yaml

# 2.3 等待就绪（8 个 pod 全部 Running）
kubectl -n order get pods
kubectl -n order wait --for=condition=available deploy/order-service deploy/warranty-service deploy/gateway-service --timeout=300s
kubectl -n order wait --for=condition=available deploy/elasticsearch deploy/prometheus --timeout=300s

# 2.4 端口转发（先清旧的，再起新的）
pkill -f 'kubectl.*port-forward' || true
bash scripts/port-forward-all.sh
```

## 3. 基线验证（数据源链路 OK）

```bash
curl -s http://localhost:19200/_cat/indices/app-logs          # ES，有 app-logs index
curl -s "http://localhost:19090/api/v1/query?query=up" | head -c 120   # Prometheus 采集中
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:18080/actuator/health   # 200
```

---

## 4. 场景 1：订单报价单打印失败（基础设施故障：磁盘 + CPU 打满）

### 4.1 注入故障
```bash
cd $TESTBED && bash fault-inject/scenario1.sh
```

### 4.2 验证故障显现（三源证据）
```bash
curl -s -m 8 "http://localhost:18080/quotation?orderId=ORD001" | head -c 200   # → 500 No space left
# Prometheus CPU 打满（throttled 飙升）
curl -s "http://localhost:19090/api/v1/query" --data-urlencode 'query=sum(increase(container_cpu_cfs_throttled_periods_total{pod=~"order-service.*"}[1m]))'
# ES ERROR 日志（5s 同步）
sleep 5
curl -s "http://localhost:19200/app-logs/_search?size=3" -H 'Content-Type: application/json' \
  -d '{"query":{"bool":{"filter":[{"term":{"app.level.keyword":"ERROR"}},{"term":{"app.service.keyword":"order-service"}}]}},"sort":[{"@timestamp":"desc"}]}'
# → 应看到 "java.io.IOException: No space left on device"
```

### 4.3 跑 AI 诊断链（真实 DeepSeek + ES/Prometheus/kubectl）
```bash
cd $BACKEND && source ../spike/.env
./venv/bin/python scripts/diagnose_scenario1.py
# 期望输出：root_cause_type: infra_issue（磁盘 EmptyDir 写满），命中 SCENARIOS 期望
```

### 4.4 恢复
```bash
cd $TESTBED && bash fault-inject/scenario1-recover.sh
curl -s -m 8 "http://localhost:18080/quotation?orderId=ORD001"   # → 200
```

---

## 5. 场景 2：订单结账无响应（跨服务代码故障：warranty fin 缺参 + 吞异常）

### 5.1 注入故障
```bash
cd $TESTBED && bash fault-inject/scenario2.sh
```

### 5.2 触发故障 + 验证（挂起、无报错）
```bash
# 触发结账（会挂起，--max-time 限制；macOS 无 timeout 命令）
curl -s --max-time 8 -X POST "http://localhost:18080/checkout?orderId=ORD20260819001"   # 无响应体 → 挂起
sleep 10
# ES：warranty-service 出现 fin 缺参 ERROR
curl -s "http://localhost:19200/app-logs/_search?size=5" -H 'Content-Type: application/json' \
  -d '{"query":{"term":{"app.service.keyword":"warranty-service"}},"sort":[{"@timestamp":"desc"}]}'
# → 应看到 "查询三包期失败: ... 必填参数 fin 没有传"
# 同时 order-service 有 Feign 读超时 ERROR（调用下游）
```

### 5.3 跑 AI 诊断链（trace-analyst 用 get_trace 定位故障 span）
```bash
cd $BACKEND && source ../spike/.env
./venv/bin/python scripts/diagnose_scenario2.py
# 期望输出：root_cause_type: code_bug（warranty-service fin 缺参），命中期望
```

> 建议在触发结账后 **60s 内**跑诊断：此时 order-service 的 Feign 超时 ERROR 尚未落日志，
> 窗口里只有 warranty 的 fin 缺参 ERROR，信号最干净（order 超时属于「下游调用症状」）。

### 5.4 恢复
```bash
cd $TESTBED && bash fault-inject/scenario2-recover.sh
curl -s --max-time 8 -X POST "http://localhost:18080/checkout?orderId=ORD20260819001"
# → {"status":"checked_out",...}
```

---

## 6. M4 沙箱验证（独立执行 Pod，design §4.1 / §10.2）

### 6.1 构建沙箱镜像 + 加载
```bash
cd $BACKEND
# exec 服务是纯 stdlib（http.server），零 pip 依赖 → 离线可建
docker build -t agentflow-sandbox:latest -f docker/sandbox/Dockerfile .
# 需要 Java 编译（fix-implementer/tester 编译 Java 代码）时：
#   docker build --build-arg WITH_JDK=1 -t agentflow-sandbox:latest -f docker/sandbox/Dockerfile .
minikube image load agentflow-sandbox:latest
```

### 6.2 K8s 端到端验证
```bash
cd $BACKEND
./venv/bin/python scripts/verify_sandbox.py
# 期望输出：
#   run_python: 沙箱 python OK 42        （沙箱 Pod 内真实执行）
#   write_file /workspace: written       （挂载卷可写）
#   超时限制: sleep2/timeout1 → timed_out（§10.2 生效）
#   destroyed=True
```

> 说明：
> - 本地联调用 **kubectl port-forward**（macOS 宿主不可路由 pod IP）；生产环境 Worker 在集群内直连 pod IP/ClusterIP。
> - 沙箱 Pod 安全基线：非特权 + drop ALL capabilities + cpu 2/mem 4Gi + /workspace 卷（§10.2）。
> - Action Executor（§10.3）：`scale_deployment[0,10]` / `restart_pod` / `patch_resources` / `delete_temp_file` 均带白名单校验，代码见 `agentflow/sandbox/action_executor.py`。

---

## 7. 停止 / 清理

```bash
pkill -f 'kubectl.*port-forward'    # 停端口转发（含沙箱）
kubectl -n agentflow delete pods -l app=agentflow-sandbox   # 清沙箱 Pod（如有）
minikube stop                        # 停集群（保留集群数据）
# 彻底清理：minikube delete && kubectl delete ns order
```

## 8. 常见问题

| 现象 | 处理 |
|---|---|
| `kubectl` 连不上（i/o timeout） | minikube VM 挂了 → `minikube start` 重启 |
| 端口"已占用"但连不通 | 旧 port-forward 残留 → `pkill -f 'kubectl.*port-forward'` 后重跑 port-forward-all.sh |
| ES 起不来（vm.max_map_count） | minikube docker driver 下未见此问题；若出现：`minikube ssh` 内 `sysctl -w vm.max_map_count=262144` |
| 诊断输出 `{"note":"no_api_key"}` | 没读到 DEEPSEEK_API_KEY → `source ../spike/.env` 或检查 backend/.env |
| trace-analyst 输出 `{}` | max_iters 不足 → 脚本已对 trace-analyst 用 12 |
| 场景2 根因误判为场景1 的磁盘问题 | **日志窗口被污染** → 恢复上一场景 + `curl -X DELETE :19200/app-logs` 清窗后重注入 |
| trace-analyst 把 order-service（Feign 超时）当故障服务 | get_trace 会区分「业务根因」（warranty fin 缺参）与「下游调用症状」（feign/timeout）；若仍误判，重跑一次或确保窗口干净 |
| AgentScope streaming 收尾警告 | 无碍功能；可 `stream=False` 消除 |
| 沙箱 Pod Error / 无法连接 | 查 `kubectl -n agentflow logs <pod>`；本地连沙箱必须 port-forward（pod IP 不可路由） |
| 沙箱镜像构建卡死 | 网络受限 → 用纯 stdlib exec 服务（`docker/sandbox/Dockerfile` 默认无 apt/pip）；需 Java 才开 `WITH_JDK=1` |

## 9. 依赖

- 服务镜像：`order-service:latest`、`warranty-service:v1`、`gateway-service:v1`（需先构建/load 进 minikube）
- ES 8.13.4 / Prometheus v2.53.0 / Grafana / Kibana / Filebeat（manifests 引用公共镜像，minikube 自动拉取）
- DeepSeek API Key（`deepseek-v4-flash`）
- 沙箱镜像：`agentflow-sandbox:latest`（本地 build + `minikube image load`，纯 stdlib 离线可建）
