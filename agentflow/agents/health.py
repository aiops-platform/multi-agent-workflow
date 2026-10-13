"""Agent 执行健康度：**每个 agent 由构造路径继承**的契约。

## 为什么需要它

一次真实故障（`run_ff42fb7bb1`）：`committer` 调了两次 `ws_open_pr`，两次都因鉴权失败
（`No anonymous write access`）；agent 按提示词如实报了 `{"pr_url":"","pr_number":0}`；
**引擎判该节点 succeeded**，run 终态 `done`，页面绿色成功 —— 而代码从未推出去。

根因不是某个守卫写错，是**判据的归属错了**：`VERDICT_FIELDS` / `ARTIFACT_FIELDS` /
`SIDE_EFFECT_AGENTS` 都是**按 agent 名查的小表**，新 agent 必须逐表登记，**漏一个就是一个盲区**。
（`committer` 就是这么漏的；代码注释自己写着"加一个 agent 就是加一条映射"——
那意味着每加一个 agent 都要人**记得**去加。）

本模块把判据换个方向：**agent 自报健康度，引擎按健康度处理，不再需要知道 agent 叫什么**。

## 两级判据 —— 全部是引擎侧事实，**不问模型**

| 层面 | 事实来源 | 处理 |
|---|---|---|
| ① 工具调用是否正确 | 每次工具结果的 `state` | 有错 → 记下来（警告，附错误原文） |
| ② agent 的目标是否达成 | `impact == "deliver"` 的工具**被调用过、却从未成功过** | ①有错且②未达成 → **节点失败、下游不执行** |

第 ② 级不问模型，是因为"这个 agent 该做成的那件事"由**工具语义**决定（工具属性 →
agent 用到即继承），而"调用过、从未成功过"是纯粹的可观测事实。

## 怎么做到"继承"

挂在 `build_agent`（`scopes.py`）**内部**，而不是由调用方传 `middlewares=[...]`。
`build_agent` 是全仓**唯一的 agent 工厂**（生产与测试都经它），所以挂在那里
= **任何构造路径都天然带上**。这才是"继承"，而不是"记得加"。

> 与 `TraceRecorder` 同层但**职责不同**：那个记明细落库（`node_traces`），
> 这个**判定并产出结论**。两者互不依赖 —— 本模块自己读工具结果，不消费 recorder.rows。

## 已知边界（别当漏洞）

1. **`state` 必要不充分**：`ws_run_tests` / `ws_build_artifact` / `sandbox_run_*` 把失败**当数据返回**
   （`passed:false` / `rc!=0`），`state` 仍是 `success` → 本模块**抓不到**"测试跑挂了"。
   那类由 `VERDICT_FIELDS` 兜。**两类判据各管一半，本模块不替代它们。**
2. **"从未调用承诺工具"判不了**：agent 压根没调 `ws_open_pr` 却填了 `pr_url` → 没有工具行 → 放行。
   这是另一个洞（编造），由 `ARTIFACT_FIELDS` 那条线覆盖。**判不了就不判，不猜。**
3. **DENY 行看不到**：`on_acting` 只包**已放行**的工具调用，被权限拒绝的不进这里。
"""
from __future__ import annotations

import logging
from typing import Any

from agentscope.middleware import MiddlewareBase

from .tools import IMPACT_DELIVER, IMPACT_EFFECT, IMPACT_READ, IMPACTS, TOOL_REGISTRY

log = logging.getLogger("agentflow.health")

#: 节点输出里健康度所在的保留键。**下划线前缀**：表明是引擎字段，不是模型 schema 的字段，
#: 免得下游 agent 把它当成自己该填的东西。
HEALTH_KEY = "_health"

#: 单条错误文本的截断长度。原始错误要**看得见**，但整段 gradle/javac 输出塞进输出里太重。
_ERR_CAP = 500


def _state_str(s: Any) -> str:
    """枚举/字符串/None → 稳定小写字符串（与 transcript 同口径）。"""
    if s is None:
        return ""
    return str(getattr(s, "value", s)).lower()


def _content_text(content: Any) -> str:
    """从 ToolResponse.content 抽文本（错误原文就在这里）。"""
    parts: list[str] = []
    for b in content or []:
        t = getattr(b, "text", None)
        if isinstance(t, str):
            parts.append(t)
    return "".join(parts)


class ExecutionHealthMiddleware(MiddlewareBase):
    """按节点实例化：观测每次工具结果，产出一份结构化健康度。

    只实现 ``on_acting``（**已放行**的工具调用才会进这里；DENY 的不会 —— 见模块 docstring 边界 3）。
    所有异常都在内部吞掉：**采集绝不能影响 agent 主流程**（与 `TraceRecorder` 同一纪律）。
    """

    def __init__(self, agent_name: str) -> None:
        self.agent_name = agent_name
        self.tool_calls = 0
        #: 失败的工具调用：``{tool, impact, error}``（error 是原始错误文本，截断到 _ERR_CAP）
        self.failed: list[dict[str, Any]] = []
        #: 承诺交付类工具：调用过的、成功过的
        self.deliver_called: set[str] = set()
        self.deliver_ok: set[str] = set()

    # ---------------------------------------------------------------- 观测

    async def on_acting(self, agent, input_kwargs: dict, next_handler):
        """包住一次工具执行，**原样向下游 yield**，退出时记一条。

        形状与 `TraceRecorder.on_acting` 逐字对齐：`last` 是最后一个 item
        （通常是带 ``state`` 的 `ToolResponse`）。
        """
        tc = input_kwargs.get("tool_call")
        last = None
        try:
            ag = next_handler()
            async for item in ag:
                last = item
                yield item
        finally:
            try:
                await self._observe(agent, tc, last)
            except Exception:  # noqa: BLE001 —— 采集失败绝不影响工具本身
                log.debug("execution health observe failed", exc_info=True)

    async def _observe(self, agent, tc, last) -> None:
        name = getattr(tc, "name", None)
        if not name:
            return
        self.tool_calls += 1

        # 尽力读工具元数据（MCP 的只读提示要它）；缺属性时静默降级
        is_mcp, read_only = False, None
        try:
            tool = await agent.toolkit.get_tool(name)
            is_mcp = bool(getattr(tool, "is_mcp", False))
            read_only = getattr(tool, "is_read_only", None)
        except Exception:  # noqa: BLE001
            pass

        impact = impact_of(name, is_mcp=is_mcp, read_only=read_only)
        state = _state_str(getattr(last, "state", None)) if last is not None else ""

        succeeded = state in ("", "success")
        if impact == IMPACT_DELIVER:
            self.deliver_called.add(name)
            if succeeded:
                self.deliver_ok.add(name)

        if succeeded:
            return

        # 失败：留原始错误文本。`state` 可能是 error / interrupted / denied（末者此处少见）
        self.failed.append({
            "tool": name,
            "impact": impact,
            "state": state or "error",
            "error": _content_text(getattr(last, "content", None))[:_ERR_CAP],
        })

    # ---------------------------------------------------------------- 结论

    def snapshot(self) -> dict[str, Any] | None:
        """结构化健康度。**无事可报时返回 None** —— 不必给每个节点的输出都挂一个空壳。

        ⚠️ 返回的对象会随 `node_attempts.output` 落库，**判据因此可在 resume 时重新推导**。
        这是刻意的：`committer`/`merger`/`deployer` 等 `SIDE_EFFECT_AGENTS` 的尝试记录
        在守卫之前就写好了，resume 会命中幂等缓存、不调 runner——若不落库，
        重新推导时"判不了就放行"，**交付失败会被洗成成功**。
        """
        unmet = sorted(self.deliver_called - self.deliver_ok)
        if not self.failed and not unmet:
            return None
        return {
            # agent 名只是**给人看的**（失败消息里带上才查得动）—— 引擎的判定**不用**它，
            # 这正是本机制与 `VERDICT_FIELDS`/`ARTIFACT_FIELDS` 那类"按 agent 名查表"的区别。
            "agent": self.agent_name,
            "tool_calls": self.tool_calls,
            "failed": self.failed,
            # ② agent 的目标是否达成：承诺交付的工具**调用过、却从未成功过**
            "unmet_delivery": unmet,
            # ① 有工具调用出错（不论是否影响目标）
            "degraded": bool(self.failed),
        }


def attach_health(output: Any, health: dict[str, Any] | None) -> Any:
    """把健康度并入节点输出（**引擎注入**）。``health is None`` 时原样返回。

    只处理 dict：模型可能返回标量/列表（畸形输出），那种情况**不包装** ——
    与既有两个守卫里 ``isinstance(output, dict)`` 的防御一致。

    为什么注入到 ``output`` 里而不是节点的别的槽位：``output`` 是**唯一**能同时到达
    下游（params 解析器走 ``$.nodes.<id>.output.*``）与 API/DB（``nodes.cp``）的路径。
    副作用是下游 agent 会在 params 里看到它 —— **那正是本意**（下游该知道上游降级了）。
    """
    if health is None or not isinstance(output, dict):
        return output
    return {**output, HEALTH_KEY: health}


def impact_of(name: str, *, is_mcp: bool = False, read_only: Any = None) -> str:
    """工具 → 失败后果分级。**本地工具查注册表；MCP 复用行里的 read_only。**

    MCP 工具没有 `ToolSpec`，但协议自带 `readOnlyHint`，`TraceRecorder` 早已把它读进
    流水行（`transcript.py` 的 `read_only`）。所以这里**复用既有信号**，不新造一套元数据。

    fail-closed 的方向是 `effect` 而不是 `read`：读不出来的工具**记入健康度**（可见），
    但**不据此判节点成败**（避免把正常探索判成故障）。
    """
    spec = TOOL_REGISTRY.get(name)
    if spec is not None and spec.impact in IMPACTS:
        return spec.impact
    if is_mcp:
        # 只读提示为 True 才算只读；False / None（服务端没给提示）一律当可写
        return IMPACT_READ if read_only is True else IMPACT_EFFECT
    return IMPACT_EFFECT
