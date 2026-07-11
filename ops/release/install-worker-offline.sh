#!/usr/bin/env bash
set -Eeuo pipefail
umask 022

artifacts=${1:?usage: install-worker-offline.sh ARTIFACT_DIR [VENV_DIR]}
venv=${2:-$HOME/.local/lib/meld7t/worker-venv}
python_bin=${PYTHON_BIN:-python3.13}

[[ -f $artifacts/requirements.lock && -d $artifacts/wheelhouse && -f $artifacts/SHA256SUMS ]] \
  || { printf 'incomplete worker artifacts: %s\n' "$artifacts" >&2; exit 1; }
(cd "$artifacts" && sha256sum --strict --check SHA256SUMS)
command -v "$python_bin" >/dev/null || { printf 'missing %s\n' "$python_bin" >&2; exit 1; }
[[ ! -e $venv ]] || { printf 'refusing to replace existing venv: %s\n' "$venv" >&2; exit 1; }

mkdir -p "$(dirname "$venv")"
partial=$(mktemp -d "$(dirname "$venv")/.worker-venv.partial.XXXXXX")
trap 'rm -rf -- "$partial"' EXIT
rmdir "$partial"
"$python_bin" -m venv "$partial"
"$partial/bin/python" -m pip install \
  --no-index \
  --require-hashes \
  --find-links "$artifacts/wheelhouse" \
  --requirement "$artifacts/requirements.lock"
"$partial/bin/python" -m pip check
sha256sum "$artifacts/requirements.lock" | awk '{print $1}' >"$partial/.meld7t-requirements.sha256"
mv -- "$partial" "$venv"
trap - EXIT
printf 'offline worker environment installed at %s\n' "$venv"
