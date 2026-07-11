#!/usr/bin/env bash
# Install tracked Quadlets and replace every Image= line with the signed lock reference.
set -Eeuo pipefail
umask 022

source_dir=${1:?usage: install-quadlets.sh SOURCE_DIR DEST_DIR IMAGE_LOCK}
dest_dir=${2:?usage: install-quadlets.sh SOURCE_DIR DEST_DIR IMAGE_LOCK}
lock_file=${3:?usage: install-quadlets.sh SOURCE_DIR DEST_DIR IMAGE_LOCK}
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
lockctl="$script_dir/../release/image-lock.sh"

[[ -d $source_dir && -f $lock_file ]] || { printf 'invalid source or image lock\n' >&2; exit 1; }
"$lockctl" --lock "$lock_file" validate runtime >/dev/null
mkdir -p "$dest_dir"

manifest="$dest_dir/.meld7t-managed-files"
new_manifest=$(mktemp)
trap 'rm -f -- "$new_manifest"' EXIT
find "$source_dir" -maxdepth 1 -type f \
  \( -name '*.container' -o -name '*.network' -o -name '*.volume' \) \
  -printf '%f\n' | LC_ALL=C sort >"$new_manifest"

# Remove only files recorded by the previous run; unrelated operator units are never touched.
if [[ -f $manifest ]]; then
  while IFS= read -r old; do
    [[ -n $old ]] || continue
    grep -Fxq "$old" "$new_manifest" || rm -f -- "$dest_dir/$old"
  done <"$manifest"
fi

while IFS= read -r name; do
  src="$source_dir/$name"
  dst="$dest_dir/$name"
  if [[ $name == *.container ]]; then
    role=${name%.container}
    image=$("$lockctl" --lock "$lock_file" get "$role")
    awk -v image="$image" '
      BEGIN { replaced=0 }
      /^Image=/ { print "Image=" image; replaced++; next }
      { print }
      END { if (replaced != 1) exit 42 }
    ' "$src" >"$dst.tmp" || {
      rm -f -- "$dst.tmp"
      printf '%s must contain exactly one Image= line\n' "$src" >&2
      exit 1
    }
    mv -f -- "$dst.tmp" "$dst"
  else
    install -m 0644 "$src" "$dst"
  fi
  chmod 0644 "$dst"
done <"$new_manifest"
install -m 0644 "$new_manifest" "$manifest"

while IFS= read -r name; do
  [[ $name == *.container ]] || continue
  grep -Eq '^Image=[^[:space:]@]+/[^[:space:]@]+@sha256:[0-9a-f]{64}$' "$dest_dir/$name" || {
    printf 'rendered Quadlet still has a non-digest image: %s\n' "$name" >&2
    exit 1
  }
done <"$new_manifest"
printf 'installed digest-rendered Quadlets in %s\n' "$dest_dir"
