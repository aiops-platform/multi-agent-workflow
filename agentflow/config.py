"""全局配置：LLM + 基础设施适配层后端切换。

本地 MVP 与生产统一架构（design §3）：核心逻辑 100% 共享，基础设施通过
接口（StateStore/Queue/Lock）定义，配置驱动切换。本模块是唯一的配置入口。
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(ROOT / ".env",),
        env_prefix="AGENTFLOW_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    # ---- LLM（design §16.3：deepseek-v4-flash）----
    # 兼容两种环境变量：AGENTFLOW_DEEPSEEK_API_KEY（本库）与 DEEPSEEK_API_KEY（spike/.env 惯例）
    deepseek_api_key: str = Field(default="", validation_alias=AliasChoices("AGENTFLOW_DEEPSEEK_API_KEY", "DEEPSEEK_API_KEY"))
    deepseek_base_url: str = Field(default="https://api.deepseek.com/v1", validation_alias=AliasChoices("AGENTFLOW_DEEPSEEK_BASE_URL", "DEEPSEEK_BASE_URL"))
    deepseek_model: str = Field(default="deepseek-v4-flash", validation_alias=AliasChoices("AGENTFLOW_DEEPSEEK_MODEL", "DEEPSEEK_MODEL"))

    # ---- StateStore ----
    state_store: str = "sqlite"  # sqlite | memory | postgres(M6)
    state_db_path: Path = ROOT / "data" / "agentflow.db"
    postgres_dsn: str = "localhost:5432/agentflow?user=agentflow&password=agentflow"

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
    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    # 租户配置文件（§9.3 配额/审批人）；空 = 全部用内置默认（不限制）。
    # v5.3 起降级为 bootstrap 种子：首启导入管理库，运行时以管理库为准。
    tenants_file: str = ""
    # db_ref/凭证加密密钥（Fernet, 32B urlsafe base64，§5.3）；缺省从 jwt_secret 派生（告警）
    secret_key: str = ""

    # ---- 数据面姿态（v5.3 §7/P1，v5.5 批3 起语义收窄）----
    # 原名"共享数据源开关"：**内置共享数据源工具已删除**（数据查询全部走租户 MCP，
    # design-v5.5），本开关如今**只剩一个作用**——是否放行 `inputs.repos` 直传。
    #   False（默认，生产加固）：封堵 inputs.repos——堵"用平台身份操作任意 repo"
    #   True（dev/testbed 联调）：放行 inputs.repos（repo 由调用方显式指定）
    # 名称保留是为了不破坏既有 .env；新代码请按"repos 直传开关"理解。
    shared_datasources: bool = False

    # ---- 配置热载（Worker）----
    # Worker 独立进程看不到 API 的内存状态，按「库内指纹」判定 agent 配置 / MCP server
    # 是否变过（`agents/config_sync.py`）。本值 = 两次查库检查之间的**最小间隔**（秒）：
    # 配置变更最迟在本间隔内生效。0 = 每次节点执行都查（测试用；生产不建议，
    # 会增加 DB 往返）。
    config_refresh_sec: float = 5.0

    # ---- 沙箱（M4）----
    open_sandbox_domain: str = "localhost:8080"
    open_sandbox_api_key: str = ""

    # ---- 工作区（§8.7.2）----
    # Run 级代码工作区根目录；布局 {root}/{tenant}/{run}/repos/{service}。
    # 修复侧 agent 的工作区工具（agents/workspace_tools.py）按 current_run 在此定位。
    workspace_root: Path = Path("/tmp/agentflow-workspace")

    # ---- 观测（可选）----
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"


@lru_cache
def get_settings() -> Settings:
    return Settings()


def postgres_dsn(settings: Settings) -> str:
    """settings.postgres_dsn → psycopg 连接串（补 postgresql:// 前缀）。

    唯一归一化点：运行期 StateStore 与控制面配置 store（MCP/workflow）都复用，
    避免各处重复拼前缀漂移。
    """
    return f"postgresql://{settings.postgres_dsn}"
