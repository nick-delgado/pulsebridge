# PulseBridge developer commands. Run `make help` for the list.
#
# The same targets run locally and in CI (.github/workflows/ci.yml), so "it passed on my machine"
# and "it passed in CI" mean the same thing.

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

PYTHON ?= python3
VENV ?= .venv
BIN := $(VENV)/bin

# Paths the targets act on. Overridable so the tooling tests can point them at a scratch directory.
PY_PATHS ?= .
# Empty means "use testpaths from pyproject.toml".
TEST_PATHS ?=
TF_ROOT ?= infra

# Every library and service with its own pyproject.toml is installed editable into the venv.
PACKAGE_PYPROJECTS := $(wildcard libs/*/pyproject.toml services/*/pyproject.toml)

.PHONY: help install hooks lint fmt test tf-validate pre-commit check clean

help: ## List the targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-12s %s\n", $$1, $$2}'

# The stamp file is rebuilt only when a pyproject.toml changes, so `make lint` stays fast.
$(VENV)/.installed: pyproject.toml $(PACKAGE_PYPROJECTS)
	$(PYTHON) -m venv $(VENV)
	$(BIN)/python -m pip install --quiet --upgrade "pip>=25.1"
	$(BIN)/python -m pip install --quiet --group dev
	@for p in $(PACKAGE_PYPROJECTS); do \
		echo "installing $$(dirname $$p) (editable)"; \
		$(BIN)/python -m pip install --quiet -e "$$(dirname $$p)"; \
	done
	@touch $@

install: $(VENV)/.installed ## Create .venv with the pinned dev tools and every lib/service

hooks: install ## Install the git pre-commit hooks (ruff, secret scan, ...)
	$(BIN)/pre-commit install

lint: install ## Check Python style and errors with ruff (no changes made)
	$(BIN)/ruff check $(PY_PATHS)
	$(BIN)/ruff format --check $(PY_PATHS)

fmt: install ## Auto-fix: ruff fixes and formatting, terraform fmt
	$(BIN)/ruff check --fix $(PY_PATHS)
	$(BIN)/ruff format $(PY_PATHS)
	@if [ -d "$(TF_ROOT)" ]; then terraform fmt -recursive "$(TF_ROOT)"; fi

# pytest exits with code 5 when it collects no tests. That is expected while libs/ and services/
# are still empty, so it counts as a pass; every other non-zero code (a failing test, a
# collection error) fails the target.
test: install ## Run pytest over tests/, libs/ and services/
	@rc=0; $(BIN)/pytest $(TEST_PATHS) || rc=$$?; \
	if [ $$rc -eq 5 ]; then echo "make test: no tests collected; treating as a pass"; exit 0; fi; \
	exit $$rc

# Validates every directory under $(TF_ROOT) that holds .tf files (env roots, modules,
# bootstrap). `init -backend=false` downloads providers without touching remote state, so this
# needs no cloud credentials. With no Terraform yet, it reports that and passes.
tf-validate: ## terraform fmt -check and validate for every Terraform directory
	@dirs=$$( [ -d "$(TF_ROOT)" ] && find "$(TF_ROOT)" -name '*.tf' -not -path '*/.terraform/*' -printf '%h\n' | sort -u || true ); \
	if [ -z "$$dirs" ]; then echo "tf-validate: no Terraform directories under $(TF_ROOT)/; nothing to validate"; exit 0; fi; \
	terraform fmt -check -recursive -diff "$(TF_ROOT)"; \
	for d in $$dirs; do \
		echo "== terraform validate $$d"; \
		terraform -chdir="$$d" init -backend=false -input=false -no-color > /dev/null; \
		terraform -chdir="$$d" validate -no-color; \
	done

pre-commit: install ## Run every pre-commit hook against all files
	$(BIN)/pre-commit run --all-files --show-diff-on-failure

check: lint test tf-validate ## lint + test + tf-validate (what CI runs)

clean: ## Remove the venv and tool caches
	rm -rf $(VENV) .pytest_cache .ruff_cache
	find . -name __pycache__ -type d -not -path './.worktrees/*' -prune -exec rm -rf {} +
