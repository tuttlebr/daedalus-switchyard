SHELL := /bin/bash
.DEFAULT_GOAL := validate

CHART_DIR := charts/switchyard
ROUTES_FILE := routes.toml
SWITCHYARD_COMMIT := 1fc9ab887d1c663b0048ae24d5f473d15ed8daaa
IMAGE_TAG ?= v0.2.0
LOCAL_IMAGE ?= switchyard:$(IMAGE_TAG)

.PHONY: validate helm-lint helm-template image deploy

validate: helm-lint helm-template

helm-lint:
	api_key_envs_json="$$(python3 scripts/list_api_key_envs.py --json $(ROUTES_FILE))"; \
	helm lint $(CHART_DIR) \
		--set-json "apiKeySecret.keys=$${api_key_envs_json}" \
		--set-file config.routesToml=$(ROUTES_FILE)

helm-template:
	api_key_envs_json="$$(python3 scripts/list_api_key_envs.py --json $(ROUTES_FILE))"; \
	helm template switchyard $(CHART_DIR) \
		--namespace daedalus \
		--set-json "apiKeySecret.keys=$${api_key_envs_json}" \
		--set-file config.routesToml=$(ROUTES_FILE) \
		> /tmp/switchyard-rendered.yaml
	kubectl apply --dry-run=client --validate=false -f /tmp/switchyard-rendered.yaml >/dev/null

image:
	docker build \
		--build-arg SWITCHYARD_COMMIT=$(SWITCHYARD_COMMIT) \
		--tag $(LOCAL_IMAGE) .

deploy:
	./scripts/deploy.sh
