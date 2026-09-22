# §7 多租户五原则

> **本文从 `CLAUDE.md` 拆出（2026-09-23）** —— 它此前**每个 session 都全量加载**，
> 而这份只在下面这个时机才需要。
>
> **触发条件**：动存储路由 / 加控制面表 / 动租户隔离 / 动 per-tenant MCP 配置时
>
> 索引在 `CLAUDE.md` 的「约束索引」；那里还有每条的一句话判据。
> ⚠️ 正文里的 `§N` 编号是**稳定标识**，全仓（README / 测试 / design / 源码注释）
> 有 14 处按它引用 —— 拆文件**不改编号**，正是为了不让那些引用断掉。

---

7. **多租户（v5.3 五原则，design-v5.3.md）**：
   - **存储路由**：一切库访问经 `statestore/router.py`——普通 StateStore 包装固定解析
     （既有单库用法/测试零改动），`TenantStoresRouter` 按 tenant_id 路由到租户库
     bundle（**state+workflow+mcp+agent_config+ticket**，LRU 淘汰关闭）。**新读路径
     必须经 `service.store_for(tenant)` / router，禁止绕过**（P4：隔离由构造保证）。
     > **新增控制面表时务必加进 `TenantStores`**。`ticket_store` 起初只作为模块全局
     > 存在、不在 bundle 里 → 所有租户的工单都写进共享库（2026-09-14 修复）。
     > 判据：**模块全局 store 只该出现在 `_GlobalStores` 回退形状里**。
   - **PG 租户库是真的独立 database**（`{基础库}-{tenant_id}`，如 `agentflow-otr`）：
     `provision` 会 `CREATE DATABASE … TEMPLATE template0` 建库，`deprovision
     --confirm-delete` 会 DROP（**拒绝删共享基础库**，防连坐管理库）。
     库名派生与建/删库在 `tenants.py`；**`_default_db_ref` 只有一份实现**——
     早期 tenantctl/tenants/router 各写一份，postgres 分支都返回共享 DSN，
     导致 `workflows`/`mcp_servers`/`agent_configs`（**这三张没有 tenant_id 列**）
     跨租户互相可见。
   - **管理库**（`api/management_store.py`）：tenants/schema_versions；db_ref Fernet
     加密（AGENTFLOW_SECRET_KEY，缺省从 jwt_secret 派生并告警）；**任何 API 不回显
     DSN**。tenants.yaml 仅 bootstrap 种子（async_bootstrap_tenants），运行时以
     管理库为准（TenantRegistry.from_management，CRUD 后重建）。
   - **审批 default-deny（§4.2）**：租户配置过 approvers 后，未命中节点 id 且无 "*"
     → 一律 403（堵"自建 workflow 换审批节点 id 绕过"）；service._check_approver
     先看 `cfg.approvers` 是否非空（空=不限制 dev 语义）。
   - **接单 CAS**：Worker trigger（queued→running）/resume（paused|waiting→running）
     经 `cas_update_run_status` 原子转换，重复消息恰一个接单；新状态机含 queued/paused。
   - **topic-per-tenant**：发布一律 `topic_trigger(tenant)/topic_command(tenant)`；
     Worker 绑定租户只消费自己的 topic，None=全局兜底（单租户回退）。
   - **数据面 = 租户 MCP（v5.5 批3 起 MCP-only）**：日志/指标/K8s 查询**全部**由
     `aiops-datasource-mcp-server` 提供（`POST /mcp-servers` 注册 →
     `PUT /agent-configs/{name}` 的 `mcp_server_ids` 绑定），**进程内直连实现已删除**
     （原 `agents/datasources.py`）。本地只读工具仅剩 `locate_code`（CMDB 映射）
     与 `search_knowledge`（占位）。详见 `docs/design-v5.8.md` §3。
   - ⚠️ **例外：`datasource/` 直连 Prometheus**（唯一一处，2026-09 引入）。遗留前端
     Smart Inspection 页面（`service-intelligence-platform-ui/js/app.js`）要的是**瞬时值
     + UI 形状的信封**，而 MCP 侧 `backends/prometheus.py` 是面向 LLM 证据的
     `query_range`（只 5 个领域语义指标，无 instant query、无网络/磁盘 IO）。两者目标
     不同，硬套两头别扭，故显式破例。**裁决边界**：本例外只服务
     `GET /app-indicators` 一个端点；agent 取数一律仍走 MCP。
     **收编时注意**：MCP 侧 `_sel()` 用 `container!="POD"`，在测试床集群上**会算错**
     （sandbox 序列的 `container` 标签是缺失的，该写法会把 pod 级 + sandbox + 应用容器
     三条序列全留下，CPU 接近翻倍）；正确写法是 `container!=""`。
     见 `agentflow/datasource/__init__.py` 与 `app_indicators.build_queries`。
   - **`AGENTFLOW_SHARED_DATASOURCES` 语义已收窄**：内置共享数据源工具没了，此开关
     如今**只剩一个作用**——是否放行 `inputs.repos` 直传（默认 0=封堵）。名称保留是
     为了不破坏既有 .env，新代码请按「repos 直传开关」理解。
   - runner 经 `exec_context.current_tenant`（executor 置位）做 per-tenant MCP
     （mcp_manager 缓存键 (tenant, server_id)，租户间物理不可见）与 per-tenant agent
     配置（agent_config_provider 代际缓存，CRUD 后失效）。
     **注意**：Worker 是独立进程，看不到 API 侧的内存代际计数器，改按**库内指纹**
     判定配置是否变过并热载（`agents/config_sync.py`，`AGENTFLOW_CONFIG_REFRESH_SEC`
     默认 5s）——**绑定新 MCP server 无需重启 worker**（`911c7d3` 修复；早期版本才是
     永久缓存。曾误留作现状描述，2026-09-11 更正）。
   - **生命周期**：`python -m agentflow.tenantctl provision|deploy|upgrade|migrate|
     deprovision`（幂等 saga）；standard 租户专属分支被拒（§9.2 规则 4）；部署记录
     pin SHA 不 pin 分支名。
   - JWT：**鉴权只有两种模式**——`AGENTFLOW_JWT_SECRET` 非空=JWT 模式（claim 派生，
     客户端提交的 tenant 字段一律忽略）；空=dev 回退（`X-Tenant-ID`，告警）。
     租户 claim 优先级 `tenant_id` > `org_id` > `org`；`sub`=审批人身份（JWT 模式下
     approve/reject 的 `by` 也取它，body 不可伪造）。跨租户 run 访问 404
     （`_run_for_tenant`，Router 模式下查不到即 404——隔离由构造保证）。
     **RS256 已可用**（`jwt_algorithm` 是算法无关的 `pyjwt.decode`，把 PEM 公钥放进
     `AGENTFLOW_JWT_SECRET` 即可）；缺的不是算法而是配套：JWKS 自动取钥/轮换、签发侧。
     详见 `docs/E2E_VERIFICATION_zh-CN.md` §JWT 模式。
