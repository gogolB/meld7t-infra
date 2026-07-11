#!/usr/bin/env bash
# Validate and query the production image lock.  This script intentionally has no network path.
set -Eeuo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(cd "$script_dir/../.." && pwd)"
lock_file="${MELD7T_IMAGE_LOCK:-$repo_dir/containers/images.lock}"

readonly -a runtime_roles=(
  meld_graph pkg spm hippunfold api postgres redis orthanc immudb ohif caddy registry
)
readonly -a build_roles=(python_base node_builder cuda_smoke meld_dev)

die() {
  printf 'image-lock: %s\n' "$*" >&2
  exit 1
}

usage() {
  cat >&2 <<'EOF'
usage: image-lock.sh [--lock PATH] COMMAND [ARG]

Commands:
  validate [runtime|all]  Validate syntax, placeholders, duplicate/missing roles
  get ROLE                Print the locked digest reference for ROLE
  list [runtime|all]      Print "role reference" rows
  env                     Emit worker runtime image environment assignments
  verify-local            Prove every runtime reference is present at its locked digest
EOF
  exit 64
}

if [[ ${1:-} == --lock ]]; then
  [[ $# -ge 3 ]] || usage
  lock_file=$2
  shift 2
fi
[[ -r $lock_file ]] || die "lock is not readable: $lock_file"

declare -A refs=()
declare -a order=()

load_lock() {
  local line_no=0 raw role ref extra
  while IFS= read -r raw || [[ -n $raw ]]; do
    ((line_no += 1))
    [[ $raw =~ ^[[:space:]]*$ || $raw =~ ^[[:space:]]*# ]] && continue
    read -r role ref extra <<<"$raw"
    [[ -n ${role:-} && -n ${ref:-} && -z ${extra:-} ]] \
      || die "$lock_file:$line_no must contain exactly two fields"
    [[ $role =~ ^[a-z][a-z0-9_]*$ ]] || die "$lock_file:$line_no invalid role: $role"
    [[ -z ${refs[$role]+x} ]] || die "$lock_file:$line_no duplicate role: $role"
    refs[$role]=$ref
    order+=("$role")
  done <"$lock_file"
}

is_digest_ref() {
  [[ $1 =~ ^[^[:space:]@]+/[^[:space:]@]+@sha256:[0-9a-f]{64}$ ]]
}

require_role() {
  local role=$1
  [[ -n ${refs[$role]+x} ]] || die "required role is absent: $role"
  [[ ${refs[$role]} != REQUIRED_* ]] || die "$role is not pinned: ${refs[$role]}"
  is_digest_ref "${refs[$role]}" || die "$role is not a fully-qualified sha256 reference: ${refs[$role]}"
}

validate() {
  local scope=${1:-runtime} role
  [[ $scope == runtime || $scope == all ]] || usage
  for role in "${runtime_roles[@]}"; do require_role "$role"; done
  if [[ $scope == all ]]; then
    for role in "${build_roles[@]}"; do require_role "$role"; done
  fi
  # Reject malformed non-required rows as well.  A lock must never carry an ambiguous third class.
  for role in "${order[@]}"; do
    [[ ${refs[$role]} != REQUIRED_* ]] || die "$role is not pinned: ${refs[$role]}"
    is_digest_ref "${refs[$role]}" || die "$role has an invalid reference: ${refs[$role]}"
  done
  printf 'validated %s (%s)\n' "$lock_file" "$scope"
}

list_rows() {
  local scope=${1:-runtime} role
  [[ $scope == runtime || $scope == all ]] || usage
  if [[ $scope == runtime ]]; then
    for role in "${runtime_roles[@]}"; do
      require_role "$role"
      printf '%s %s\n' "$role" "${refs[$role]}"
    done
  else
    validate all >/dev/null
    for role in "${order[@]}"; do printf '%s %s\n' "$role" "${refs[$role]}"; done
  fi
}

worker_env() {
  local role
  for role in pkg meld_graph hippunfold spm; do require_role "$role"; done
  printf 'MELD7T_PKG_IMAGE=%s\n' "${refs[pkg]}"
  printf 'MELD7T_MELD_IMAGE=%s\n' "${refs[meld_graph]}"
  printf 'MELD7T_HIPPUNFOLD_IMAGE=%s\n' "${refs[hippunfold]}"
  printf 'MELD7T_MAP_IMAGE=%s\n' "${refs[spm]}"
}

verify_local() {
  command -v podman >/dev/null || die "podman is required"
  local role ref expected actual
  validate runtime >/dev/null
  for role in "${runtime_roles[@]}"; do
    ref=${refs[$role]}
    expected=${ref##*@}
    actual=$(podman image inspect "$ref" --format '{{.Digest}}' 2>/dev/null) \
      || die "$role is not present locally at $ref"
    [[ $actual == "$expected" ]] \
      || die "$role digest mismatch: expected $expected, found ${actual:-none}"
    printf 'ok %-12s %s\n' "$role" "$expected"
  done
}

load_lock
command=${1:-}
shift || true
case "$command" in
  validate) validate "${1:-runtime}" ;;
  get)
    [[ $# -eq 1 ]] || usage
    require_role "$1"
    printf '%s\n' "${refs[$1]}"
    ;;
  list) list_rows "${1:-runtime}" ;;
  env) [[ $# -eq 0 ]] || usage; worker_env ;;
  verify-local) [[ $# -eq 0 ]] || usage; verify_local ;;
  *) usage ;;
esac
