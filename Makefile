.PHONY: install test lint api doctor sandbox-image sync-workflows

# 变量：此前几个目标各自硬编码 ./venv/bin/...，而 `doctor` 用了没定义的 $(PY)
# —— 展开成空，于是它去执行脚本文件本身（`Permission denied`）。**这个目标从加进来
# 那天起就没跑通过**，而文档里把它写成了"换机器先跑这个"。定义在这里，一处收口。
PY       := ./venv/bin/python
# 容器 CLI：**podman 优先**。macOS 上 `docker` 常常是 podman 的兼容壳（两者共用同一个
# 镜像存储），而那种情况下 `docker compose` 子命令往往不可用。
CONTAINER ?= $(shell command -v podman >/dev/null 2>&1 && echo podman || echo docker)

install:
	python3 -m venv venv
	./venv/bin/pip install -e ".[dev]"

test:
	./venv/bin/pytest -q

lint:
	./venv/bin/ruff check agentflow tests

# 跑一个带审批的完整流程（控制面 API）
api:
	./venv/bin/uvicorn agentflow.api.app:app --reload --port 8000

# 注：原先的 `make demo`（agentflow/demo.py）已删除。它读的是仓库里的
# `workflows/bug-fix-pipeline.yaml`——而那**不是运行时的真源**（workflow 存数据库，
# 见 CLAUDE.md「工作流的真源」）。留着一个读文件的 demo 会让人以为改 YAML 就生效。

# 环境体检（换机器时先跑这个）。INSTALL=1 时把能自动装的装上（gh CLI）。
#   make doctor
#   make doctor INSTALL=1
doctor:
	$(PY) scripts/doctor.py $(if $(INSTALL),--install,)

# 构建沙箱镜像（基础 → JDK21 变体）。**换机器必须先跑这一步**：
# compose 的 sandbox 服务只声明了 image、没有 build，镜像不在就起不来，
# 而它起不来的症状是 ws_write_file / ws_run_tests 一律 fail-closed
# （修复不落盘、测试一条不跑），不是一句"沙箱没起来"。
#
#   make sandbox-image
#
# 幂等：已存在也照建，层缓存会让它很快。两步**有先后**（java21 FROM 基础镜像）。
sandbox-image:
	$(CONTAINER) build -t localhost/agentflow-sandbox:local -f docker/sandbox/Dockerfile .
	$(CONTAINER) build -t localhost/agentflow-sandbox-java21:local \
	    -f docker/sandbox/Dockerfile.java21 .
	@echo "✓ 沙箱镜像就绪：localhost/agentflow-sandbox-java21:local"

# 把 seed 里的 workflow 推到**已开通租户**。
#
# 为什么需要这条：workflow 的真源是**数据库**，而 seed 是「空表才播、绝不覆盖」——
# 改了 `agentflow/seed/workflows/*.yaml` 对新租户自动生效，**对已开通租户毫无效果、
# 也没有任何提示**。这是本仓反复踩的一个坑（CLAUDE.md §6.0）。
#
#   make sync-workflows TENANT=otr
#   make sync-workflows TENANT=otr DRY=1
#
# ⚠️ 只同步 workflow；agent 定义与 MCP 绑定另有自己的一套（见脚本头部说明）。
sync-workflows:
	@test -n "$(TENANT)" || { echo "用法：make sync-workflows TENANT=<租户id>"; exit 1; }
	$(PY) scripts/push_seed_workflows.py --tenant $(TENANT) $(if $(DRY),--dry-run,)
