SHELL := /bin/bash
.DEFAULT_GOAL := validate

CHART_DIR := charts/switchyard
ROUTES_FILE ?= routes_fireworks.toml
SWITCHYARD_COMMIT := 1fc9ab887d1c663b0048ae24d5f473d15ed8daaa
IMAGE_REPOSITORY ?= daedalus.ddns.me:5050/btuttle/switchyard
IMAGE_TAG ?= v0.2.0
LOCAL_IMAGE ?= $(IMAGE_REPOSITORY):$(IMAGE_TAG)
EXPECTED_CONTEXT ?= kubernetes-admin@kubernetes
ROUTE_FILES := $(wildcard routes_*.toml)
ROUTE_VARIANTS := $(patsubst routes_%.toml,%,$(ROUTE_FILES))
DEPLOY_ROUTE_TARGETS := $(addprefix deploy-route_,$(ROUTE_VARIANTS))

.PHONY: validate helm-lint helm-template image publish deploy $(DEPLOY_ROUTE_TARGETS)

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

publish: image
	docker push $(LOCAL_IMAGE)

deploy:
	ROUTES_FILE=$(ROUTES_FILE) \
	IMAGE_REPOSITORY=$(IMAGE_REPOSITORY) \
	IMAGE_TAG=$(IMAGE_TAG) \
	EXPECTED_CONTEXT=$(EXPECTED_CONTEXT) \
	./scripts/deploy.sh

$(DEPLOY_ROUTE_TARGETS): deploy-route_%: routes_%.toml publish
	$(MAKE) deploy ROUTES_FILE=$<
