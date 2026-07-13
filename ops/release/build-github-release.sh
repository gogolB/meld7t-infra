#!/usr/bin/env bash
set -Eeuo pipefail

umask 022
export LC_ALL=C
export TZ=UTC
export PYTHONHASHSEED=0

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
VALIDATOR="$ROOT/ops/release/developer_release.py"

usage() {
  echo "usage: $0 VERSION OUTPUT_DIR" >&2
  echo "example: $0 0.2.0 /tmp/meld7t-developer-release-0.2.0" >&2
  exit 2
}

[[ $# -eq 2 ]] || usage
VERSION=$1
OUTPUT=$2

for command in git gzip node npm python3 sha256sum tar uv; do
  command -v "$command" >/dev/null || {
    echo "required command is unavailable: $command" >&2
    exit 1
  }
done

cd "$ROOT"
[[ -z $(git status --porcelain --untracked-files=normal) ]] || {
  echo "developer releases must be built from a clean committed worktree" >&2
  exit 1
}

python3 "$VALIDATOR" check --root "$ROOT" --version "$VERSION" --tracked

GIT_SHA=$(git rev-parse --verify HEAD^{commit})
RELEASE_TAG="v$VERSION"
git show-ref --verify --quiet "refs/tags/$RELEASE_TAG" || {
  echo "required local release tag is missing: $RELEASE_TAG" >&2
  exit 1
}
TAG_SHA=$(git rev-parse --verify "refs/tags/$RELEASE_TAG^{commit}")
[[ $TAG_SHA == "$GIT_SHA" ]] || {
  echo "release tag $RELEASE_TAG does not resolve to HEAD ($GIT_SHA)" >&2
  exit 1
}
SOURCE_DATE_EPOCH=$(git show -s --format=%ct "$GIT_SHA")
export SOURCE_DATE_EPOCH

OUTPUT_ABS=$(realpath -m "$OUTPUT")
[[ ! -e "$OUTPUT_ABS" ]] || {
  echo "output already exists: $OUTPUT_ABS" >&2
  exit 1
}
OUTPUT_PARENT=$(dirname "$OUTPUT_ABS")
mkdir -p "$OUTPUT_PARENT"
TEMP_ROOT=$(mktemp -d "$OUTPUT_PARENT/.meld7t-developer-release.XXXXXX")
STAGE="$TEMP_ROOT/release"
trap 'rm -rf -- "$TEMP_ROOT"' EXIT
mkdir -p "$STAGE"

uv build "$ROOT/platform/api" --out-dir "$STAGE" --no-create-gitignore
uv build "$ROOT/platform/worker" --out-dir "$STAGE" --no-create-gitignore

npm --prefix "$ROOT/platform/web" ci --ignore-scripts --no-audit --no-fund
npm --prefix "$ROOT/platform/web" test
MELD7T_GIT_SHA="$GIT_SHA" npm --prefix "$ROOT/platform/web" run build
printf '%s\n' "$GIT_SHA" >"$ROOT/platform/web/dist/.meld7t-git-sha"

git archive --format=tar --prefix="meld7t-$VERSION/" "$GIT_SHA" \
  | gzip -n >"$STAGE/meld7t-source-$VERSION.tar.gz"
tar --sort=name --format=gnu --mtime="@$SOURCE_DATE_EPOCH" \
  --owner=0 --group=0 --numeric-owner --mode='u+rwX,go+rX,go-w' \
  -C "$ROOT/platform/web" -cf - dist \
  | gzip -n >"$STAGE/meld7t-web-$VERSION.tar.gz"

install -m 0644 "$ROOT/ops/release/DEVELOPER_RELEASE_NOTICE.txt" \
  "$STAGE/DEVELOPER_RELEASE_NOTICE.txt"
install -m 0644 "$ROOT/ops/release/GITHUB_RELEASE_NOTES.md" \
  "$STAGE/RELEASE-NOTES.md"
python3 "$VALIDATOR" metadata \
  --root "$ROOT" \
  --version "$VERSION" \
  --git-sha "$GIT_SHA" \
  --source-date-epoch "$SOURCE_DATE_EPOCH" \
  --output "$STAGE/RELEASE-METADATA.json"

(
  cd "$STAGE"
  while IFS= read -r -d '' artifact; do
    sha256sum "$artifact"
  done < <(find . -maxdepth 1 -type f ! -name SHA256SUMS -print0 | sort -z)
) >"$STAGE/SHA256SUMS"
(
  cd "$STAGE"
  sha256sum --check SHA256SUMS
)

find "$STAGE" -type f -exec chmod 0644 {} +
mv "$STAGE" "$OUTPUT_ABS"
echo "developer release kit created: $OUTPUT_ABS"
