# 教训索引

> 判据若**已在** `docs/constraints/`，这里只留一句 + 指针，**不复制正文**（两处真源必然漂移）。
> 收录标准与三条去向见 [`README.md`](README.md)。

## A. 教训（踩过的坑）

| # | 现象 | **判据**（遇到 X 就做 Y） | **代价**（不这么做会看到什么） | 去向 |
|---|---|---|---|---|
| 1 | **"看着全绿实则为空"的缺陷族**（本仓最高频） | 改完任何"接线"类代码，问三句：① 这条路径在**部署形态**下真的走到吗？② 下游按图里写的方式读，读到的是值还是 `None`？③ **这个能力的消费方是谁**？（`grep 类名` 只有 docstring 命中 = 零消费方） | 不报错、run 照报 `done`、接口照回 `ok:true`，所有常规信号都是绿的 —— 排查成本极高 | 📄 → [`silent-failure-family.md`](silent-failure-family.md) |
| 2 | agent 回复里**没有合法 JSON** | 输出契约要有 `AgentOutputError`，**不能 `extract_json` 静默返回 `{}`** | "做了但没汇报"与"完全没做"不可区分：实测 `committer` 已成功 add+commit，却因轮次耗尽返回 `{}`，run 照常 success | ✅ 已机器化（`AgentOutputError`） |
| 3 | **审批超时 → skip 级联 → run 卡 `running`** | 审批超时要走**拒绝路径**收敛，且 `rejected-canceled` 必须能 resume | `run.status` 卡在 `running`、`from_checkpoint` 白名单不含它 → **永不收敛**。极隐蔽（`test_m5_approval` 用 InMemory 同 dict 别名效应掩盖了它） | ✅ 已机器化（回归测试 `test_approval_timeout_resume_converges_sqlite`） |
| 4 | **`.gitignore` 里一个裸 `workspace/`** | 忽略规则要锚定根（`/workspace/`），**裸目录名会匹配任意层级** | `agentflow/workspace/` **从未入库**，新克隆 ImportError —— 而本机一切正常 | ✅ 已机器化（`tests/test_workspace.py` 的 file:// 本地源） |
| 5 | 改了 prompt / schema，实验"不工作" | **改了 prompt 或 schema 必须重启 API 与 worker** | 静默跑旧 prompt —— 表现为"这个方案不工作"，而不是报错 | 🔴 待机器化（可：启动时把 `agent_configs` 的指纹写进日志，或在 `make api` 里强制提示） |
| 6 | **`mcp` 升到 2.x 后 8 个测试失败** | `mcp>=1.13,<2.0` —— AgentScope 2.0.3 的 MCP 能力按 **mcp SDK v1** API 写（`MCPTool` 读 `tool.inputSchema`），2.x 改 `input_schema` 会使包装崩溃 | `No module named 'mcp.server.fastmcp'`；且 merge 后须 **`pip install -e ".[dev]"` 重装**才生效 | ✅ 已机器化（`pyproject.toml` 显式 pin + 注释） |
| 7 | `$.nodes.X.output` 解析成 `None` | `output` 是**标准访问器，不能当字段遍历** | **所有 workflow params 静默失效**（曾导致整条诊断链拿不到上游输出） | ✅ 已机器化（`test_param_resolution_output_accessor`） |
| 8 | **`ticket_id` 跨仓一词两义** | 跨仓传标识时，**先在两边确认这个词各指什么** | MCP 的 `ticket_id` 装 `INC-…`，而 APM 的 `ticket_id` 指 agentflow 内部 id（`INC-…` 在那边叫 `ticket_number`）→ 按 `ticket_id` 查**永远查不到** | 📄 → [`cross-repo-ticket-id.md`](cross-repo-ticket-id.md) |
| 9 | 容器时间比宿主**慢 8 小时**，而 `chronyc` 报一切正常 | 别信 `chronyc tracking`（它照样报 `synchronized: yes`）；**直接跟宿主对表** | ①Kibana/Grafana"什么都没有"（数据在，是**浏览器**时钟窗口错位）②**同一行两个时间列差 8 小时**（PG 写的偏、Python 写的不偏） | ✅ 已机器化（`~/bin/podman-clock-guard.sh` + launchd）→ [`clock-drift.md`](clock-drift.md) |
| 10 | 删了一个 `id`，**样式静默丢了** | **样式表也是 id 的消费方** —— 删 `id` / 改类名前先 grep CSS | 按钮仍可点，只是变窄（`#bsZoomFit` 让 Fit 按钮变宽）—— 没有报错，只是"看着有点怪" | 🔴 待机器化（可：grep CSS 里被引用的 `#id`，断言模板里仍存在） |
| 11 | 前端 `npm run preview` 连不上后端 | Vite 的 **preview server 不继承 `server.proxy`**，必须另配 `preview.proxy` | 页面看着像坏了（不是报错，是空白/404） | 🔴 待机器化（可：断言 `vite.config.js` 同时有 `server.proxy` 与 `preview.proxy`） |
| 12 | 换台机器，某条 run 跑到某步"结果不对" | **换机器先 `make doctor`** | 机器相关外部件（pg/kafka/沙箱/**gh**）**缺了不报错**。最阴的是缺 gh：`commit` 失败 → `on_failure: abort` → 下游 `ticket-done` **根本不执行** → 闭环**彻底没有回音** | ✅ 已机器化（`make doctor`，含 `--install`） |
| 13 | 沙箱不可达时**修复不落盘、测试一条不跑**，而节点全绿 | 沙箱不可达 ⇒ `ws_write_file`/`ws_run_tests` **fail-closed**（不回退本地执行） | 实测 `run_668981c0a7`：`fix` 改的文件根本没写进去、`test` 的 `tests_run: 0`，**而节点全绿、run 算 completed** —— 图上一片绿，实际什么都没验证 | ✅ 已机器化（`WORKSPACE_SANDBOXED` fail-closed + `make doctor` 探针） |

## B. 项目事实 / 指针（**别在这里复制正文**）

这些是"X 在哪、谁是当前基线"，正文在各自的文档里：

| 事实 | 正文在哪 |
|---|---|
| **当前设计基线 = `docs/design-v5.8.md`**（v5.6 合并了 v5.4/v5.5） | `docs/design-v5.8.md`；旧稿并存 4 份，**引用前先确认是哪份** |
| 动态编排（L1–L4）**代码零实现**，未实施部分在 TODO **§6**（早前记作 §12，**已过时**） | `docs/design-v5.8.md` §5 + `docs/TODO.md` §6 |
| 业务域分层与 scope 节点（Enterprise→Journey→Portfolio→App） | `docs/design-v5.7.md` §7 |
| CMDB 图谱实体文件 + 写端点 + Architecture 域编辑页 | `docs/ONTOLOGY_REFERENCE_zh-CN.md`、`/admin/cmdb` |
| 参考站（Constellation）的真实结构：**只有 4 类节点 / 3 类边**（不是 12/10），归属是**父指针不是边** | `backend/docs/` 相关调研稿；取数工具在 UI 仓 |
| git 分支约定：**后端直提 `main`、前端提 `vue`、不开 feature 分支** | 本文件「Git 约定」 |
| 全栈 E2E 启动顺序（**compose → minikube**）、`SECRET_KEY` 与 `db_ref`、`inputs` 形状 | `docs/DEPLOYMENT_zh-CN.md`、`REPRODUCE.md` |
| 工单回传闭环的三仓链路（agentflow → MCP → APM） | `docs/design-v5.8.md` §13 |
