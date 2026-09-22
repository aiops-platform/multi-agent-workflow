# `ticket_id` 跨仓一词两义 —— 一处只会在运行时咬人的命名陷阱

**判据**：**跨仓传标识时，先在两边各确认一次"这个词在你这边指什么"。**
不是"名字一样就同一个东西"——恰恰相反，名字一样时最容易不查。

**代价**：按 `ticket_id` 查**永远查不到**（不报错，只是查不到 → 404 → 整条回传失败）。
而且这个错**在单仓视角下完全看不出来**：两边各自都自洽。

**去向**：📄 只能文档（跨仓契约，无法在任一仓里写成红的检查）+
🔴 待机器化（可：E2E 断言 `INC-` 前缀的标识只出现在 `ticket_number` 字段路径）。

---

## 这个词在两边各指什么

| 仓 | `ticket_id` 指 | `INC-YYYYMMDD-NNNN` 在那边叫 |
|---|---|---|
| **MCP server**（`returnApmTicketStatus` 的入参） | 原系统的工单号（`INC-…`） | `ticket_id` |
| **APM**（`POST /v1/problems/ticket-status` 的载荷） | **agentflow 内部 id**（`row.get("id")` 12 位 hex） | **`ticket_number`** |

→ 所以 **APM 的接收端不能按 `ticket_id` 查，要按 `ticket_number` 查**
（其 `find_by_ticket` 两个都认，所以是"能工作但语义混淆"的状态）。

## 闭环的三仓链路

```
agentflow/backend        ticket-done agent（绑 aiops-datasource）
                         + VERDICT_FIELDS 的 ticket-done: delivered
        ↓
aiops-mcp-servers        returnApmTicketStatus(ticket_id, status, description)
                         —— 谓词 + URI 由 DATASOURCE_APM_TICKET_URL / _METHOD 配（**不由 agent 传**）
        ↓
aiops-apm-anomaly-detector   POST /v1/problems/ticket-status（派单方，反查持有该工单的问题单）
```

## 另外两条同源的判据

1. **投不出去必须是红的。** MCP 未配 URL 即 fail-closed 报错 → agent 如实报 `delivered: false`
   → 节点判 FAILED。**绿着一条没交付的 run 比红着更危险**（看板会算成已闭环）。
2. **副作用 agent 的判据**：`这个 agent 一旦重跑，外部世界会不会多一次可见的变化`
   —— 是则进 `SIDE_EFFECT_AGENTS`（`ticket-done` 会对外 POST，重放即**重复投递**）。

## APM 侧的状态语义（改前端/看板时要知道）

- **只有 `resolved` 会改问题单状态**（`escalated` → `resolved`）；
  `failed` / `insufficient` **只追加证据**，不动状态。
- **人的裁定优先于回传**：已是 `resolved` / `closed` 的问题单，晚到的回传不覆盖。

## 一个反直觉的连锁

`ticket-done` 挂在 `commit` 之后 —— 于是 **`commit` 失败会 `on_failure: abort` 中止整条 run，
它下游的 `ticket-done` 根本不执行**，原系统那边**什么都收不到**。
**不是"报了个失败"，是闭环彻底没有回音。**
（这也是 `ws_open_pr` 起不来时最阴的症状，见 §9.7。）
