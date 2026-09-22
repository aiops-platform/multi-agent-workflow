# §10 真实数据源 = MCP server

> **本文从 `CLAUDE.md` 拆出（2026-09-23）** —— 它此前**每个 session 都全量加载**，
> 而这份只在下面这个时机才需要。
>
> **触发条件**：动取数、MCP server 注册 / 绑定、或 CMDB 相关时
>
> 索引在 `CLAUDE.md` 的「约束索引」；那里还有每条的一句话判据。
> ⚠️ 正文里的 `§N` 编号是**稳定标识**，全仓（README / 测试 / design / 源码注释）
> 有 14 处按它引用 —— 拆文件**不改编号**，正是为了不让那些引用断掉。

---

10. **真实数据源 = MCP server**（v5.5）：查询逻辑在
    `aiops-mcp-servers/servers/aiops-datasource-mcp-server`（独立仓库）。要点：
    - **查询必须带时间区间与目标**：`start_time`/`end_time` 必填（ISO8601），窗口由
      workflow `inputs.window_start/window_end` 下发，取数节点 `require` 预检
    - **metric 是领域语义**（cpu_percent / memory_percent / disk_percent / error_rate /
      p95_latency_ms），**不传 PromQL 表达式**；未知值报错并列出可用项
    - ES index `app-logs`（字段 `app.*`，时间 `app.@timestamp`，链路 `app.traceId` 驼峰）；
      Prometheus :19090；kubectl namespace `order`
    - `get_trace` 的**故障 span 启发式**（优先「错误非下游调用症状」的服务=业务根因，
      feign/Read timed out 视为症状）住在 server 侧，属**测试床特定经验**
    - 本地联调：`uv run python -m aiops_datasource_mcp_server`（:8300），再注册/绑定
    trace-analyst 需 `max_iters≥12`（2 个工具 + 链合成，默认 6 会迭代耗尽返回 {}）。
    **场景复现需干净日志窗口**：连续跑两场景会互相污染，切换前
    `curl -X DELETE :19200/app-logs` 清窗。
