# §2 基础设施可插拔

> **本文从 `CLAUDE.md` 拆出（2026-09-23）** —— 它此前**每个 session 都全量加载**，
> 而这份只在下面这个时机才需要。
>
> **触发条件**：动 StateStore / Queue / Lock，或加新的基础设施适配器时
>
> 索引在 `CLAUDE.md` 的「约束索引」；那里还有每条的一句话判据。
> ⚠️ 正文里的 `§N` 编号是**稳定标识**，全仓（README / 测试 / design / 源码注释）
> 有 14 处按它引用 —— 拆文件**不改编号**，正是为了不让那些引用断掉。

---

2. **基础设施可插拔**：StateStore/Queue/Lock 只通过 `agentflow/statestore|queue|lock/base.py`
   接口访问，配置驱动切换（`config.py`）。本地 InMemory/SQLite，生产 M6 接 Kafka/Postgres/Redis。
