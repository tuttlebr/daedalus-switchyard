SHELL := /bin/bash
.DEFAULT_GOAL := validate

CHART_DIR := charts/switchyard
ROUTES_FILE ?=
SWITCHYARD_COMMIT := 336196f6fbfc97ddc71c1700f6092e564e9f23c2
IMAGE_REPOSITORY ?= daedalus.ddns.me:5050/btuttle/switchyard
IMAGE_TAG ?= v0.3.0
IMAGE_PLATFORM ?= linux/amd64
LOCAL_IMAGE ?= $(IMAGE_REPOSITORY):$(IMAGE_TAG)
EXPECTED_CONTEXT ?= kubernetes-admin@kubernetes
ROUTE_FILES := $(wildcard routes_*.toml)
ROUTE_VARIANTS := $(patsubst routes_%.toml,%,$(ROUTE_FILES))
DEPLOY_ROUTE_TARGETS := $(addprefix deploy-route_,$(ROUTE_VARIANTS))
TEST_BASE_URL ?=
TEST_NAMESPACE ?= daedalus
TEST_SERVICE ?= switchyard
TEST_TIMEOUT ?= 120
TEST_MAX_OUTPUT_TOKENS ?= 4096

# Treat a TOML goal as the test input, including nonexistent paths so the
# script can report a useful file error. Do not swallow unrelated Make goals.
TEST_ROUTES_FILE := $(ROUTES_FILE)
ifneq ($(filter test,$(MAKECMDGOALS)),)
TEST_ROUTE_ARGS := $(filter %.toml,$(MAKECMDGOALS))
ifneq ($(word 2,$(TEST_ROUTE_ARGS)),)
$(error Use one TOML file: make test routes_PROVIDER.toml)
endif
ifneq ($(TEST_ROUTE_ARGS),)
TEST_ROUTES_FILE := $(TEST_ROUTE_ARGS)
.PHONY: $(TEST_ROUTE_ARGS)
$(TEST_ROUTE_ARGS):
	@:
endif
endif

.PHONY: validate test helm-lint helm-template image publish deploy deploy-existing $(DEPLOY_ROUTE_TARGETS)

validate: helm-lint helm-template

test:
	@test -n "$(TEST_ROUTES_FILE)" || { echo 'Select a provider with ROUTES_FILE or a TOML argument. Available: $(ROUTE_FILES)' >&2; exit 64; }
	@python3 scripts/test_routes.py "$(TEST_ROUTES_FILE)" \
		--base-url "$(TEST_BASE_URL)" \
		--context "$(EXPECTED_CONTEXT)" \
		--namespace "$(TEST_NAMESPACE)" --service "$(TEST_SERVICE)" \
		--timeout "$(TEST_TIMEOUT)" --max-output-tokens "$(TEST_MAX_OUTPUT_TOKENS)"

helm-lint:
	@test -n "$(ROUTES_FILE)" || { echo 'Set ROUTES_FILE. Available: $(ROUTE_FILES)' >&2; exit 64; }
	@set -euo pipefail; \
	routes_tmp_dir="$$(mktemp -d)"; trap 'rm -rf -- "$$routes_tmp_dir"' EXIT; \
	bash scripts/render_routes.sh "$(ROUTES_FILE)" "$$routes_tmp_dir/routes.toml"; \
	api_key_envs_json="$$(python3 scripts/list_api_key_envs.py --json "$$routes_tmp_dir/routes.toml")"; \
	helm lint $(CHART_DIR) \
		--set-json "apiKeySecret.keys=$${api_key_envs_json}" \
		--set-file "config.routesToml=$$routes_tmp_dir/routes.toml"

helm-template:
	@test -n "$(ROUTES_FILE)" || { echo 'Set ROUTES_FILE. Available: $(ROUTE_FILES)' >&2; exit 64; }
	@set -euo pipefail; \
	routes_tmp_dir="$$(mktemp -d)"; trap 'rm -rf -- "$$routes_tmp_dir"' EXIT; \
	bash scripts/render_routes.sh "$(ROUTES_FILE)" "$$routes_tmp_dir/routes.toml"; \
	api_key_envs_json="$$(python3 scripts/list_api_key_envs.py --json "$$routes_tmp_dir/routes.toml")"; \
	helm template switchyard $(CHART_DIR) \
		--namespace daedalus \
		--set-json "apiKeySecret.keys=$${api_key_envs_json}" \
		--set-file "config.routesToml=$$routes_tmp_dir/routes.toml" \
		> /tmp/switchyard-rendered.yaml
	kubectl apply --dry-run=client --validate=false -f /tmp/switchyard-rendered.yaml >/dev/null

image:
	docker build --pull \
		--platform $(IMAGE_PLATFORM) \
		--build-arg SWITCHYARD_COMMIT=$(SWITCHYARD_COMMIT) \
		--tag $(LOCAL_IMAGE) .

publish: image
	docker push $(LOCAL_IMAGE)

deploy:
	./scripts/deploy.sh --build-publish

# Caller environment and command-line Make variables are exported by Make.
# Do not export the empty ROUTES_FILE default over an operator's saved selection.
deploy-existing:
	./scripts/deploy.sh

$(DEPLOY_ROUTE_TARGETS): deploy-route_%: routes_%.toml
	$(MAKE) deploy ROUTES_FILE=$<
