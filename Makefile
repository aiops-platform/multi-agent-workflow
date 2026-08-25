.PHONY: install test lint demo api

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

# 跑脚本化 demo：create_run → approve → done
demo:
	./venv/bin/python -m agentflow.demo
