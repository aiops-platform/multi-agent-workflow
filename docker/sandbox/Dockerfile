# agentflow-sandbox（基础镜像）：沙箱执行 Pod 镜像（design §4.1 / §10.2）
# exec 服务纯 Python stdlib 实现（http.server），零 pip 依赖 → 完全离线可建。
#
# **本镜像刻意保持最小**：只跑 exec 服务，不含任何语言工具链、不含 git、不含密钥。
# 需要"能编译/跑某个服务的测试"时，用按 runtime 分出的变体镜像叠加，例如
# `docker/sandbox/Dockerfile.java21`（FROM 本镜像 + JDK21）。
#
# 为什么不再用 `--build-arg WITH_JDK=1`：工具链与"零依赖、离线可建"这条约束是矛盾的，
# 揉进一个 Dockerfile 会让基础镜像的构建条件变得看开关。拆成独立变体后，
# 基础镜像**永远**可离线构建，而变体按需联网、按 runtime 各建一个。
FROM python:3.12-slim

# 注：python:3.12-slim 自带 bash 与 ca-certificates（实测 /etc/ssl/certs 301 条）

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
