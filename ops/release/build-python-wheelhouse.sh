#!/usr/bin/env bash
# Materialize a reviewed uv.lock as hash-checked, wheel-only offline installation inputs.
set -Eeuo pipefail
umask 022

project=${1:?usage: build-python-wheelhouse.sh PROJECT_DIR OUTPUT_DIR}
output=${2:?usage: build-python-wheelhouse.sh PROJECT_DIR OUTPUT_DIR}
python_bin=${PYTHON_BIN:-python3.13}
for command in uv "$python_bin"; do
  command -v "$command" >/dev/null || { printf 'missing required command: %s\n' "$command" >&2; exit 1; }
done
[[ -f $project/pyproject.toml && -f $project/uv.lock ]] || {
  printf 'project must contain pyproject.toml and a reviewed uv.lock: %s\n' "$project" >&2; exit 1; }
[[ ! -e $output ]] || { printf 'refusing to replace existing output: %s\n' "$output" >&2; exit 1; }
mkdir -p "$output/wheelhouse"

uv export --project "$project" --locked --no-dev --no-emit-project \
  --format requirements.txt --output-file "$output/requirements.lock"
"$python_bin" -m pip download --require-hashes --only-binary=:all: \
  --dest "$output/wheelhouse" --requirement "$output/requirements.lock"
(cd "$output" && find requirements.lock wheelhouse -type f -print0 | sort -z | xargs -0 sha256sum >SHA256SUMS)
printf 'hash-locked wheelhouse written to %s\n' "$output"
