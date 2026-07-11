#!/usr/bin/env bash
set -Eeuo pipefail
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(cd "$script_dir/../.." && pwd)"
exec "$script_dir/build-python-wheelhouse.sh" \
  "$repo_dir/platform/worker" "${1:-$HOME/meld7t-release-input/worker}"
