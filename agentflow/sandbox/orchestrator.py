# -*- coding: utf-8 -*-
"""SandboxOrchestrator：动态拉起/销毁沙箱 Pod（design §4.1 / §10.2）。

推理容器（Worker/Agent）与执行容器（沙箱）分离：沙箱是独立 Pod，非特权、
受限 Capabilities，挂载工作区（RWX PVC 或 emptyDir），只暴露 exec 端口。
```
Worker/Agent (推理, 只读)
   │ SandboxClient (HTTP/gRPC)
   ▼
沙箱 Pod (opensandbox 或自研 exec 服务; 代码写/编译/测试/Shell)
```
"""
from __future__ import annotations

import logging

from kubernetes import client, config as k8s_config

log = logging.getLogger("agentflow.sandbox")

SANDBOX_IMAGE = "agentflow-sandbox:latest"
SANDBOX_PORT = 44772


class SandboxPodError(RuntimeError):
    pass


class SandboxOrchestrator:
    """用 K8s API 创建/销毁沙箱 Pod。本地用 kubeconfig，生产用 in-cluster。"""

    def __init__(
        self,
        *,
        namespace: str = "agentflow",
        image: str = SANDBOX_IMAGE,
        service_account: str | None = None,
    ) -> None:
        try:
            k8s_config.load_incluster_config()
        except Exception:  # noqa: BLE001 - 本地回退 kubeconfig
            k8s_config.load_kube_config()
        self._core = client.CoreV1Api()
        self.namespace = namespace
        self.image = image
        self.service_account = service_account

    # ------------------------------------------------------------------
    def build_pod_spec(self, run_id: str, tenant_id: str, service: str, workspace_pvc: str | None) -> client.V1Pod:
        """构造沙箱 Pod（§10.2 安全基线 + 资源限制）。"""
        labels = {"app": "agentflow-sandbox", "run": run_id, "tenant": tenant_id, "service": service}
        workspace_volume, workspace_mount = self._workspace(run_id, workspace_pvc)
        container = client.V1Container(
            name="sandbox",
            image=self.image,
            image_pull_policy="IfNotPresent",
            ports=[client.V1ContainerPort(container_port=SANDBOX_PORT)],
            command=["python", "-m", "uvicorn", "agentflow.sandbox.exec_service:app",
                     "--host", "0.0.0.0", "--port", str(SANDBOX_PORT)],
            resources=client.V1ResourceRequirements(
                requests={"cpu": "1", "memory": "2Gi"},
                limits={"cpu": "2", "memory": "4Gi"},  # §10.2 resource_limits
            ),
            security_context=client.V1SecurityContext(
                privileged=False,
                allow_privilege_escalation=False,
                capabilities=client.V1Capabilities(drop=["ALL"]),
            ),
            volume_mounts=[
                client.V1VolumeMount(name="workspace", mount_path="/workspace"),
                client.V1VolumeMount(name="tmp", mount_path="/tmp"),
            ] + ([workspace_mount] if workspace_mount else []),
        )
        spec = client.V1PodSpec(
            containers=[container],
            volumes=[
                workspace_volume,
                client.V1Volume(name="tmp", empty_dir={}),
            ],
            restart_policy="Never",
            service_account_name=self.service_account,
        )
        return client.V1Pod(
            metadata=client.V1ObjectMeta(
                name=f"sandbox-{run_id}-{service}".replace("_", "-")[:60],
                namespace=self.namespace, labels=labels,
            ),
            spec=spec,
        )

    @staticmethod
    def _workspace(run_id: str, workspace_pvc: str | None) -> tuple[client.V1Volume, client.V1VolumeMount | None]:
        if workspace_pvc:
            return (
                client.V1Volume(name="workspace", persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(claim_name=workspace_pvc)),
                None,
            )
        # 默认 per-run emptyDir（本地 MVP；生产用 RWX PVC 共享给 Agent 只读）
        return client.V1Volume(name="workspace", empty_dir={}), None

    # ------------------------------------------------------------------
    async def create(self, run_id: str, tenant_id: str, service: str, workspace_pvc: str | None = None) -> dict:
        """拉起沙箱 Pod 并等待就绪，返回 (pod_name, base_url)。"""
        pod = self.build_pod_spec(run_id, tenant_id, service, workspace_pvc)
        try:
            created = self._core.create_namespaced_pod(self.namespace, pod)
        except client.ApiException as exc:
            raise SandboxPodError(f"创建沙箱 Pod 失败: {exc}") from exc
        name = created.metadata.name
        log.info("[%s] 沙箱 Pod %s 已创建（%s）", run_id, name, service)

        ip = await self._wait_ready(name, timeout=180)
        return {"pod": name, "ip": ip, "base_url": f"http://{ip}:{SANDBOX_PORT}"}

    async def _wait_ready(self, name: str, timeout: int = 180) -> str:
        import asyncio

        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            pod = self._core.read_namespaced_pod(name, self.namespace)
            phase = pod.status.phase
            ip = pod.status.pod_ip
            if phase == "Running" and ip:
                # 服务就绪检查由 SandboxClient 完成；这里只等 Pod Running
                return ip
            await asyncio.sleep(2)
        raise SandboxPodError(f"沙箱 Pod {name} 未在 {timeout}s 内 Running")

    async def destroy(self, pod_name: str) -> bool:
        try:
            self._core.delete_namespaced_pod(pod_name, self.namespace)
            return True
        except client.ApiException as exc:
            if exc.status == 404:
                return False
            raise SandboxPodError(f"销毁沙箱 Pod 失败: {exc}") from exc
