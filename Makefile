# =============================================================================
# neiWangAgent-linux Makefile
# =============================================================================
.PHONY: install test lint clean init-demo run-demo warmup

PYTHON := python3
PIP := pip3

install:
	bash scripts/install.sh --user

install-system:
	bash scripts/install.sh --system

test:
	$(PYTHON) -m pytest tests/ -v

lint:
	$(PYTHON) -m ruff check src/

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name '*.pyc' -delete 2>/dev/null || true
	rm -rf build/ dist/ *.egg-info/

# ── Demo: 在 demo-project/ 中运行完整流程 ──
demo-project/:
	mkdir -p demo-project
	cd demo-project && git init && git config user.email "agent@neiwang.dev" && git config user.name "neiWangAgent"
	echo "# Demo Project" > demo-project/README.md
	cd demo-project && git add -A && git commit -m "chore: init demo project"

init-demo: demo-project/
	cd demo-project && $(PYTHON) -m agent_mcp.cli init

warmup: init-demo
	cd demo-project && $(PYTHON) -m agent_mcp.cli warmup

# 创建测试需求并运行
run-demo: warmup
	@echo "## Demo Task\n\n在 README.md 中添加一行 '## Features'\n然后添加 '- Automated code review'\n然后提交并推送" > demo-project/task.md
	cd demo-project && $(PYTHON) -m agent_mcp.cli run --task task.md

full-demo: run-demo
	@echo ""
	@echo "===== Demo 完成 ====="
	@cd demo-project && git log --oneline -3
