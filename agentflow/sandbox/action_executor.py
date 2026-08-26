# -*- coding: utf-8 -*-
"""Action Executor（design §10.3：有限集合动作 + 参数白名单）。

Agent → RemediationPlan（结构化）→ Action Executor（参数校验）→ K8s API（执行）。
新增动作需经评审和审批。所有动作校验通过后执行并返回结构化结果 + 审计记录。
"""
from __future__ import annotations

import logging
import re
from typing import Any

from kubernetes import client, config as k8s_config

log = logging.getLogger("agentflow.sandbox.action")

# §10.3 参数约束
REPLICA_RANGE = (0, 10)
CPU_RANGE = ("100m", "4")
MEMORY_RANGE = ("128Mi", "8Gi")
TEMP_PATH_PREFIX = "/data/tmp/"


class ActionValidationError(RuntimeError):
    pass


def _parse_quantity(q: str) -> int:
    """把 K8s 数量字符串转成便于比较的 millis/mi：'100m'→100, '4'→4000, '2Gi'→2048Mi。"""
    m = re.match(r"^(\d+(?:\.\d+)?)([mMGikK]i?)?$", q.strip())
    if not m:
        raise ActionValidationError(f"非法资源量: {q}")
    num = float(m.group(1))
    unit = (m.group(2) or "").lower()
    if unit == "m":
        return int(num)
    if unit == "":
        return int(num * 1000)  # cpu 无后缀 = 核 → milli
    if unit in ("gi", "g"):
        return int(num * 1024)  # memory → Mi
    if unit in ("mi", "m"):
        return int(num)
    if unit == "ki":
        return int(num / 1024)
    return int(num)


def _in_range(value: str, lo: str, hi: str) -> bool:
    return lo is None or (lo and _parse_quantity(lo) <= _parse_quantity(value) <= _parse_quantity(hi))


class ActionExecutor:
    """执行受限基础设施动作（§10.3 有限集合 + 白名单）。"""

    def __init__(self, *, namespace_whitelist: list[str] | None = None, pod_prefix_whitelist: list[str] | None = None) -> None:
        try:
            k8s_config.load_incluster_config()
        except Exception:  # noqa: BLE001
            k8s_config.load_kube_config()
        self._apps = client.AppsV1Api()
        self._core = client.CoreV1Api()
        self.namespace_whitelist = namespace_whitelist or []
        self.pod_prefix_whitelist = pod_prefix_whitelist or []

    # ------------------------------------------------------------------
    def _check_ns(self, namespace: str) -> None:
        if self.namespace_whitelist and namespace not in self.namespace_whitelist:
            raise ActionValidationError(f"namespace {namespace!r} 不在白名单: {self.namespace_whitelist}")

    async def execute(self, action: str, *, namespace: str, **params: Any) -> dict:
        """统一入口：action ∈ §10.3 有限集合。返回 {action, namespace, ok, detail, audit}。"""
        handlers = {
            "scale_deployment": self.scale_deployment,
            "restart_pod": self.restart_pod,
            "patch_resources": self.patch_resources,
            "delete_temp_file": self.delete_temp_file,
        }
        if action not in handlers:
            raise ActionValidationError(f"不支持的动作: {action!r}（有限集合: {list(handlers)}，新增需评审）")
        return await handlers[action](namespace=namespace, **params)

    # ------------------------------------------------------------------
    async def scale_deployment(self, *, namespace: str, name: str, replicas: int) -> dict:
        self._check_ns(namespace)
        if not (REPLICA_RANGE[0] <= replicas <= REPLICA_RANGE[1]):
            raise ActionValidationError(f"replicas {replicas} 超出范围 {REPLICA_RANGE}")
        dep = self._apps.read_namespaced_deployment(name, namespace)
        dep.spec.replicas = replicas
        self._apps.replace_namespaced_deployment(name, namespace, dep)
        return self._ok("scale_deployment", namespace, f"{name} → replicas={replicas}")

    async def restart_pod(self, *, namespace: str, name: str) -> dict:
        self._check_ns(namespace)
        if self.pod_prefix_whitelist and not any(name.startswith(p) for p in self.pod_prefix_whitelist):
            raise ActionValidationError(f"pod {name!r} 前缀不在白名单: {self.pod_prefix_whitelist}")
        self._core.delete_namespaced_pod(name, namespace)
        return self._ok("restart_pod", namespace, f"已删除 pod {name}（由 Deployment 重建）")

    async def patch_resources(self, *, namespace: str, name: str, cpu: str, memory: str) -> dict:
        self._check_ns(namespace)
        if not _in_range(cpu, CPU_RANGE[0], CPU_RANGE[1]):
            raise ActionValidationError(f"cpu {cpu} 超出范围 {CPU_RANGE}")
        if not _in_range(memory, MEMORY_RANGE[0], MEMORY_RANGE[1]):
            raise ActionValidationError(f"memory {memory} 超出范围 {MEMORY_RANGE}")
        body = {"spec": {"template": {"spec": {"containers": [{"name": name, "resources": {
            "limits": {"cpu": cpu, "memory": memory},
        }}]}}}}
        self._apps.patch_namespaced_deployment(name, namespace, body)
        return self._ok("patch_resources", namespace, f"{name} → cpu={cpu} mem={memory}")

    async def delete_temp_file(self, *, namespace: str, path: str) -> dict:
        import asyncio

        self._check_ns(namespace)
        if not path.startswith(TEMP_PATH_PREFIX):
            raise ActionValidationError(f"路径必须在 {TEMP_PATH_PREFIX} 下: {path}")
        # 沙箱/执行环境删除临时文件（本地抽象：直接删，生产经 kubectl exec）
        proc = await asyncio.create_subprocess_exec("rm", "-f", path)
        await proc.communicate()
        return self._ok("delete_temp_file", namespace, f"已删除 {path}")

    def _ok(self, action: str, namespace: str, detail: str) -> dict:
        return {
            "action": action, "namespace": namespace, "ok": True, "detail": detail,
            "audit": {"ts": __import__("datetime").datetime.now().isoformat(), "actor": "agentflow"},
        }
