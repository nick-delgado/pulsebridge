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
# Installed in this order: the libraries first, sorted, then the services. Local packages are
# declared by name only and are not on PyPI, so each must already be installed when something
# that depends on it is: pulsebridge_gcp depends on pulsebridge_fhir, and "fhir" sorts before
# "gcp". $(sort) makes the order explicit; GNU make 3.82 to 4.2 return wildcard results unsorted.
PACKAGE_PYPROJECTS := $(sort $(wildcard libs/*/pyproject.toml)) $(sort $(wildcard services/*/pyproject.toml))

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

# ---- Terraform lint and security scan (T26) -----------------------------------------------------
# tflint (with the Google ruleset) and Trivy's misconfiguration scan over $(TF_ROOT). CI runs both
# on every pull request (.github/workflows/terraform-checks.yml). Neither needs credentials or a
# backend: tflint reads the configuration (call_module_type = "local" follows local module calls
# without `terraform init`), and Trivy parses the .tf files.
#
# The tools are downloaded from their GitHub releases at pinned versions and checked against the
# SHA-256 below before use (Linux x86_64 only; elsewhere install them yourself). Pinning the
# checksum here, rather than trusting the release's own checksums file, means a release asset
# replaced after the fact is refused. The versions were at least two weeks old when pinned, a
# margin against a just-published compromised release (Trivy's release tags were hijacked once,
# in 2026). The Google ruleset is unpacked straight into the plugin directory, so `tflint --init`
# (which calls the GitHub API) is not needed. Keep TFLINT_GOOGLE_VERSION equal to the plugin
# version in .tflint.hcl; tests/tooling/test_terraform_workflows.py checks that they match.
TOOLS_DIR := $(VENV)/tools
TFLINT_VERSION := v0.65.0
TFLINT_SHA256 := d4c545ed2e29d6338604b586f34f544f380f70f243aa72f7b484713835eaec61
TFLINT_GOOGLE_VERSION := 0.41.0
TFLINT_GOOGLE_SHA256 := efb3910bbc9f0b90bebd76d475ca6cd2cf04a032f6cddc8fa7a334e5011da6d6
TRIVY_VERSION := 0.76.1
TRIVY_SHA256 := c7cbe3137fb8a48a17574bede3dd6beb16742d1e9772929940fc90082c39d9c0
TFLINT_CONFIG ?= .tflint.hcl
TFLINT := $(TOOLS_DIR)/tflint-$(TFLINT_VERSION)/tflint
TFLINT_PLUGIN_DIR := $(TOOLS_DIR)/tflint-plugins
TFLINT_GOOGLE := $(TFLINT_PLUGIN_DIR)/github.com/terraform-linters/tflint-ruleset-google/$(TFLINT_GOOGLE_VERSION)/tflint-ruleset-google
TRIVY := $(TOOLS_DIR)/trivy-$(TRIVY_VERSION)/trivy
# Severities that fail tf-scan. MEDIUM and LOW findings are printed but accepted: the ones the
# stack has today are listed in infra/envs/README.md ("CI/CD", "What the checks accept").
TRIVY_FAIL_SEVERITY ?= HIGH,CRITICAL

.PHONY: tf-lint tf-scan

# $(call fetch,<url>,<sha256>,<output file>): download to <output>.download and move it into place
# only when its checksum matches. A mismatching download is left as .download and never used.
define fetch
	@if [ "$$(uname -sm)" != "Linux x86_64" ]; then \
		echo "$(3): only Linux x86_64 binaries are pinned here; install the tool yourself" >&2; exit 1; fi
	@mkdir -p "$(dir $(3))"
	curl -fsSL --retry 3 -o "$(3).download" "$(1)"
	@echo "$(2)  $(3).download" | sha256sum --check --strict --quiet \
		|| { echo "checksum mismatch for $(1)" >&2; exit 1; }
	@mv "$(3).download" "$(3)"
endef

$(TOOLS_DIR)/tflint_$(TFLINT_VERSION).zip:
	$(call fetch,https://github.com/terraform-linters/tflint/releases/download/$(TFLINT_VERSION)/tflint_linux_amd64.zip,$(TFLINT_SHA256),$@)

$(TFLINT): $(TOOLS_DIR)/tflint_$(TFLINT_VERSION).zip
	$(PYTHON) -m zipfile -e "$<" "$(dir $@)"
	@chmod +x "$@"

$(TOOLS_DIR)/tflint-ruleset-google_$(TFLINT_GOOGLE_VERSION).zip:
	$(call fetch,https://github.com/terraform-linters/tflint-ruleset-google/releases/download/v$(TFLINT_GOOGLE_VERSION)/tflint-ruleset-google_linux_amd64.zip,$(TFLINT_GOOGLE_SHA256),$@)

$(TFLINT_GOOGLE): $(TOOLS_DIR)/tflint-ruleset-google_$(TFLINT_GOOGLE_VERSION).zip
	$(PYTHON) -m zipfile -e "$<" "$(dir $@)"
	@chmod +x "$@"

$(TOOLS_DIR)/trivy_$(TRIVY_VERSION).tar.gz:
	$(call fetch,https://github.com/aquasecurity/trivy/releases/download/v$(TRIVY_VERSION)/trivy_$(TRIVY_VERSION)_Linux-64bit.tar.gz,$(TRIVY_SHA256),$@)

$(TRIVY): $(TOOLS_DIR)/trivy_$(TRIVY_VERSION).tar.gz
	@mkdir -p "$(dir $@)"
	tar -xzf "$<" -C "$(dir $@)" trivy
	@chmod +x "$@"

# --recursive lints every directory under $(TF_ROOT) that holds .tf files, each with the repo's
# .tflint.hcl. Run from inside $(TF_ROOT), so a checkout's .worktrees/ is never walked.
tf-lint: $(TFLINT) $(TFLINT_GOOGLE) ## tflint (Terraform and Google rulesets) over $(TF_ROOT)
	@if [ ! -d "$(TF_ROOT)" ]; then echo "tf-lint: no $(TF_ROOT)/; nothing to lint"; exit 0; fi
	cd "$(TF_ROOT)" && $(TF_NO_CREDS) TFLINT_PLUGIN_DIR="$(abspath $(TFLINT_PLUGIN_DIR))" \
		"$(abspath $(TFLINT))" --recursive --config="$(abspath $(TFLINT_CONFIG))" --format=compact

# --skip-check-update: use the checks embedded in the pinned binary, so a run does not depend on
# (or silently change with) the checks bundle Trivy would otherwise download. Two passes: every
# finding is listed for information, then the findings at $(TRIVY_FAIL_SEVERITY) fail the target.
tf-scan: $(TRIVY) ## Trivy misconfiguration scan of $(TF_ROOT) (fails on HIGH and CRITICAL)
	@if [ ! -d "$(TF_ROOT)" ]; then echo "tf-scan: no $(TF_ROOT)/; nothing to scan"; exit 0; fi
	$(TF_NO_CREDS) TRIVY_CACHE_DIR="$(abspath $(TOOLS_DIR))/trivy-cache" TRIVY_DISABLE_VEX_NOTICE=1 \
		"$(TRIVY)" config --skip-check-update --misconfig-scanners terraform --quiet \
		--severity UNKNOWN,LOW,MEDIUM,HIGH,CRITICAL --exit-code 0 "$(TF_ROOT)"
	$(TF_NO_CREDS) TRIVY_CACHE_DIR="$(abspath $(TOOLS_DIR))/trivy-cache" TRIVY_DISABLE_VEX_NOTICE=1 \
		"$(TRIVY)" config --skip-check-update --misconfig-scanners terraform --quiet \
		--severity $(TRIVY_FAIL_SEVERITY) --exit-code 1 "$(TF_ROOT)"

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

# ---- FHIR Validator (T08) -----------------------------------------------------------------------
# Runs the official HL7 FHIR Validator (pinned, checksum-verified) over libs/pulsebridge_fhir/examples/
# as FHIR R4, with codes checked by the public terminology server tx.fhir.org. Deliberately NOT part
# of `check`: it needs Java 17+ and the network (GitHub, packages.fhir.org, tx.fhir.org). CI runs it
# in its own workflow, .github/workflows/fhir-validate.yml. Pass options through FHIR_VALIDATE_ARGS,
# for example `make fhir-validate FHIR_VALIDATE_ARGS=--offline` (structure only, codes NOT checked).
FHIR_VALIDATE_ARGS ?=

.PHONY: fhir-validate

fhir-validate: ## FHIR Validator over the examples (needs Java 17+ and network; not in `check`)
	scripts/fhir-validate.sh $(FHIR_VALIDATE_ARGS)

# ---- Container images (T24) ---------------------------------------------------------------------
# Builds the same images as cloudbuild.yaml on a developer machine with Docker, without Cloud Build
# and without pushing anything. Every services/*/Dockerfile is built from the repository root (the
# Dockerfiles copy from libs/). Deliberately NOT part of `check`: it needs a Docker daemon and the
# network (Docker Hub for the base image, PyPI for the dependencies). See services/README.md.
#   make images                     build pulsebridge/<service>:local for every service
#   make images-smoke               start each built image with fake configuration and probe it
#   make images IMAGE_PIP_CERT=...  behind a TLS-inspecting proxy: a CA bundle for pip, passed as
#                                   a build secret (never stored in a layer); pass the same
#                                   IMAGE_PIP_CERT to images-smoke to check no copy is in an image
DOCKER ?= docker
IMAGE_PREFIX ?= pulsebridge
IMAGE_TAG ?= local
IMAGE_PIP_CERT ?=
IMAGE_SERVICES := $(patsubst services/%/Dockerfile,%,$(wildcard services/*/Dockerfile))
# Defined on its own line: a comma inside $(if ...) would split its arguments.
IMAGE_SECRET_FLAG := --secret "id=pip_cert,src=$(IMAGE_PIP_CERT)"

.PHONY: images images-smoke

images: ## Build every service image locally with Docker (not in `check`)
	@for s in $(IMAGE_SERVICES); do \
		echo "== $(DOCKER) build $(IMAGE_PREFIX)/$$s:$(IMAGE_TAG)"; \
		$(DOCKER) build --file "services/$$s/Dockerfile" --tag "$(IMAGE_PREFIX)/$$s:$(IMAGE_TAG)" \
			$(if $(IMAGE_PIP_CERT),$(IMAGE_SECRET_FLAG)) . || exit 1; \
	done

images-smoke: install ## Start each image built by `make images` and check it serves (not in `check`)
	PULSEBRIDGE_IMAGE_SMOKE=1 IMAGE_PREFIX="$(IMAGE_PREFIX)" IMAGE_TAG="$(IMAGE_TAG)" DOCKER="$(DOCKER)" \
		IMAGE_PIP_CERT="$(IMAGE_PIP_CERT)" \
		$(BIN)/pytest tests/tooling/test_image_smoke.py

# ---- End-to-end smoke test (T29) ----------------------------------------------------------------
# Runs against a DEPLOYED stack through its public front door (the fhir-proxy behind the Hosting
# rewrite) and asserts from the APIs: a simulated cuff reading through MLLP to the FHIR store,
# shared to OpenEMR, plus health-sync's first-sync checks on a real-data stack. Deliberately NOT
# part of `check`: it needs a deployment, the network and the reviewer password, and it writes a
# synthetic reading into the stack. Its own unit tests (against an in-process stack) run in
# `make test`. Configuration is by environment variable only (tests/e2e/README.md), for example:
#   PULSEBRIDGE_BASE_URL="$(terraform -chdir=infra/envs/dev output -raw dashboard_url)/api" \
#   SECRET_NAME_PREFIX="$(terraform -chdir=infra/envs/dev output -raw secret_name_prefix)" make smoke
# Extra flags go through SMOKE_ARGS, e.g. SMOKE_ARGS="--verbose --report-json /tmp/smoke.json".
SMOKE_ARGS ?=

.PHONY: smoke

smoke: install ## End-to-end smoke test of a deployed stack (not in `check`; tests/e2e/README.md)
	PYTHONPATH="$(abspath tests/e2e)" $(BIN)/python -m pulsebridge_smoke $(SMOKE_ARGS)
