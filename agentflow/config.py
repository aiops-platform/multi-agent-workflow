"""全局配置：LLM + 基础设施适配层后端切换。

本地 MVP 与生产统一架构（design §3）：核心逻辑 100% 共享，基础设施通过
接口（StateStore/Queue/Lock）定义，配置驱动切换。本模块是唯一的配置入口。
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(ROOT / ".env",),
        env_prefix="AGENTFLOW_",
        env_nested_delimiter="__",
        extra="ignore",
        # 密钥字段是 SecretStr：**赋值也要走校验**，否则 `settings.jwt_secret = "x"`
        # 会把裸 str 直接塞进 __dict__，之后 `.get_secret_value()` 报 AttributeError
        # （测试广泛用 monkeypatch.setattr 改这些字段，实测踩过）。
        validate_assignment=True,
        # ⚠️ 带 `validation_alias` 的字段（deepseek_*）默认**只认别名**——于是
        # `Settings(deepseek_api_key="sk-x")` 会**静默忽略**这个 kwarg、读回空串，
        # 然后一路回退成 mock runner（run 照样 done）。这就是
        # `test_build_reasoning_model_thinking_enabled_with_key` 长期失败的真因，
        # 它被记成了"需真实 DeepSeek key"（TODO §10）——**是误诊**。
        # 打开后字段名与别名都可用（环境变量仍走别名），赋值/构造两条路行为一致。
        populate_by_name=True,
    )

    # ---- LLM（design §16.3：deepseek-v4-flash）----
    # 兼容两种环境变量：AGENTFLOW_DEEPSEEK_API_KEY（本库）与 DEEPSEEK_API_KEY（spike/.env 惯例）
    # SecretStr：`repr(Settings)` 只显示 **********，取值处显式 `.get_secret_value()`。
    # 裸 str 曾让 key 明文出现在 repr 里 —— pytest/CI 失败回溯默认打印局部变量，
    # 会把真 key 打进构建日志（见 docs/TODO.md §24）。取原始值的写法是**故意啰嗦**的：
    # 它让每一处"我要用明文密钥了"在 code review 里显形，也让 `grep get_secret_value` 可审计。
    deepseek_api_key: SecretStr = Field(default=SecretStr(""), validation_alias=AliasChoices("AGENTFLOW_DEEPSEEK_API_KEY", "DEEPSEEK_API_KEY"))
    deepseek_base_url: str = Field(default="https://api.deepseek.com/v1", validation_alias=AliasChoices("AGENTFLOW_DEEPSEEK_BASE_URL", "DEEPSEEK_BASE_URL"))  # 非密钥，保持 str
    deepseek_model: str = Field(default="deepseek-v4-flash", validation_alias=AliasChoices("AGENTFLOW_DEEPSEEK_MODEL", "DEEPSEEK_MODEL"))

    # ---- StateStore ----
    state_store: str = "sqlite"  # sqlite | memory | postgres(M6)
    state_db_path: Path = ROOT / "data" / "agentflow.db"
    # 连接串里带明文口令 → 同样按密钥处理。**取原始值统一走下方 `postgres_dsn(settings)`**，
    # 那是唯一的归一化点（补 postgresql:// 前缀），别在别处直接读字段。
    postgres_dsn: SecretStr = SecretStr("localhost:5432/agentflow?user=agentflow&password=agentflow")

    # ---- Queue ----
    queue: str = "memory"  # memory | kafka(M6)

    # ---- Lock ----
    lock: str = "memory"  # memory | redis(M6)
    redis_url: str = "redis://localhost:6379/0"

    # ---- Kafka（M6）----
    kafka_bootstrap: str = "localhost:9092"

    # ---- CORS（控制面 API 前端跨域，逗号分隔的 origin 列表，默认 *）----
    cors_origins: str = "*"

    # ---- 执行模式（§6/§8.6）----
    # inline：API 进程内直接执行 DAG（本地 MVP 默认）
    # queue ：API 只发布 run.trigger，Worker 消费执行（生产形态；queue=memory 时
    #         Worker 以进程内后台任务运行，queue=kafka 时用 `python -m agentflow.worker`）
    run_mode: str = "inline"  # inline | queue

    # ---- 多租户 / 认证（§9）----
    # JWT 密钥（HS256）。非空 = 强制 Bearer JWT，tenant_id 由 claim 派生（org_id/
    # tenant_id），客户端提交的 tenant 一律忽略；为空 = dev 模式，回退显式传参
    # （本地联调，启动时告警）。
    jwt_secret: SecretStr = SecretStr("")
    jwt_algorithm: str = "HS256"
    # 租户配置文件（§9.3 配额/审批人）；空 = 全部用内置默认（不限制）。
    # v5.3 起降级为 bootstrap 种子：首启导入管理库，运行时以管理库为准。
    tenants_file: str = ""
    # db_ref/凭证加密密钥（Fernet, 32B urlsafe base64，§5.3）；缺省从 jwt_secret 派生（告警）
    secret_key: SecretStr = SecretStr("")

    # ---- 新租户默认数据播种（`agentflow/seed/`，见其 README）----
    # 租户库建好之后往里写一份默认数据，让它**开箱可用**：默认 workflow +
    # MCP server 注册 + agent 绑定。不播的话新租户 `POST /tickets/{tid}/run` 直接 400；
    # 只播 workflow 而不播绑定的话，run 能跑完但**每个 agent 零工具**（空转）。
    #
    # 语义：**空表才播、绝不覆盖**（三张表各自判断）。推论——把某张表清空的租户会在
    # 下次进程启动 / LRU 重建时重新拿到种子（"空 = 出厂态"）；要彻底关掉置 0。
    seed_defaults: bool = True
    # 种子中那个数据面 MCP server 的地址。**是配置不是常量**：URL 环境相关
    # （本地 127.0.0.1，k8s 里要 pod 可达的 service DNS）。默认值对齐
    # `uv run python -m aiops_datasource_mcp_server`（:8300）。
    mcp_datasource_url: str = "http://127.0.0.1:8300/mcp"

    # ---- 数据面姿态（v5.3 §7/P1，v5.5 批3 起语义收窄）----
    # 原名"共享数据源开关"：**内置共享数据源工具已删除**（数据查询全部走租户 MCP，
    # design-v5.6），本开关如今**只剩一个作用**——是否放行 `inputs.repos` 直传。
    #   False（默认，生产加固）：封堵 inputs.repos——堵"用平台身份操作任意 repo"
    #   True（dev/testbed 联调）：放行 inputs.repos（repo 由调用方显式指定）
    # 名称保留是为了不破坏既有 .env；新代码请按"repos 直传开关"理解。
    shared_datasources: bool = False

    # ---- App Indicators（Smart Inspection 数据面）----
    # ⚠️ **架构例外**：本组配置支撑 `datasource/` 直连 Prometheus。既定姿态是
    # 「数据面查询全部走租户 MCP」（design-v5.6，见 CLAUDE.md 关键设计约束 §7）。
    # 本次为让遗留前端 Smart Inspection 跑通显式破例，TODO(v5.7) 收编进
    # aiops-datasource-mcp-server 后删除本组配置。
    # Prometheus 基址（不含 /api/v1）。测试床用 kubectl port-forward 的固定映射。
    prometheus_url: str = "http://localhost:19090"
    # 服务自动发现所用的 job：up{job=...} 的 service 标签即服务清单。
    prometheus_job: str = "app-metrics"
    # cAdvisor 指标的 namespace 过滤；空串 = 不过滤（跨命名空间汇总）。
    prometheus_namespace: str = "order"
    # 单次 PromQL 查询超时（秒）。
    prometheus_timeout_sec: float = 5.0
    # 容器活跃判定阈值（秒）：container_last_seen 早于 now-该值 的样本视为已销毁 pod，
    # 不参与聚合（Prometheus staleness 会滞留旧样本约 5 分钟，不过滤会把上个 pod
    # 死前的 CPU 算进来）。
    prometheus_staleness_sec: float = 60.0
    # 快照短缓存（秒）。0 = 禁用。只缓存成功结果，失败不缓存。
    app_indicators_cache_ttl: float = 2.0
    # 展示元数据（owner/type/agentName）从 K8s Deployment 的 label 读
    # （service_meta.py）。这些字段没有实测来源，label 是**声明配置**而非指标。
    # 读取失败只让这三个字段留空 + 记 warning，不影响指标。
    service_meta_enabled: bool = True
    # 读 label 的 namespace；空 = 沿用 prometheus_namespace。
    service_meta_namespace: str = ""
    # label 缓存（秒）。Deployment label 极少变，没必要每 5 秒轮询都打 K8s API。
    service_meta_cache_ttl: float = 60.0

    # status 判定阈值（%）。与前端 metricClass 的 ≥90 红 / ≥70 黄对齐，
    # 避免「数字绿色但徽章红色」。
    app_indicators_warn_cpu: float = 70.0
    app_indicators_crit_cpu: float = 90.0
    app_indicators_warn_mem: float = 70.0
    app_indicators_crit_mem: float = 90.0

    # ---- 配置热载（Worker）----
    # Worker 独立进程看不到 API 的内存状态，按「库内指纹」判定 agent 配置 / MCP server
    # 是否变过（`agents/config_sync.py`）。本值 = 两次查库检查之间的**最小间隔**（秒）：
    # 配置变更最迟在本间隔内生效。0 = 每次节点执行都查（测试用；生产不建议，
    # 会增加 DB 往返）。
    config_refresh_sec: float = 5.0

    # MCP 工具清单的本地记忆化 TTL（秒）。上游 `list_raw_tools` 每次调用都真的打服务端
    # （写缓存但不读），而 Toolkit 每轮 LLM 调用 + 每次工具执行前都会触发一次——实测
    # 单次节点执行 56 个会话里 44 个来自这种重复列举。TTL 内复用缓存，把握手降到 1 次。
    # 0 = 禁用记忆化（退回上游行为）；负值同 0。详见 agents/mcp_tool_cache.py。
    mcp_tools_cache_ttl: float = 60.0

    # ---- 沙箱（M4）----
    open_sandbox_domain: str = "localhost:8080"
    open_sandbox_api_key: SecretStr = SecretStr("")

    # ---- 仓库映射（工作区准备用，§8.7.2）----
    # 工作区准备发生在 run 创建期（早于任何 agent 节点），此时还没有 MCP 调用，
    # 故用**部署配置**驱动，不走 MCP 的 CMDB（agent 侧查 CMDB 走 locate_repo）。
    # 仓库根目录；**空 = 不做工作区准备**（修复侧工具会明确报错，fail-closed）。
    # 支持本地路径（自动补 file://）或 http(s)/file:// URL 前缀。
    repo_root: str = ""
    # service → 仓库目录名 的 JSON 覆盖，如
    # {"order-service": "aiops-test-order-service"}
    # 缺省（空）→ 目录名取服务名本身。**注意：这里没有默认个人路径。**
    repo_map: str = ""

    # ---- 工作区（§8.7.2）----
    # Run 级代码工作区根目录；布局 {root}/{tenant}/{run}/repos/{service}。
    # 修复侧 agent 的工作区工具（agents/workspace_tools.py）按 current_run 在此定位。
    workspace_root: Path = Path("/tmp/agentflow-workspace")

    # ---- 观测（可选）----
    langfuse_public_key: str = ""  # 公开 key，非密钥
    langfuse_secret_key: SecretStr = SecretStr("")
    langfuse_host: str = "https://cloud.langfuse.com"


@lru_cache
def get_settings() -> Settings:
    return Settings()


def postgres_dsn(settings: Settings) -> str:
    """settings.postgres_dsn → psycopg 连接串（补 postgresql:// 前缀）。

    唯一归一化点：运行期 StateStore 与控制面配置 store（MCP/workflow）都复用，
    避免各处重复拼前缀漂移。

    也是 ``postgres_dsn`` 这个 ``SecretStr`` **唯一取明文的地方**——调用方一律走本函数。
    """
    return f"postgresql://{settings.postgres_dsn.get_secret_value()}"
