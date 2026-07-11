#!/usr/bin/env bash
# Build API from a temporary context containing only committed source + the hash-locked wheelhouse.
set -Eeuo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(cd "$script_dir/../.." && pwd)"
tag=${1:-localhost/meld7t/api:0.2.0}
artifacts=${2:-$HOME/meld7t-release-input/api}
[[ -f $artifacts/requirements.lock && -d $artifacts/wheelhouse && -f $artifacts/SHA256SUMS ]] || {
  printf 'build API wheelhouse first: %s\n' "$artifacts" >&2; exit 1; }
(cd "$artifacts" && sha256sum --strict --check SHA256SUMS >/dev/null)
[[ -z $(git -C "$repo_dir" status --porcelain) ]] || {
  printf 'API release images must be built from a clean committed worktree\n' >&2; exit 1; }

context=$(mktemp -d /tmp/meld7t-api-context.XXXXXX)
trap 'rm -rf -- "$context"' EXIT
git -C "$repo_dir" archive --format=tar HEAD platform/api \
  | tar -xf - --strip-components=2 -C "$context"
cp "$artifacts/requirements.lock" "$context/requirements.lock"
cp -a "$artifacts/wheelhouse" "$context/wheelhouse"
podman build --pull=never \
  --build-arg BUILD_GIT_SHA="$(git -C "$repo_dir" rev-parse HEAD)" \
  --build-arg BUILD_RELEASE="${MELD7T_RELEASE_ID:-0.2.0}" \
  --tag "$tag" --file "$context/Containerfile" "$context"
podman image inspect "$tag" --format 'manifest={{.Digest}} image={{.Id}}'
