# §11 分层：API ↔ Service ↔ Repository

> **本文从 `CLAUDE.md` 拆出（2026-09-23）** —— 它此前**每个 session 都全量加载**，
> 而这份只在下面这个时机才需要。
>
> **触发条件**：**加新模块 / 移动文件 / 改 import 时**（几乎每次写新代码都要）
>
> 索引在 `CLAUDE.md` 的「约束索引」；那里还有每条的一句话判据。
> ⚠️ 正文里的 `§N` 编号是**稳定标识**，全仓（README / 测试 / design / 源码注释）
> 有 14 处按它引用 —— 拆文件**不改编号**，正是为了不让那些引用断掉。

---

11. **分层：API ↔ Service ↔ Repository**（2026-09-17 定）。**新增/移动模块前先定它属于哪层。**

    | 层 | 定义 | 判据 |
    |---|---|---|
    | **API** | 带 FastAPI、**经外部 HTTP 访问**的接口 | 文件里真的 `from fastapi import …` |
    | **Service** | **进程内**调用，只做逻辑组装，**不含 FastAPI** | 无 fastapi、无 `api/` |
    | **Repository** | 数据访问 | 被 service 调用，**不反向依赖** |

    调用方向 **API → Service → Repository**，**不可跳层**。

    ⚠️ **判层不能 grep 字符串**——`datasource/*.py` 的 docstring 里就写着 "FastAPI"，
    而它零依赖。看 `import`，不看注释。

    **四条判据**（应当写成测试，比约定可靠）：

    1. `agentflow/api/` **只** import service 与 fastapi
    2. `service/` 与 `repository/` **不得** import fastapi，也**不得** import `api/`
    3. **Worker 必须经 service**，不得直接调 repository / executor
    4. Repository **不得**依赖 service / api

    ⚠️ **当前代码有四处已知违反，别把现状当范本**（整改计划见 `docs/TODO.md` §14）：

    - **（最大）API 层直接编排一切**：`api/app.py`（1600+ 行）import 了 **11 个顶层包**
      —— `agents approval config core datasource executor lock queue service statestore
      tenants worker`，其中包括 `from ..worker import WorkerPool`（`queue=memory` 模式
      下 API 内联拉起 worker，这是设计，但也说明这个文件同时在当端点与当编排层）。
      **它 import 了 `service`，可大量编排逻辑仍写在端点文件里**——新增端点时请把逻辑
      放进 Service，别继续往这个文件里堆。
    - **`api/*_store.py` ×5 是 Repository，却放在 API 层** → `statestore/router.py`
      （Repository）反向依赖 `api/`。今天零代价，但它锁死未来：哪天某个 store 要
      import `app.py` 的东西，**Worker 进程就会被拖上整个 web 栈**（FastAPI/starlette/
      uvicorn），且不会有任何提示。
    - **Worker 绕过 Service**（`worker.py` 直接调 executor + repository），
      **已经造成重复实现**：`service.py:231` 与 `worker.py:212` 各有一份逐行近乎相同的
      `_mark_cancelled`，Worker 那版的 docstring 自己写着「与 RunService.stop_run 同语义」。
      **两处要同步维护**——这就是跳层的实际代价。
    - **`/app-indicators` 由 API 层直接调 `datasource/`**（跳过 Service），
      是已知边界情况，见 `docs/TODO.md` §15。

    > **分层（角色）与进程归属（跑在哪）是正交的两把尺子，同一模块两个答案都要对**：
    > `api/app.py` = API 层 + 仅 API 进程；`service.py` = Service 层 + **两个进程都要**；
    > `statestore/` = Repository + 两个进程都要；`executor/` `agents/` `sandbox/`
    > = 执行引擎（不属三层）+ 仅 Worker。
