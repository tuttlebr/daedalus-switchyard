#!/usr/bin/env bash
# Load non-secret header settings with the same caller-over-.env precedence as keys.
# Stays within bash 3.2 (macOS /bin/bash): no associative arrays, `[[ -v ]]` or mapfile.
set -euo pipefail

if [[ $# != 2 ]]; then
  echo "usage: bash scripts/render_routes.sh SOURCE OUTPUT" >&2
  exit 64
fi
render_script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly render_script_dir
render_header_names="$(python3 "${render_script_dir}/render_routes.py" "$1" --list-env)"
# Names are validated identifiers; keep caller values in prefixed scalars.
while IFS= read -r render_header_name; do
  if [[ -n "${render_header_name}" ]]; then
    if [[ -n "${!render_header_name+x}" ]]; then
      printf -v "render_caller_value_${render_header_name}" '%s' "${!render_header_name}"
      printf -v "render_caller_set_${render_header_name}" '%s' 1
    fi
  fi
done <<< "${render_header_names}"

if [[ -n "${render_header_names}" && -f "${render_script_dir}/../.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${render_script_dir}/../.env"
  set +a
fi
while IFS= read -r render_header_name; do
  if [[ -n "${render_header_name}" ]]; then
    render_caller_set_name="render_caller_set_${render_header_name}"
    if [[ -n "${!render_caller_set_name+x}" ]]; then
      render_caller_value_name="render_caller_value_${render_header_name}"
      printf -v "${render_header_name}" '%s' "${!render_caller_value_name}"
      export "${render_header_name}"
    fi
  fi
done <<< "${render_header_names}"

python3 "${render_script_dir}/render_routes.py" "$1" "$2"
