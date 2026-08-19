SHELL := /bin/bash
.DEFAULT_GOAL := validate

CHART_DIR := charts/switchyard
ROUTES_FILE := routes.toml
SWITCHYARD_COMMIT := 1fc9ab887d1c663b0048ae24d5f473d15ed8daaa
IMAGE_TAG ?= v0.2.0
LOCAL_IMAGE ?= switchyard:$(IMAGE_TAG)

.PHONY: validate test-routes validate-routes helm-lint helm-template image validate-image deploy

validate: test-routes validate-routes helm-lint helm-template

test-routes:
	python3 -m unittest discover -s tests -v

validate-routes:
	python3 scripts/validate_routes.py $(ROUTES_FILE)

helm-lint:
	api_key_envs_json="$$(python3 scripts/validate_routes.py --json-api-key-envs $(ROUTES_FILE))"; \
	helm lint $(CHART_DIR) \
		--set-json "apiKeySecret.keys=$${api_key_envs_json}" \
		--set-file config.routesToml=$(ROUTES_FILE)

helm-template:
	api_key_envs_json="$$(python3 scripts/validate_routes.py --json-api-key-envs $(ROUTES_FILE))"; \
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

validate-image:
	api_key_env_args=(); \
	while IFS= read -r api_key_env_name; do \
		api_key_env_args+=(--env "$${api_key_env_name}=configuration-validation-only"); \
	done < <(python3 scripts/validate_routes.py --list-api-key-envs $(ROUTES_FILE)); \
	docker run --rm "$${api_key_env_args[@]}" \
		--volume "$(CURDIR)/$(ROUTES_FILE):/etc/switchyard/routes.toml:ro" \
		$(LOCAL_IMAGE) --config /etc/switchyard/routes.toml --dry-run

deploy:
	./scripts/deploy.sh
