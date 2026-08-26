# agentflow-sandbox：沙箱执行 Pod 镜像（design §4.1 / §10.2）
# exec 服务纯 Python stdlib 实现（http.server），零 pip 依赖 → 完全离线可建。
FROM python:3.12-slim

# Java 21（可选，编译 Java 用；apt 下载 ~250MB 依赖网络，默认关以离线构建）
# 注：python:3.12-slim 自带 bash
ARG WITH_JDK=0
RUN if [ "$WITH_JDK" = "1" ]; then \
        apt-get update && apt-get install -y --no-install-recommends \
            openjdk-21-jdk-headless git \
        && rm -rf /var/lib/apt/lists/*; \
    fi

# 拷贝 exec 服务（stdlib-only，无第三方依赖）
WORKDIR /opt/agentflow
COPY agentflow/__init__.py agentflow/__init__.py
COPY agentflow/sandbox/__init__.py agentflow/sandbox/__init__.py
COPY agentflow/sandbox/exec_service.py agentflow/sandbox/exec_service.py

# §10.2 默认限制（可用环境变量覆盖）
ENV SBX_MAX_EXEC_SECONDS=300 \
    SBX_MAX_STDOUT_BYTES=1048576 \
    SBX_MAX_CONCURRENT=10 \
    SBX_WRITABLE=/workspace:/tmp

EXPOSE 44772
CMD ["python", "-m", "agentflow.sandbox.exec_service"]
