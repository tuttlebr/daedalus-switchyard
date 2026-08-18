SHELL := /bin/bash
.DEFAULT_GOAL := validate

CHART_DIR := charts/switchyard
ROUTES_FILE := routes.toml
SWITCHYARD_COMMIT := 1fc9ab887d1c663b0048ae24d5f473d15ed8daaa
IMAGE_TAG ?= v0.2.0
LOCAL_IMAGE ?= switchyard:$(IMAGE_TAG)

.PHONY: validate validate-routes helm-lint helm-template image validate-image deploy

validate: validate-routes helm-lint helm-template

validate-routes:
	python3 scripts/validate_routes.py $(ROUTES_FILE)

helm-lint:
	helm lint $(CHART_DIR) --set-file config.routesToml=$(ROUTES_FILE)

helm-template:
	helm template switchyard $(CHART_DIR) \
		--namespace daedalus \
		--set-file config.routesToml=$(ROUTES_FILE) \
		> /tmp/switchyard-rendered.yaml
	kubectl apply --dry-run=client --validate=false -f /tmp/switchyard-rendered.yaml >/dev/null

image:
	docker build \
		--build-arg SWITCHYARD_COMMIT=$(SWITCHYARD_COMMIT) \
		--tag $(LOCAL_IMAGE) .

validate-image:
	docker run --rm \
		--env OPENAI_API_KEY=configuration-validation-only \
		--volume "$(CURDIR)/$(ROUTES_FILE):/etc/switchyard/routes.toml:ro" \
		$(LOCAL_IMAGE) --config /etc/switchyard/routes.toml --dry-run

deploy:
	./scripts/deploy.sh
