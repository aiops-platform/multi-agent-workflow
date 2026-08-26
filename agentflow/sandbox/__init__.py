# -*- coding: utf-8 -*-
"""M4：Sandbox（独立执行 Pod）+ Action Executor + Tool Policy。

- ``exec_service``：沙箱 Pod 内执行的 exec 服务（§10.2 限制）
- ``client``：SandboxClient（Worker → 沙箱）
- ``orchestrator``：SandboxOrchestrator（K8s 动态拉起/销毁沙箱 Pod）
- ``action_executor``：Action Executor（§10.3 白名单动作）
- ``policy``：ToolPolicy（§9.5 租户工具策略 + §10.2 资源限制）
"""
