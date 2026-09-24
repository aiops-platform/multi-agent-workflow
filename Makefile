.PHONY: install test lint api doctor sandbox-image sync-workflows sync-agents retro-harvest codegraph

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

# 静态检查两件，都要过：
#   ruff         —— 代码风格/常见缺陷
#   lint-imports —— §11 分层契约（配置在 `.importlinter`）
#
# 为什么分层契约放这里而不是单独一个 target：§11 原文写着四条判据
# 「**应当写成测试**，比约定可靠」—— 接进 lint 才算真的"写成了检查"；
# 单独一个 `make lint-layers` 只会变成又一条没人跑的约定。
lint:
	./venv/bin/ruff check agentflow tests
	./venv/bin/lint-imports

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
# ⚠️ 只同步 workflow；数据面（MCP server + agent 绑定）走下面的 `sync-agents`。
sync-workflows:
	@test -n "$(TENANT)" || { echo "用法：make sync-workflows TENANT=<租户id>"; exit 1; }
	$(PY) scripts/push_seed_workflows.py --tenant $(TENANT) $(if $(DRY),--dry-run,)

# 把 seed 的**数据面**推到**已开通租户**：MCP server 注册 + agent 绑定 + 自定义 agent。
#
# 为什么需要这条：与 workflow 同一个坑（上面那条），但**后果更隐蔽**。绑定缺行 ⇒
# `mcp_server_ids` 为 NULL ⇒ 该 agent **零工具**，而它的提示词照旧点名要调 MCP 工具
# —— 模型于是把工具调用写成纯文本，最终表现为「agent 未输出合法 JSON」，
# **中间没有任何一步会报"绑定缺失"**。实测代价见脚本头部（`run_62f21fa82f`）。
#
#   make sync-agents TENANT=otr
#   make sync-agents TENANT=otr DRY=1
#
# 语义：**并集，只加不删**（租户自己加的 server 绑定会被保留）。
sync-agents:
	@test -n "$(TENANT)" || { echo "用法：make sync-agents TENANT=<租户id>"; exit 1; }
	$(PY) scripts/push_seed_agents.py --tenant $(TENANT) $(if $(DRY),--dry-run,)

# 经验回收：把一段时间内的提交「为什么」/ changelog / TODO 变更收拢成素材，
# 交给 LLM 按 `docs/lessons/README.md` 的收录标准做三分类。
#
#   make retro-harvest SINCE="2 weeks ago"
#   make retro-harvest SINCE="1 month ago" REPOS="../backend ../service-intelligence-platform-ui"
#
# ⚠️ **脚本不做判断** —— 三分类（可机器化 / 只能文档 / 丢弃）是 LLM 的活，它会打印出
# 可直接粘贴的指令。为什么是"回收"而不是"让人记录"：见脚本头部。
retro-harvest:
	$(PY) scripts/retro_harvest.py --since "$(if $(SINCE),$(SINCE),2 weeks ago)" $(foreach r,$(REPOS),--repo $(r))

# 代码索引（codegraph）—— **团队共用的是约定，索引本身每台机器各自建**。
#
#   make codegraph          # 没索引就建，有就增量同步，最后报状态
#   make codegraph MCP=1    # 顺带把 MCP server 接进 agent（**一次性**，改本机配置）
#
# 为什么索引不入库：它自带的 `.codegraph/.gitignore` 注释写着
# 「local to each machine, not for committing」—— 而且索引**必须与代码同步**，
# 入库的索引对任何改过代码的人立刻就过期。所以入库的只有那条忽略规则。
#
# 为什么钉版本号：同一台机器上两个版本的索引器交替写同一个 db 是没意义的。
# 这个版本号与 `.claude/hooks/codegraph-sync.sh` 里的一致，改要一起改。
CG := npx -y @colbymchenry/codegraph@1.6.0
codegraph:
	@command -v npx >/dev/null 2>&1 || { \
	  echo "需要 Node ≥22（npx 不在 PATH）。装好后重跑，或 `make doctor` 看版本基线。"; exit 1; }
	@if [ -d .codegraph ]; then \
	  echo "== 增量同步 =="; $(CG) sync . ; \
	else \
	  echo "== 首次建索引 =="; $(CG) init . ; \
	fi
	@echo
	@$(CG) status . | tail -22
	@if [ -n "$(MCP)" ]; then \
	  echo; echo "== 接进 agent（改本机配置）=="; $(CG) install --yes ; \
	fi
	@echo
	@echo "提示：索引靠 MCP server 常驻时自动跟（会话期间），会话之间由"
	@echo "      .claude/hooks/codegraph-sync.sh 在下次开会话时补齐。"
