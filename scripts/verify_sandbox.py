# -*- coding: utf-8 -*-
"""M4 沙箱部署验证：SandboxOrchestrator 拉起沙箱 Pod → SandboxClient 执行 → 销毁。

用法（先构建镜像 + load 进 minikube）：
    docker build -t agentflow-sandbox:latest -f docker/sandbox/Dockerfile .
    minikube image load agentflow-sandbox:latest
    ./venv/bin/python scripts/verify_sandbox.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentflow.sandbox.client import SandboxClient  # noqa: E402
from agentflow.sandbox.orchestrator import SandboxOrchestrator  # noqa: E402


async def main() -> None:
    orch = SandboxOrchestrator(namespace="agentflow")
    # 确保 namespace 存在
    try:
        orch._core.read_namespace("agentflow")
    except Exception:  # noqa: BLE001
        orch._core.create_namespace({"metadata": {"name": "agentflow"}})

    print("==> 拉起沙箱 Pod ...")
    info = await orch.create("run_demo", "team-alpha", "order-service")
    print(f"    pod={info['pod']} base_url={info['base_url']}")

    client = SandboxClient(info["base_url"])
    try:
        print("==> health:")
        print("   ", json.dumps(await client.health(), ensure_ascii=False))

        print("==> run_python:")
        r = await client.run_python("print('沙箱 python OK', 6*7)")
        print(f"    rc={r.rc} stdout={r.stdout!r} stderr={r.stderr!r}")

        print("==> run_shell (java version):")
        r = await client.run_shell("python3 --version && java -version 2>&1")
        print(f"    rc={r.rc} stdout={r.stdout.strip()!r}")

        print("==> write_file (/workspace):")
        w = await client.write_file("/workspace/fix.py", "def patch(): return 'fixed'")
        print("   ", json.dumps(w))

        print("==> 超时限制（sleep 2, timeout 1）:")
        r = await client.run_shell("sleep 2", timeout=1)
        print(f"    timed_out={r.timed_out} rc={r.rc}")
    finally:
        await client.aclose()

    print("==> 销毁沙箱 Pod ...")
    ok = await orch.destroy(info["pod"])
    print(f"    destroyed={ok}")
    print("\n✅ 沙箱端到端验证完成")


if __name__ == "__main__":
    asyncio.run(main())
