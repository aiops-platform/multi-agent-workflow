# 静默失败缺陷族 —— 本仓最高频的一类缺陷

**判据（改完任何"接线"类代码，问三句）**：

1. **这条路径在部署形态下真的走到吗？**（本地测试都直接构造对象，不走 `main()`）
2. **下游按图里写的方式读，读到的是值还是 `None`？**（`_walk` 遇失配键**静默返回 None**）
3. **这个能力的消费方是谁？**（`grep 类名` 若只有 docstring 命中 = **零消费方**）

**代价**：不报错、run 照报 `done`、接口照回 `ok: true`。**所有常规信号都是绿的**，
只有去核数据形状或部署形态才发现是空的/没接线。排查成本极高。

**去向**：📄 只能文档（判据本身是人类判断，但每一族实例都应有一条检查 —— 见文末）。

---

## 已修的实例（每个都值得当模板看）

- **`worker --dsn` 分支在装配 node_runner 之前就 `return`** → 容器形态下每个节点落到
  `_default_runner`（睡 10ms 返回 `{"ok": True}`），**不调 LLM、不调工具，run 照样 done**。
  修法：装配抽成 `worker.build_node_runner(settings, stores)`，两条路径共用。
  **判据：别让任何分支在装配前 return。**
- **负证据形状与下游读的不一致**：`on_failure: continue` 原返回 `{"found": false, "error": …}`，
  而四个取证 agent 的 schema 都把 `summary` 列为 required、`rca` 读的正是
  `$.nodes.X.output.summary` → "这一路挂了"在下游眼里变成"这一路什么都没说"。
- **`params` 引用不存在的字段**：`recap` 写 `$.nodes.commit.status`，而 `CommitSchema`
  无 `status` → 恒 `null`，"提交成没成"从未到达复盘 agent。**这种引用不报错、不加载失败**。
  已加测试 `test_seed_params_reference_real_schema_fields` 全量兜住种子里所有引用。
- **`join` 默认 `any` 导致的多入边节点过早调度**（踩过两次：`locate`、`rca`）：
  `rca` 因此**从来没拿到过五维取证摘要**（`docs/TODO.md` §19）。
  **判据：入边是不是都"同一批产出"？是就 `join: all` + `required_edges`。**

## 已核实、未修 → `docs/TODO.md` §23

`pending_approvals[].trigger` 恒 null；resume 对终态 run 回 `ok:true` 实为 no-op；
**`ToolPolicy` 全类零运行期消费方**（租户 deny 规则**从未生效过**）；
`ActionExecutor._l2_action` 不传 `tenant_id` → 租户 namespace 边界是开的。

## 三个新变体（2026-09-22 命名）

**① 两个绿着的信号互相矛盾。** 沙箱从宿主不可达，而同一时刻 `podman ps` 报 **healthy**
（容器内 healthcheck 打的是容器内 `127.0.0.1`）、宿主 `curl` 报 **connection reset**。
两边都"有证据"，所以第一反应是怀疑网络/转发，而不是配置。
**可复用判据：服务绑的地址要对上"连接从哪来"** —— 同 netns（K8s sidecar / 容器内
healthcheck）→ loopback 够；跨 netns（compose 端口发布）→ 必须绑到 eth0。

**② 一条永远只走同一分支的 `when`。** `eval_condition` 的实现是
`left.replace("$.nodes.", "", 1)`，所以 `$.inputs.x == 'y'` 的 replace 不生效 → 取到 `None`
→ **`==` 恒假、`!=` 恒真，一声不吭**。
**判据：写错了必须与"条件真的不满足"可区分** —— 取值取不到要静默判不满足（承重语义），
但**根前缀不认识是写错**，必须报错。

**③ 把一张没有上游的单送去了投递口。** 手工建的工单在原系统里没有对应物，
而 `ticket-done` 照跑 → 404 → 节点 FAILED → run 中止。
链路里**每一环单独看都是对的**（404 是"不猜"、如实归一、判红是"投不出去必须红"），
错的是**根本不该把这张单送过去**。

## 一条重要的推论

**"AI 只做 demo、不具生产健壮性"的根因不在 AI** —— 上面那一串
（`ToolPolicy` 零消费方、`--dsn` 提前 return、`severity-medium` 没 CSS 规则、`on_reject` 是死配置）
**全都有测试覆盖、测试全绿**。问题在于**没有人定义过"完成"必须交出什么**。

**每个实例都应该有一条检查。** 判断方式：这个缺陷如果重现，**哪条测试会红**？
答不出来的，就是还没机器化。
