.PHONY: help install dev test test-all test-core test-smoke test-all-WARN lint format clean bench bench-quick dashboard docker-build docker-shell docker-dashboard docker-mcp docker-test docker-bench-data docker-bench-write docker-bench-locomo docker-stop docker-restart docker-down docker-fix-perms docker-build-gpu docker-shell-gpu docker-dashboard-gpu docker-down-gpu

help:
	@echo "PMB development targets:"
	@echo "  make install        - pip install -e ."
	@echo "  make dev            - install + dev tools (pytest, ruff, textual)"
	@echo ""
	@echo "  make test           - full CI suite, excluding quarantined load-flaky tests"
	@echo "  make test-all       - same as make test"
	@echo "  make test-core      - fast deterministic engine/security subset"
	@echo "  make test-smoke     - lightweight import smoke tests only (~5s)"
	@echo "  make test-all-WARN  - backwards-compatible alias for make test-all"
	@echo ""
	@echo "  make lint           - ruff check"
	@echo "  make format         - ruff format"
	@echo "  make clean          - remove build artefacts and __pycache__"
	@echo "  make bench          - full LoCoMo benchmark (10 conversations, ~30 min)"
	@echo "  make bench-quick    - quick smoke benchmark (3 conversations, ~3 min)"
	@echo "  make dashboard      - launch web dashboard on :8765"
	@echo ""
	@echo "  Containerized mode (does not touch host Python or ~/.pmb):"
	@echo "  make docker-build     - build the pmb:local image"
	@echo "  make docker-shell     - dev sandbox (pip/python/pytest) in a container"
	@echo "  make docker-dashboard - web dashboard on :8765 from a container"
	@echo "  make docker-mcp       - print the MCP server command for agent config"
	@echo "  make docker-test      - run the core test suite inside the container"
	@echo "  make docker-bench-data   - download the LoCoMo dataset into scripts/data/"
	@echo "  make docker-bench-write  - write-path latency benchmark"
	@echo "  make docker-bench-locomo - LoCoMo recall@10 (n=3)"
	@echo "  make docker-stop      - stop containers without removing them"
	@echo "  make docker-restart   - restart the dashboard (no rebuild)"
	@echo "  make docker-down      - stop and remove containers"
	@echo "  make docker-fix-perms - repair model-cache/data ownership to your user"
	@echo "  (CPU by default; add -gpu for the CUDA build: docker-build-gpu,"
	@echo "   docker-shell-gpu, docker-dashboard-gpu — needs an NVIDIA GPU + toolkit)"

install:
	pip install -e .

dev:
	pip install -c requirements-dev.lock -e ".[dev,crypto]"

# --------------------------------------------------------------------------
# Tests. CI runs the whole suite on Linux, Windows, and macOS, excluding only
# tests explicitly marked `quarantined`. Keep the default local target aligned
# with that contract; `test-core` remains available for a fast edit loop.
# --------------------------------------------------------------------------

CORE_TESTS = tests/engine/test_graph.py tests/engine/test_persons.py tests/engine/test_goals_chains.py \
	tests/engine/test_fact_tree.py tests/recall/test_recall_cache.py tests/engine/test_config.py \
	tests/security/test_redact.py tests/recall/test_causation.py

test: test-all

test-all:
	pytest tests/ -q -m "not quarantined"

test-core:
	pytest $(CORE_TESTS) -q

test-smoke:
	pytest tests/meta/test_lightweight_imports.py -v

test-all-WARN: test-all

lint:
	ruff check src/ tests/ scripts/

format:
	ruff format src/ tests/ scripts/

clean:
	rm -rf build/ dist/ *.egg-info src/*.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type d -name .pytest_cache -exec rm -rf {} +
	find . -type d -name .ruff_cache -exec rm -rf {} +

bench:
	python scripts/benchmarks/benchmark_locomo.py --n-conversations 10 --top-k 10

bench-quick:
	python scripts/benchmarks/benchmark_locomo.py --n-conversations 3 --top-k 10


dashboard:
	pmb dashboard

# --------------------------------------------------------------------------
# Containerized mode. Optional alternative to the host pip install above —
# everything runs in the pmb:local image with data isolated in ./docker/data.
# Pass UID/GID so bind-mounted data stays owned by you:
#   make docker-build UID=$(id -u) GID=$(id -g)
# --------------------------------------------------------------------------
UID ?= $(shell id -u)
GID ?= $(shell id -g)
export UID
export GID

# Note: shell/dashboard/test do NOT depend on docker-build. compose builds the
# image automatically the first time (when pmb:local is missing) and reuses it
# afterwards, so these start instantly. Source is bind-mounted + installed
# editable, so code edits are picked up live with no rebuild. Run
# `make docker-build` explicitly only to force a rebuild (e.g. after changing
# dependencies in pyproject.toml).
docker-build:
	docker compose build shell

docker-shell:
	docker compose run --rm shell

docker-dashboard:
	docker compose --profile dashboard up

docker-mcp:
	@echo "Wire this into your agent's MCP config as the server command:"
	@echo "  docker compose run --rm -i mcp"

docker-test:
	docker compose run --rm shell pytest $(CORE_TESTS) -q

docker-bench-data:
	docker compose run --rm shell python scripts/_bench_data.py

docker-bench-write:
	docker compose run --rm shell python scripts/benchmarks/bench_qa_scenarios.py

docker-bench-locomo: docker-bench-data
	docker compose run --rm shell python scripts/benchmarks/benchmark_locomo.py --n-conversations 3 --top-k 10

docker-stop:
	docker compose --profile dev --profile dashboard --profile mcp stop

# Restart the dashboard without rebuilding. `--no-build` guarantees the
docker-restart:
	docker compose --profile dashboard up -d --no-build

docker-down:
	docker compose --profile dev --profile dashboard --profile mcp down

# Repair ownership of the model-cache volume(s) and ./docker/data to your user.
docker-fix-perms:
	@for vol in $$(docker volume ls -q | grep hf_cache); do \
	  docker run --rm -u 0 -v $$vol:/c alpine chown -R $(UID):$(GID) /c \
	    && echo "chowned volume $$vol -> $(UID):$(GID)"; \
	done
	-chown -R $(UID):$(GID) docker/data
	@echo "perms fixed for UID:GID = $(UID):$(GID)"

# GPU variant (opt-in). Needs an NVIDIA GPU + the NVIDIA Container Toolkit.
# Builds the CUDA torch image (pmb:gpu) and passes the GPU into the container.
GPU_COMPOSE = docker compose -f compose.yaml -f docker/compose.gpu.yaml

docker-build-gpu:
	$(GPU_COMPOSE) build shell

docker-shell-gpu:
	$(GPU_COMPOSE) run --rm shell

docker-dashboard-gpu:
	$(GPU_COMPOSE) --profile dashboard up

docker-down-gpu:
	$(GPU_COMPOSE) --profile dev --profile dashboard --profile mcp down
