#!/usr/bin/env bash
# Load non-secret header settings with the same caller-over-.env precedence as keys.
set -euo pipefail

if [[ $# != 2 ]]; then
  echo "usage: bash scripts/render_routes.sh SOURCE OUTPUT" >&2
  exit 64
fi
render_script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly render_script_dir
render_header_names="$(python3 "${render_script_dir}/render_routes.py" "$1" --list-env)"
declare -A render_caller_values=()
while IFS= read -r render_header_name; do
  if [[ -n "${render_header_name}" && -v "${render_header_name}" ]]; then
    render_caller_values["${render_header_name}"]="${!render_header_name}"
  fi
done <<< "${render_header_names}"

if [[ -n "${render_header_names}" && -f "${render_script_dir}/../.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${render_script_dir}/../.env"
  set +a
fi
for render_header_name in "${!render_caller_values[@]}"; do
  printf -v "${render_header_name}" '%s' "${render_caller_values[${render_header_name}]}"
  export "${render_header_name}"
done

python3 "${render_script_dir}/render_routes.py" "$1" "$2"
