#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
CHART_DIR="${PROJECT_DIR}/charts/switchyard"
ROUTES_FILE="${PROJECT_DIR}/routes.toml"

NAMESPACE="daedalus"
RELEASE_NAME="${RELEASE_NAME:-switchyard}"
SECRET_NAME="${SECRET_NAME:-switchyard-api-key}"
EXPECTED_CONTEXT="${EXPECTED_CONTEXT:-kubernetes-admin@kubernetes}"
IMAGE_TAG="${IMAGE_TAG:-v0.2.0}"
HELM_TIMEOUT="${HELM_TIMEOUT:-10m}"

if [[ -f "${PROJECT_DIR}/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${PROJECT_DIR}/.env"
  set +a
fi

: "${OPENAI_API_KEY:?Set OPENAI_API_KEY in .env or the environment}"
: "${IMAGE_REPOSITORY:?Set IMAGE_REPOSITORY to the registry image Kubernetes can pull}"

if [[ ! "${RELEASE_NAME}" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ || ${#RELEASE_NAME} -gt 53 ]]; then
  echo "RELEASE_NAME must be a lowercase Helm release name of at most 53 characters" >&2
  exit 64
fi
if [[ ! "${SECRET_NAME}" =~ ^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$ || ${#SECRET_NAME} -gt 253 ]]; then
  echo "SECRET_NAME must be a valid lowercase Kubernetes resource name" >&2
  exit 64
fi
if [[ "${IMAGE_REPOSITORY}" == *","* ]]; then
  echo "IMAGE_REPOSITORY must not contain a comma" >&2
  exit 64
fi
if [[ -n "${IMAGE_DIGEST:-}" && ! "${IMAGE_DIGEST}" =~ ^sha256:[a-f0-9]{64}$ ]]; then
  echo "IMAGE_DIGEST must be a sha256:<64 lowercase hex characters> digest" >&2
  exit 64
fi

for command_name in helm kubectl python3; do
  command -v "${command_name}" >/dev/null 2>&1 || {
    echo "required command not found: ${command_name}" >&2
    exit 127
  }
done

actual_context="$(kubectl config current-context)"
if [[ "${actual_context}" != "${EXPECTED_CONTEXT}" ]]; then
  echo "refusing deployment: expected context ${EXPECTED_CONTEXT}, found ${actual_context}" >&2
  exit 1
fi

python3 "${PROJECT_DIR}/scripts/validate_routes.py" "${ROUTES_FILE}"

helm_validation_args=(
  --set-string "fullnameOverride=${RELEASE_NAME}"
  --set-string "image.repository=${IMAGE_REPOSITORY}"
  --set-string "image.tag=${IMAGE_TAG}"
  --set-string "apiKeySecret.name=${SECRET_NAME}"
  --set-file "config.routesToml=${ROUTES_FILE}"
)
if [[ -n "${IMAGE_DIGEST:-}" ]]; then
  helm_validation_args+=(--set-string "image.digest=${IMAGE_DIGEST}")
fi

helm lint "${CHART_DIR}" "${helm_validation_args[@]}"
helm template "${RELEASE_NAME}" "${CHART_DIR}" \
  --namespace "${NAMESPACE}" "${helm_validation_args[@]}" >/dev/null

kubectl create namespace "${NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -
kubectl --namespace "${NAMESPACE}" create secret generic "${SECRET_NAME}" \
  --from-literal="OPENAI_API_KEY=${OPENAI_API_KEY}" \
  --dry-run=client -o yaml | kubectl apply -f -

helm_args=(
  upgrade --install "${RELEASE_NAME}" "${CHART_DIR}"
  --namespace "${NAMESPACE}"
  --create-namespace
  --atomic
  --cleanup-on-fail
  --wait
  --timeout "${HELM_TIMEOUT}"
  --history-max 10
  --set-string "fullnameOverride=${RELEASE_NAME}"
  --set-string "image.repository=${IMAGE_REPOSITORY}"
  --set-string "image.tag=${IMAGE_TAG}"
  --set-string "apiKeySecret.name=${SECRET_NAME}"
  --set-file "config.routesToml=${ROUTES_FILE}"
)

if [[ -n "${IMAGE_DIGEST:-}" ]]; then
  helm_args+=(--set-string "image.digest=${IMAGE_DIGEST}")
fi

helm "${helm_args[@]}"

kubectl --namespace "${NAMESPACE}" rollout status \
  "deployment/${RELEASE_NAME}" --timeout="${HELM_TIMEOUT}"

ready_addresses=""
for ((attempt = 1; attempt <= 30; attempt++)); do
  ready_addresses="$(
    kubectl --namespace "${NAMESPACE}" get endpointslice \
      --selector="kubernetes.io/service-name=${RELEASE_NAME}" \
      --output=jsonpath='{range .items[*].endpoints[?(@.conditions.ready==true)]}{.addresses[0]}{"\n"}{end}' \
      2>/dev/null || true
  )"
  if [[ -n "${ready_addresses}" ]]; then
    break
  fi
  sleep 1
done
if [[ -z "${ready_addresses}" ]]; then
  echo "deployment has no ready Service endpoints" >&2
  exit 1
fi

kubectl --request-timeout=15s get --raw \
  "/api/v1/namespaces/${NAMESPACE}/services/${RELEASE_NAME}:4000/proxy/health"
printf '\nSwitchyard is ready at http://%s.%s.svc.cluster.local:4000\n' \
  "${RELEASE_NAME}" "${NAMESPACE}"
