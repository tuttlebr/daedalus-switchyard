#!/usr/bin/env bash
# Stays within bash 3.2 (macOS /bin/bash): no associative arrays, `[[ -v ]]` or mapfile.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
CHART_DIR="${PROJECT_DIR}/charts/switchyard"
build_publish=false
if [[ $# == 1 && "$1" == --build-publish ]]; then
  build_publish=true
elif [[ $# != 0 ]]; then
  echo 'usage: scripts/deploy.sh [--build-publish]' >&2
  exit 64
fi
readonly build_publish

# A saved provider is an explicit operator choice, never a filename-order default.
# Preserve an explicitly empty caller value so it fails rather than selecting .env.
if [[ -z "${ROUTES_FILE+x}" && -f "${PROJECT_DIR}/.env" ]]; then
  ROUTES_FILE="$(
    source "${PROJECT_DIR}/.env"
    printf '%s' "${ROUTES_FILE:-}"
  )"
fi
ROUTES_FILE="${ROUTES_FILE:-}"
if [[ -z "${ROUTES_FILE}" ]]; then
  echo 'Set ROUTES_FILE to a provider configuration. Available:' >&2
  printf '%s\n' "${PROJECT_DIR}"/routes_*.toml >&2
  exit 64
fi
if [[ "${ROUTES_FILE}" != /* ]]; then
  ROUTES_FILE="${PROJECT_DIR}/${ROUTES_FILE}"
fi

NAMESPACE="daedalus"
deploy_setting_names=(RELEASE_NAME SECRET_NAME EXPECTED_CONTEXT IMAGE_REPOSITORY
  IMAGE_TAG IMAGE_PLATFORM IMAGE_DIGEST HELM_TIMEOUT CAPABILITY_TIMEOUT CAPABILITY_MAX_OUTPUT_TOKENS
  DEPLOY_BACKUP_DIR)
# Setting names are fixed identifiers; keep caller values in prefixed scalars.
for deploy_setting_name in "${deploy_setting_names[@]}"; do
  if [[ -n "${!deploy_setting_name+x}" ]]; then
    printf -v "caller_deploy_value_${deploy_setting_name}" '%s' "${!deploy_setting_name}"
    printf -v "caller_deploy_set_${deploy_setting_name}" '%s' 1
  fi
done

for command_name in helm kubectl python3; do
  command -v "${command_name}" >/dev/null 2>&1 || {
    echo "required command not found: ${command_name}" >&2
    exit 127
  }
done

if [[ ! -f "${ROUTES_FILE}" ]]; then
  echo "routes file not found: ${ROUTES_FILE}" >&2
  exit 66
fi

# Validate, probe and deploy exactly one immutable local snapshot, even if the
# operator edits the source TOML while the capability requests are running.
gate_snapshot_dir="$(mktemp -d)"
readonly gate_snapshot_dir
trap 'rm -rf -- "${gate_snapshot_dir}"' EXIT
cp -- "${ROUTES_FILE}" "${gate_snapshot_dir}/routes.source.toml"
bash "${PROJECT_DIR}/scripts/render_routes.sh" \
  "${gate_snapshot_dir}/routes.source.toml" "${gate_snapshot_dir}/routes.toml"
ROUTES_FILE="${gate_snapshot_dir}/routes.toml"

# Key names are validated identifiers; blank lines are dropped while reading.
api_key_env_names=()
while IFS= read -r api_key_env_name; do
  if [[ -n "${api_key_env_name}" ]]; then
    api_key_env_names+=("${api_key_env_name}")
  fi
done < <(python3 "${PROJECT_DIR}/scripts/list_api_key_envs.py" "${ROUTES_FILE}")
api_key_envs_json="$(
  python3 "${PROJECT_DIR}/scripts/list_api_key_envs.py" \
    --json "${ROUTES_FILE}"
)"

for api_key_env_name in ${api_key_env_names[@]+"${api_key_env_names[@]}"}; do
  if [[ -n "${!api_key_env_name+x}" ]]; then
    printf -v "caller_api_key_value_${api_key_env_name}" '%s' "${!api_key_env_name}"
    printf -v "caller_api_key_set_${api_key_env_name}" '%s' 1
  fi
done

if [[ -f "${PROJECT_DIR}/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${PROJECT_DIR}/.env"
  set +a
fi
ROUTES_FILE="${gate_snapshot_dir}/routes.toml"

for deploy_setting_name in "${deploy_setting_names[@]}"; do
  caller_deploy_set_name="caller_deploy_set_${deploy_setting_name}"
  if [[ -n "${!caller_deploy_set_name+x}" ]]; then
    caller_deploy_value_name="caller_deploy_value_${deploy_setting_name}"
    printf -v "${deploy_setting_name}" '%s' "${!caller_deploy_value_name}"
    export "${deploy_setting_name}"
  fi
done
RELEASE_NAME="${RELEASE_NAME:-switchyard}"
SECRET_NAME="${SECRET_NAME:-switchyard-api-key}"
EXPECTED_CONTEXT="${EXPECTED_CONTEXT:-kubernetes-admin@kubernetes}"
IMAGE_REPOSITORY="${IMAGE_REPOSITORY:-daedalus.ddns.me:5050/btuttle/switchyard}"
IMAGE_TAG="${IMAGE_TAG:-v0.3.0}"
HELM_TIMEOUT="${HELM_TIMEOUT:-10m}"

for api_key_env_name in ${api_key_env_names[@]+"${api_key_env_names[@]}"}; do
  caller_api_key_set_name="caller_api_key_set_${api_key_env_name}"
  if [[ -n "${!caller_api_key_set_name+x}" ]]; then
    caller_api_key_value_name="caller_api_key_value_${api_key_env_name}"
    printf -v "${api_key_env_name}" '%s' "${!caller_api_key_value_name}"
    export "${api_key_env_name}"
  fi
  if [[ -z "${!api_key_env_name:-}" ]]; then
    echo "Set ${api_key_env_name} in .env or the environment; ${ROUTES_FILE} requires it" >&2
    exit 64
  fi
  if [[ "${!api_key_env_name}" == *$'\n'* ]]; then
    echo "${api_key_env_name} must not contain a newline" >&2
    exit 64
  fi
done

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
if [[ "${build_publish}" == true ]]; then
  if [[ -n "${IMAGE_DIGEST:-}" ]]; then
    echo 'IMAGE_DIGEST selects an existing image; use make deploy-existing or unset it to rebuild.' >&2
    exit 64
  fi
  for command_name in make docker; do
    command -v "${command_name}" >/dev/null 2>&1 || {
      echo "required command not found: ${command_name}" >&2
      exit 127
    }
  done
fi

actual_context="$(kubectl config current-context)"
if [[ "${actual_context}" != "${EXPECTED_CONTEXT}" ]]; then
  echo "refusing deployment: expected context ${EXPECTED_CONTEXT}, found ${actual_context}" >&2
  exit 1
fi

redeployed_at="$(date -u +'%Y-%m-%dT%H:%M:%S.%NZ')"
restart_annotation="podAnnotations.kubectl\\.kubernetes\\.io/restartedAt=${redeployed_at}"

helm_validation_args=(
  --set-string "fullnameOverride=${RELEASE_NAME}"
  --set-string "image.repository=${IMAGE_REPOSITORY}"
  --set-string "image.tag=${IMAGE_TAG}"
  --set-string "image.pullPolicy=Always"
  --set-string "apiKeySecret.name=${SECRET_NAME}"
  --set-json "apiKeySecret.keys=${api_key_envs_json}"
  --set-string "${restart_annotation}"
  --set-file "config.routesToml=${ROUTES_FILE}"
)
if [[ -n "${IMAGE_DIGEST:-}" ]]; then
  helm_validation_args+=(--set-string "image.digest=${IMAGE_DIGEST}")
fi

helm lint "${CHART_DIR}" "${helm_validation_args[@]}"
helm template "${RELEASE_NAME}" "${CHART_DIR}" \
  --namespace "${NAMESPACE}" "${helm_validation_args[@]}" >"${gate_snapshot_dir}/manifest.yaml"

# A listed model and a healthy pod do not prove generation or tool support.
# Warn about advertised context limits that do not meet route declarations.
# Run before any namespace/Secret/release mutation; a generation/tool failure leaves live routes intact.
python3 "${PROJECT_DIR}/scripts/check_capabilities.py" "${ROUTES_FILE}" \
  --timeout "${CAPABILITY_TIMEOUT:-60}" \
  --max-output-tokens "${CAPABILITY_MAX_OUTPUT_TOKENS:-4096}"

# Preflight and provider checks finish before registry writes. Build from this
# checkout and publish exactly the repository/tag that the upgrade will select.
# A failed build/push stops before backup/adoption, Secret changes or upgrade.
if [[ "${build_publish}" == true ]]; then
  make --no-print-directory -C "${PROJECT_DIR}" publish \
    "LOCAL_IMAGE=${IMAGE_REPOSITORY}:${IMAGE_TAG}"
  docker image inspect "${IMAGE_REPOSITORY}:${IMAGE_TAG}" \
    --format '{{json .RepoDigests}}' >"${gate_snapshot_dir}/image-digests.json"
  IMAGE_DIGEST="$(python3 - "${IMAGE_REPOSITORY}" "${gate_snapshot_dir}/image-digests.json" <<'PY'
import json, re, sys
from pathlib import Path

repository = sys.argv[1]
entries = json.loads(Path(sys.argv[2]).read_text())
if not isinstance(entries, list):
    raise SystemExit("Pushed image has no repository digests; refusing deployment")
digests = {
    entry.split("@", 1)[1] for entry in entries
    if isinstance(entry, str) and entry.startswith(repository + "@")
    and re.fullmatch(r"sha256:[a-f0-9]{64}", entry.split("@", 1)[1])
}
if len(digests) != 1:
    raise SystemExit("Expected one digest for the pushed repository; refusing deployment")
print(digests.pop())
PY
  )"
  helm_validation_args+=(--set-string "image.digest=${IMAGE_DIGEST}")
  helm template "${RELEASE_NAME}" "${CHART_DIR}" \
    --namespace "${NAMESPACE}" "${helm_validation_args[@]}" >"${gate_snapshot_dir}/manifest.yaml"
fi

python3 "${PROJECT_DIR}/scripts/deploy_state.py" \
  --manifest "${gate_snapshot_dir}/manifest.yaml" \
  --state "${gate_snapshot_dir}/state.json" \
  --backup-root "${DEPLOY_BACKUP_DIR:-${PROJECT_DIR}/.deploy-backups}" \
  --namespace "${NAMESPACE}" --release "${RELEASE_NAME}" \
  --secret "${SECRET_NAME}" --context "${EXPECTED_CONTEXT}" --timeout "${HELM_TIMEOUT}"
deployment_state=()
while IFS= read -r deployment_state_line; do
  deployment_state+=("${deployment_state_line}")
done < <(
  python3 - "${gate_snapshot_dir}/state.json" <<'PY'
import json, sys
state = json.load(open(sys.argv[1]))
print(state["backup"])
print("true" if state["nodeport_only"] else "false")
print("true" if state["can_restore"] else "false")
PY
)
deployment_backup="${deployment_state[0]}"

recover_deployment() {
  local status=$?
  trap - ERR
  echo "Deployment failed; recovery snapshot: ${deployment_backup}" >&2
  if [[ "${deployment_state[2]}" == true ]]; then
    bash "${deployment_backup}/restore.sh" || \
      echo "Automatic recovery failed; retry: bash '${deployment_backup}/restore.sh'" >&2
  fi
  exit "${status}"
}
trap recover_deployment ERR
set -E

# The namespace is shared infrastructure. Do not apply an empty namespace
# manifest over it just to ensure it exists.
if [[ -z "$(kubectl get namespace "${NAMESPACE}" --ignore-not-found -o name)" ]]; then
  kubectl create namespace "${NAMESPACE}"
fi
if ((${#api_key_env_names[@]} > 0)); then
  api_key_env_file="${gate_snapshot_dir}/api-keys.env"
  touch "${api_key_env_file}"
  chmod 600 "${api_key_env_file}"
  for api_key_env_name in "${api_key_env_names[@]}"; do
    printf '%s=%s\n' "${api_key_env_name}" "${!api_key_env_name}" \
      >>"${api_key_env_file}"
  done
  kubectl --namespace "${NAMESPACE}" create secret generic "${SECRET_NAME}" \
    --from-env-file="${api_key_env_file}" \
    --dry-run=client -o yaml | kubectl apply -f -
  rm -f -- "${api_key_env_file}"
fi

helm_args=(
  upgrade --install "${RELEASE_NAME}" "${CHART_DIR}"
  --namespace "${NAMESPACE}"
  --create-namespace
  --timeout "${HELM_TIMEOUT}"
  --history-max 10
  --set-string "fullnameOverride=${RELEASE_NAME}"
  --set-string "image.repository=${IMAGE_REPOSITORY}"
  --set-string "image.tag=${IMAGE_TAG}"
  --set-string "image.pullPolicy=Always"
  --set-string "apiKeySecret.name=${SECRET_NAME}"
  --set-json "apiKeySecret.keys=${api_key_envs_json}"
  --set-string "${restart_annotation}"
  --set-file "config.routesToml=${ROUTES_FILE}"
)

# Helm waits forever for an ingress IP on the intentional NodePort-only class.
# Explicit rollout, EndpointSlice and HTTP checks below gate success instead.
# On failure recover the live snapshot, not potentially stale Helm networking.
if [[ "${deployment_state[1]}" != true ]]; then
  helm_args+=(--wait)
fi

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
  false
fi

kubectl --request-timeout=15s get --raw \
  "/api/v1/namespaces/${NAMESPACE}/services/${RELEASE_NAME}:4000/proxy/health"
printf '\nSwitchyard is ready at http://%s.%s.svc.cluster.local:4000\n' \
  "${RELEASE_NAME}" "${NAMESPACE}"
