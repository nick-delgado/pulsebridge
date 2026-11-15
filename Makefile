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

.PHONY: help install hooks lint fmt test tf-validate tf-test secret-scan pre-commit check clean

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
	@dirs=$$( [ -d "$(TF_ROOT)" ] && find "$(TF_ROOT)" -name '*.tf' -not -path '*/.terraform/*' -exec dirname {} \; | sort -u || true ); \
	if [ -z "$$dirs" ]; then echo "tf-validate: no Terraform directories under $(TF_ROOT)/; nothing to validate"; exit 0; fi; \
	terraform fmt -check -recursive -diff "$(TF_ROOT)"; \
	for d in $$dirs; do \
		echo "== terraform validate $$d"; \
		terraform -chdir="$$d" init -backend=false -input=false -no-color > /dev/null; \
		terraform -chdir="$$d" validate -no-color; \
	done

# Terraform tests for every directory under $(TF_ROOT) that has a tests/ folder:
#   - `terraform test` (the *.tftest.hcl files, which plan against a mocked provider),
#   - every tests/*.sh static check (things `terraform test` cannot assert, such as "no key
#     resources are declared" or "this depends_on edge exists"),
#   - every tests/*.py check (for example the scan proving a password never reaches state).
# Everything runs with Google credentials removed from the environment and the metadata server
# pointed at a closed port: a mocked provider is not a guarantee, and with no credentials the real
# provider cannot reach a real project even if a mock fails to apply.
TF_NO_CREDS := env -u GOOGLE_APPLICATION_CREDENTIALS -u GOOGLE_CREDENTIALS \
	-u GOOGLE_OAUTH_ACCESS_TOKEN -u GOOGLE_IMPERSONATE_SERVICE_ACCOUNT -u GCP_SA_KEY_B64 \
	-u CLOUDSDK_CORE_PROJECT -u GOOGLE_CLOUD_PROJECT -u GOOGLE_PROJECT \
	CLOUDSDK_CONFIG=/nonexistent GCE_METADATA_HOST=127.0.0.1:9

tf-test: ## terraform test plus static checks for every Terraform directory with tests/
	@dirs=$$( [ -d "$(TF_ROOT)" ] && find "$(TF_ROOT)" -path '*/.terraform' -prune -o -type d -name tests -print | sort || true ); \
	if [ -z "$$dirs" ]; then echo "tf-test: no tests/ directories under $(TF_ROOT)/; nothing to test"; exit 0; fi; \
	for t in $$dirs; do \
		d=$$(dirname "$$t"); \
		ls "$$d"/*.tf > /dev/null 2>&1 || continue; \
		echo "== terraform test $$d"; \
		$(TF_NO_CREDS) terraform -chdir="$$d" init -backend=false -input=false -no-color > /dev/null || exit 1; \
		if ls "$$t"/*.tftest.hcl > /dev/null 2>&1; then \
			$(TF_NO_CREDS) terraform -chdir="$$d" test -no-color || exit 1; \
		fi; \
		for s in "$$t"/*.sh; do \
			[ -e "$$s" ] || continue; \
			echo "== $$s"; \
			$(TF_NO_CREDS) bash "$$s" || exit 1; \
		done; \
		for s in "$$t"/*.py; do \
			[ -e "$$s" ] || continue; \
			echo "== $$s"; \
			$(TF_NO_CREDS) python3 "$$s" || exit 1; \
		done; \
	done

# Secret scan of committed history. The pre-commit gitleaks hook only sees *staged* changes, so it
# protects a commit made on a machine with hooks installed, but not a commit made with
# `--no-verify` or from a clone without hooks. This target scans commits instead; CI runs it on
# every pull request's commits. SECRET_SCAN_RANGE is passed to `git log`: "HEAD" means the whole
# history, "<base>..<head>" means the commits a pull request adds.
# gitleaks is built from source at the pinned version with `go install` (Go verifies the module
# against its checksum database) into .venv/bin. Keep the version in step with the hook's `rev`
# in .pre-commit-config.yaml; tests/tooling/test_ci_workflow.py checks that they match.
GITLEAKS_VERSION := v8.30.1
GITLEAKS := $(BIN)/gitleaks
SECRET_SCAN_RANGE ?= HEAD
SECRET_SCAN_REPO ?= .

$(VENV)/.gitleaks-$(GITLEAKS_VERSION): | $(VENV)/.installed
	GOBIN="$(abspath $(BIN))" go install github.com/zricethezav/gitleaks/v8@$(GITLEAKS_VERSION)
	@touch $@

secret-scan: $(VENV)/.gitleaks-$(GITLEAKS_VERSION) ## gitleaks over git history (SECRET_SCAN_RANGE, default all)
	$(GITLEAKS) git --redact --no-banner --verbose --log-opts="$(SECRET_SCAN_RANGE)" "$(SECRET_SCAN_REPO)"

pre-commit: install ## Run every pre-commit hook against all files
	$(BIN)/pre-commit run --all-files --show-diff-on-failure

check: lint test tf-validate tf-test ## lint + test + tf-validate + tf-test (what CI runs)

clean: ## Remove the venv and tool caches
	rm -rf $(VENV) .pytest_cache .ruff_cache
	find . -name __pycache__ -type d -not -path './.worktrees/*' -prune -exec rm -rf {} +

# ---- Dashboard (web/) ---------------------------------------------------------------------------
# The single-page dashboard is a Node project (Vite + TypeScript); see web/README.md. These targets
# need Node 22.12 or newer and npm. They are deliberately NOT part of `check`: `check` is the
# Python and Terraform gate and must keep working on a machine without Node. CI runs the web
# targets in their own job ("Web (lint, test, build)"); run `make web-check` locally.
WEB_DIR := web
NPM ?= npm

.PHONY: web-install web-lint web-test web-browser-test web-build web-check

# `npm ci` installs exactly what package-lock.json lists; the stamp reruns it only when the lockfile
# changes.
$(WEB_DIR)/node_modules/.installed: $(WEB_DIR)/package-lock.json
	cd $(WEB_DIR) && $(NPM) ci --no-audit --no-fund
	@touch $@

web-install: $(WEB_DIR)/node_modules/.installed ## Install the dashboard's pinned npm packages

web-lint: web-install ## Dashboard: ESLint, Prettier check and TypeScript type check
	cd $(WEB_DIR) && $(NPM) run lint && $(NPM) run typecheck

web-test: web-install ## Dashboard: unit tests (Vitest)
	cd $(WEB_DIR) && $(NPM) test

web-browser-test: web-install ## Dashboard: browser test (Playwright, Chromium) against the mock build
	cd $(WEB_DIR) && $(NPM) run test:browser

web-build: web-install ## Dashboard: production build plus the bundle check
	cd $(WEB_DIR) && $(NPM) run build

web-check: web-lint web-test web-build web-browser-test ## Every dashboard check (needs Chromium for web-browser-test)
