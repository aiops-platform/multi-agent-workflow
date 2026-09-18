"""M4：Sandbox（独立执行 Pod）+ Action Executor + Tool Policy。

- ``exec_service``：沙箱 Pod 内执行的 exec 服务（§10.2 限制）
- ``client``：SandboxClient（Worker → 沙箱）
- ``orchestrator``：SandboxOrchestrator（K8s 动态拉起/销毁沙箱 Pod）
- ``action_executor``：Action Executor（§10.3 白名单动作）
- ``policy``：ToolPolicy（§9.5 租户工具策略 + §10.2 资源限制）
"""
from __future__ import annotations


def build_sandbox_client(settings=None):
    """按配置构造 SandboxClient；``AGENTFLOW_SANDBOX_URL`` 为空则返回 ``None``。

    返回 ``None`` 的含义是**不接线**，不是"用本地实现代替"：调用方（runner）拿到 None
    后，写文件/跑测试那两个工具会注册成**调用即报错**的占位（见
    ``agents/tools.WORKSPACE_SANDBOXED``）。刻意不做本地回退——它们执行的是仓库代码，
    而 worker 持有全部密钥，回退等于把隔离作废。
    """
    from ..config import get_settings
    from .client import SandboxClient

    settings = settings or get_settings()
    url = (settings.sandbox_url or "").strip()
    if not url:
        return None
    return SandboxClient(url)
