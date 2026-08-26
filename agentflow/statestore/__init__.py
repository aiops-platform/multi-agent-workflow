# -*- coding: utf-8 -*-
"""StateStore 适配层：配置驱动切换（design §5）。"""
from __future__ import annotations

from ..config import Settings
from .base import StateStore
from .memory import InMemoryStateStore


def build_state_store(settings: Settings) -> StateStore:
    if settings.state_store == "memory":
        return InMemoryStateStore()
    if settings.state_store == "sqlite":
        from .sqlite import SqliteStateStore

        store = SqliteStateStore(settings.state_db_path)
        # 由调用方（Worker/API）负责 connect()：SQLite 需要异步初始化
        return store
    if settings.state_store == "postgres":
        from .postgres import PostgresStateStore

        return PostgresStateStore(f"postgresql://{settings.postgres_dsn}")
    raise ValueError(f"未知 state_store 后端: {settings.state_store!r}")


async def connect_state_store(store: StateStore) -> None:
    """若后端需要异步初始化（如 SQLite connect），在这里统一完成。"""
    connect = getattr(store, "connect", None)
    if connect is not None:
        await connect()
