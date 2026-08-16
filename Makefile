# va-legal-agent test runner
#
#   make              run the full suite (default)
#   make test-search  fast slice: search parsing, providers, query expansion
#   make test-fetch   fast slice: fetching, enrichment, agent pipeline
#   make test-cli     fast slice: CLI output formats, events, batch tracking
#   make test-config  fast slice: environment parsing and typed settings
#   make test-core    fast slice: ranking, impact, interpretation, llm, topics
#   make test-w       full suite with deprecation warnings promoted to errors
#   make lint         ruff static checks
#   make lint-fix     auto-fix what ruff can
#   make coverage     full suite with coverage report
#   make mutate       mutation-testing pass (mutmut) over every module
#   make mutate-check full mutation pass + the kill-property baseline gate
#   make smoke        one real query per configured provider (manual network check)
#   make help         list all targets
#
# Override the interpreter or pass pytest args, e.g.:
#   make PYTHON=python3
#   make test ARGS="-k telemetry"
#   make smoke QUERY="tinnitus"

PYTHON ?= .venv/bin/python
PYTEST = $(PYTHON) -m pytest
ARGS =
QUERY =

.PHONY: test test-search test-fetch test-cli test-config test-core test-w lint lint-fix coverage mutate mutate-check smoke help

test: ## Run the full test suite
	$(PYTEST) -q $(ARGS)

test-search: ## Search parsing, providers, and query expansion
	$(PYTEST) -q tests/test_search.py tests/test_providers.py $(ARGS)

test-fetch: ## Fetching, enrichment, and the agent pipeline
	$(PYTEST) -q tests/test_fetch.py tests/test_legal_agent.py tests/test_research.py $(ARGS)

test-cli: ## CLI output formats, structured events, and batch tracking
	$(PYTEST) -q tests/test_cli.py tests/test_batch.py $(ARGS)

test-config: ## Environment parsing and typed settings
	$(PYTEST) -q tests/test_config.py $(ARGS)

test-core: ## Ranking, impact, interpretation, LLM, and topic tables
	$(PYTEST) -q tests/test_ranking.py tests/test_impact.py tests/test_interpretation.py tests/test_llm.py tests/test_topics.py $(ARGS)

test-w: ## Full suite with warnings promoted to errors
	PYTHONWARNINGS=error $(PYTEST) -q $(ARGS)

lint: ## Static checks with ruff (pyflakes + pycodestyle rules)
	$(PYTHON) -m ruff check va_legal_agent tests scripts

lint-fix: ## Auto-fix what ruff can
	$(PYTHON) -m ruff check --fix va_legal_agent tests scripts

coverage: ## Full suite with a per-module coverage report (line + branch)
	$(PYTEST) --cov=va_legal_agent --cov-branch --cov-report=term-missing $(ARGS)

# module -> test slice pairings for the per-module mutation pass; each module
# is mutated against the tests that exercise it (see scripts/mutmut_pass.py).
MUTATE_MODULES = \
	"search.py tests/test_search.py" \
	"queries.py tests/test_providers.py tests/test_search.py" \
	"providers.py tests/test_providers.py tests/test_search.py" \
	"fetch.py tests/test_fetch.py tests/test_legal_agent.py" \
	"agent.py tests/test_legal_agent.py tests/test_research.py" \
	"batch.py tests/test_batch.py tests/test_cli.py" \
	"config.py tests/test_config.py tests/test_cli.py" \
	"models.py tests/test_cli.py tests/test_batch.py" \
	"topics.py tests/test_topics.py tests/test_legal_agent.py" \
	"interpretation.py tests/test_interpretation.py tests/test_topics.py" \
	"llm.py tests/test_llm.py" \
	"impact.py tests/test_impact.py" \
	"ranking.py tests/test_ranking.py"

mutate: ## Mutation-testing pass (mutmut) over every module; survivors to /tmp
	@for spec in $(MUTATE_MODULES); do \
		$(PYTHON) scripts/mutmut_pass.py $$spec; \
	done

mutate-check: mutate ## Full mutation pass + kill-property baseline gate (fails on untriaged survivors)
	$(PYTHON) scripts/check_mutation_baseline.py

smoke: ## One real query per configured provider (manual network sanity check)
	$(PYTHON) scripts/smoke_search.py $(QUERY)

help: ## List available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-12s %s\n", $$1, $$2}'
