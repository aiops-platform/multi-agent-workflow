# agentflow-sandbox：沙箱执行 Pod 镜像（design §4.1 / §10.2）
# 含 Python/Bash/Java 工具链 + exec 服务（agentflow.sandbox.exec_service）
FROM python:3.12-slim

# Java 21（fix-implementer/tester 编译 Java 用）
RUN apt-get update && apt-get install -y --no-install-recommends \
        openjdk-21-jdk-headless \
        bash \
        git \
    && rm -rf /var/lib/apt/lists/*

# exec 服务依赖
RUN pip install --no-cache-dir fastapi uvicorn httpx pydantic

# 拷贝 exec 服务（自包含，仅依赖 fastapi/pydantic）
WORKDIR /opt/agentflow
COPY agentflow/sandbox/exec_service.py agentflow/sandbox/exec_service.py
COPY agentflow/sandbox/__init__.py agentflow/sandbox/__init__.py
COPY agentflow/__init__.py agentflow/__init__.py

# §10.2 默认限制（可用环境变量覆盖）
ENV SBX_MAX_EXEC_SECONDS=300 \
    SBX_MAX_STDOUT_BYTES=1048576 \
    SBX_MAX_CONCURRENT=10 \
    SBX_WRITABLE=/workspace:/tmp

EXPOSE 44772
CMD ["python", "-m", "uvicorn", "agentflow.sandbox.exec_service:app", "--host", "0.0.0.0", "--port", "44772"]
