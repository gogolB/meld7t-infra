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

# Podman accepts overlapping bridge subnets, but the resulting routing is ambiguous. Reject both
# planned/planned and planned/existing overlaps at staging time. An existing network is accepted
# only when its name and complete subnet set exactly match the planned replacement.
planned_networks=$(mktemp)
existing_networks=$(mktemp)
trap 'rm -f -- "$planned_networks" "$existing_networks"' EXIT
while IFS= read -r network_file; do
  subnet=$(sed -n 's/^Subnet=//p' "$network_file")
  [[ -n $subnet && $subnet != *$'\n'* ]] || {
    printf '%s must contain exactly one Subnet= line\n' "$network_file" >&2
    exit 1
  }
  network_name=$(sed -n 's/^NetworkName=//p' "$network_file")
  [[ $network_name =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$ \
     && $network_name != *$'\n'* ]] || {
    printf '%s must contain exactly one safe NetworkName= line\n' "$network_file" >&2
    exit 1
  }
  printf '%s\t%s\t%s\n' "$network_name" "$subnet" "$network_file" >>"$planned_networks"
done < <(find "$source_dir" -maxdepth 1 -type f -name '*.network' -print | LC_ALL=C sort)

existing_names=$(podman network ls --format '{{.Name}}') || {
  printf 'unable to enumerate existing rootless Podman networks\n' >&2
  exit 1
}
while IFS= read -r network_name; do
  [[ -n $network_name ]] || continue
  # Preserve the name even for a plugin/network with no IPAM subnets so a same-name topology
  # collision cannot disappear from the inventory.
  printf '%s\t-\n' "$network_name" >>"$existing_networks"
  podman network inspect --format \
    '{{$name := .Name}}{{range .Subnets}}{{$name}}{{"\t"}}{{.Subnet}}{{"\n"}}{{end}}' \
    "$network_name" >>"$existing_networks" || {
      printf 'unable to inspect existing rootless Podman network %s\n' "$network_name" >&2
      exit 1
    }
done <<<"$existing_names"
python3 "$script_dir/validate-network-subnets.py" "$planned_networks" "$existing_networks"

mkdir -p "$dest_dir"

manifest="$dest_dir/.meld7t-managed-files"
new_manifest=$(mktemp)
trap 'rm -f -- "$planned_networks" "$existing_networks" "$new_manifest"' EXIT
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
    # Multiple isolated instances intentionally reuse one signed upstream image. Keep one lock role
    # per image rather than duplicating an identical digest under deployment-specific unit names.
    case "$role" in
      harmonization-orthanc) role=orthanc ;;
      harmonization-postgres) role=postgres ;;
    esac
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
