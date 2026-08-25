# -*- coding: utf-8 -*-
"""全局配置：LLM + 基础设施适配层后端切换。

本地 MVP 与生产统一架构（design §3）：核心逻辑 100% 共享，基础设施通过
接口（StateStore/Queue/Lock）定义，配置驱动切换。本模块是唯一的配置入口。
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(ROOT / ".env",),
        env_prefix="AGENTFLOW_",
        extra="ignore",
    )

    # ---- LLM（design §16.3：deepseek-v4-flash）----
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    deepseek_model: str = "deepseek-v4-flash"

    # ---- StateStore ----
    state_store: str = "sqlite"  # sqlite | memory | postgres(M6)
    state_db_path: Path = ROOT / "data" / "agentflow.db"

    # ---- Queue ----
    queue: str = "memory"  # memory | kafka(M6)

    # ---- Lock ----
    lock: str = "memory"  # memory | redis(M6)
    redis_url: str = "redis://localhost:6379/0"

    # ---- Kafka（M6）----
    kafka_bootstrap: str = "localhost:9092"

    # ---- 沙箱（M4）----
    open_sandbox_domain: str = "localhost:8080"
    open_sandbox_api_key: str = ""

    # ---- 观测（可选）----
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"


@lru_cache
def get_settings() -> Settings:
    return Settings()
