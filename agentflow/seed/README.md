# agentflow/seed —— 新租户的**默认数据种子**

> ## ⚠️ 这是种子，**不是真源**
>
> workflow 的真源是**数据库**（`CLAUDE.md` §6.0）。本目录里的文件只在
> **新租户的库刚建好、目标表为空**时被写入一次。
>
> **改了这里的文件，对已经存在（已播种过）的租户没有任何效果。**
> 要改已开通租户，走 `PUT /workflows/{wid}`（或 agent / mcp-server 的对应端点）。

## 里面有什么

| 文件 | 播到哪张表 | 触发条件 |
|---|---|---|
| `workflows/_manifest.yaml` + `*.yaml` | 租户库 `workflows` | 该表为空 |
| `dataplane.yaml` | 租户库 `mcp_servers` + `agent_configs`（**内置 agent 的绑定**） | 各自表为空 |
| `agents/*.yaml` | 租户库 `agent_configs`（**自定义 agent 的完整定义**） | 同上（与绑定同一张表、同一道空表守卫） |

播种发生在 `TenantStoresRouter._build()`——即**任何导致某个租户库被打开**的路径
（`tenantctl provision` / `migrate`、API 启动、Worker 装配…），所以覆盖是全的。
新租户建库后即"开箱可用"。

## 几个容易踩的约定

- **`workflows/_manifest.yaml` 的最后一条 = 新租户的默认流程**。因为
  `POST /tickets/{tid}/run` 不指定 `workflow_id` 时取 `saved[0]`，而 `list()` 按
  `created_at DESC`；播种时按 manifest 顺序给 `created_at` 递增，顺序因此是确定的。
  这是**兜底**：工单在建单时钉了 `next_workflow` 就以钉的为准（`api/app.py` 的
  `_workflow_for_ticket`），只有没钉过的老工单和手工建的工单才轮到这条顺序。
- **workflow 的 YAML 正文逐字节保留，不要加头注释**——那段文本会原样进 `workflows.yaml`
  列，并在 UI 的 YAML 编辑框里显示给终端用户。
- **`agents/` 与 `dataplane.yaml` 的 `bindings` 是两个来源，别混**：
  - `bindings` = 「**内置** agent 绑哪些 MCP server」。role/stage/prompt/schema 全在代码里
    （`agents/registry.py` / `prompts.py`），种子里只写绑定 —— 多写一份就是**双真源**。
  - `agents/*.yaml` = 「**自定义** agent 长什么样」。代码里**没有副本**，不写在这儿就没地方写，
    所以它可以把 prompt / schema 一起带上，不存在双真源问题。
  - 选择哪条：名字在内置 16 个里 → `bindings`；不在 → `agents/`。
    写错地方的表现：走 `bindings` 会被 `_agent_row` 静默跳过（"不是内置 agent"），
    结果是**新租户少一个 agent 且只有一条 warning 日志**。

- **`dataplane.yaml` 里不写 URL**：URL 是环境相关的（本地 `127.0.0.1` / k8s 里是
  service DNS），由 `url_setting` 指向的配置项注入（见 `.env.example`）。
- **不写 `tools` / `enable_tools` / `disable_tools`**：那几列是 MCP server 被 load 时
  **运行时发现**的，写进种子等于把一次性的发现冻成声明。
- **agent 的 `role` / `stage` 不在这里声明**：从 `agents/agent_config.py` 的静态表取，
  重复一份只会引入漂移。

## 逃生阀

`AGENTFLOW_SEED_DEFAULTS=0` 关闭播种。注意"空表才播"的推论：**把某张表清空的租户，
会在下次进程启动 / LRU 重建时重新拿到种子**（语义是"空 = 出厂态"）。

设计取舍（为什么是"仓库种子"而不是"从参考租户复制"）见 `docs/TODO.md` §13。
