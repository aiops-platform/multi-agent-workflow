.PHONY: install test lint api

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
