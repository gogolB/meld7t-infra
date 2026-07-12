#!/usr/bin/env bash
# Verify, import immutable images, and stage (but do not activate) a release on the server.
set -Eeuo pipefail
umask 077

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
bundle= trusted_key= release_root=${MELD7T_RELEASE_ROOT:-$HOME/.local/lib/meld7t}
cache_volume=hippunfold-cache
usage() {
  cat >&2 <<'EOF'
usage: import-airgap.sh --bundle DIR --trusted-key PUBLIC_PEM
       [--release-root DIR] [--cache-volume NAME]

The release is staged under releases/<id> and the `staged` symlink is updated.  This command never
changes `current`, runs migrations, or restarts production services.
EOF
  exit 64
}
die() { printf 'import-airgap: %s\n' "$*" >&2; exit 1; }
while (($#)); do
  case "$1" in
    --bundle) bundle=${2:-}; shift 2 ;;
    --trusted-key) trusted_key=${2:-}; shift 2 ;;
    --release-root) release_root=${2:-}; shift 2 ;;
    --cache-volume) cache_volume=${2:-}; shift 2 ;;
    *) usage ;;
  esac
done
[[ -n $bundle && -n $trusted_key ]] || usage
"$script_dir/verify-airgap.sh" "$bundle" "$trusted_key"
# shellcheck disable=SC1090
source "$bundle/release.env"
target="$release_root/releases/$MELD7T_RELEASE_ID"
[[ ! -e $target ]] || die "release is already staged: $target"
target_partial="$release_root/releases/.${MELD7T_RELEASE_ID}.partial.$$"
cache_tmp=
cache_final_incomplete=false
cleanup() {
  rm -rf -- "$target_partial"
  if [[ -n $cache_tmp ]] && podman volume exists "$cache_tmp"; then
    podman volume rm "$cache_tmp" >/dev/null 2>&1 || true
  fi
  if $cache_final_incomplete && podman volume exists "$cache_volume"; then
    podman volume rm "$cache_volume" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

while read -r role _ref; do
  printf 'loading %-12s\n' "$role"
  podman load --input "$bundle/images/$role.oci.tar" >/dev/null
done < <("$script_dir/image-lock.sh" --lock "$bundle/images.lock" list runtime)
"$script_dir/image-lock.sh" --lock "$bundle/images.lock" verify-local

mkdir -p "$release_root/releases" "$target_partial/platform/web" "$target_partial/artifacts/worker" \
  "$target_partial/artifacts/api" "$target_partial/release-receipt" "$target_partial/harmonization" \
  "$target_partial/attestations"
tar -xzf "$bundle/source.tar.gz" --strip-components=1 -C "$target_partial"
tar -xzf "$bundle/assets/web-dist.tar.gz" -C "$target_partial/platform/web"
tar -xzf "$bundle/assets/worker-artifacts.tar.gz" -C "$target_partial/artifacts/worker"
tar -xzf "$bundle/assets/api-build-artifacts.tar.gz" -C "$target_partial/artifacts/api"
tar -xzf "$bundle/assets/harmonization.tar.gz" -C "$target_partial/harmonization"
tar -xzf "$bundle/attestations.tar.gz" -C "$target_partial/attestations"
cp -- "$bundle/release.env" "$bundle/images.lock" "$bundle/SHA256SUMS" \
  "$bundle/SHA256SUMS.sig" "$bundle/source.tar.gz" "$bundle/attestations.tar.gz" \
  "$target_partial/release-receipt/"
"$script_dir/image-lock.sh" --lock "$bundle/images.lock" env >"$target_partial/release-receipt/runtime-images.env"
printf 'MELD7T_RELEASE_MANIFEST_DIGEST=%s\n' \
  "$(sha256sum "$bundle/SHA256SUMS" | awk '{print $1}')" \
  >>"$target_partial/release-receipt/runtime-images.env"
printf 'MELD7T_GIT_SHA=%s\n' "$MELD7T_GIT_SHA" \
  >>"$target_partial/release-receipt/runtime-images.env"
printf 'MELD7T_MAP_SCRIPT_SHA256=%s\n' "$MELD7T_MAP_SCRIPT_SHA256" \
  >>"$target_partial/release-receipt/runtime-images.env"
printf 'MELD7T_HIPPUNFOLD_CACHE_SHA256=%s\n' "$MELD7T_HIPPUNFOLD_CACHE_SHA256" \
  >>"$target_partial/release-receipt/runtime-images.env"
printf 'MELD7T_HARMONIZATION_INVENTORY_SHA256=%s\n' \
  "$MELD7T_HARMONIZATION_INVENTORY_SHA256" \
  >>"$target_partial/release-receipt/runtime-images.env"
printf 'MELD7T_HARMONIZATION_COHORT_BOOTSTRAP_ALLOWED=%s\n' \
  "$MELD7T_HARMONIZATION_COHORT_BOOTSTRAP_ALLOWED" \
  >>"$target_partial/release-receipt/runtime-images.env"

cache_digest=$(sha256sum "$bundle/assets/hippunfold-cache-files.sha256" | awk '{print $1}')
[[ $cache_digest == "$MELD7T_HIPPUNFOLD_CACHE_SHA256" ]] \
  || die "HippUnfold file manifest differs from the signed runtime identity"
helper_image=$("$script_dir/image-lock.sh" --lock "$bundle/images.lock" get postgres)
if podman volume exists "$cache_volume"; then
  installed_digest=$(podman run --rm --pull=never --network=none \
    --entrypoint /bin/sh --volume "$cache_volume:/cache:ro" \
    --volume "$bundle/assets/hippunfold-cache-files.sha256:/run/cache-files.sha256:ro,z" \
    "$helper_image" -c 'cmp -s /cache/.meld7t-cache-files.sha256 /run/cache-files.sha256 \
      && cat /cache/.meld7t-signed-archive-sha256 2>/dev/null' || true)
  [[ $installed_digest == "$cache_digest" ]] \
    || die "existing HippUnfold cache is not the signed release cache: $cache_volume"
  podman volume export "$cache_volume" \
    | python3 "$script_dir/cache-manifest.py" verify - \
      "$bundle/assets/hippunfold-cache-files.sha256" \
    || die "existing HippUnfold cache file closure is modified"
else
  cache_tmp="${cache_volume}-import-$$"
  podman volume create "$cache_tmp" >/dev/null
  podman volume import "$cache_tmp" "$bundle/assets/hippunfold-cache.tar"
  podman volume export "$cache_tmp" \
    | python3 "$script_dir/cache-manifest.py" verify - \
      "$bundle/assets/hippunfold-cache-files.sha256" \
    || die "temporary HippUnfold import failed file-closure verification"
  podman volume create --label "io.meld7t.cache.sha256=$cache_digest" "$cache_volume" >/dev/null
  cache_final_incomplete=true
  if ! podman volume export "$cache_tmp" | podman volume import "$cache_volume" -; then
    podman volume rm "$cache_volume" >/dev/null 2>&1 || true
    die "HippUnfold cache import failed"
  fi
  podman run --rm --pull=never --network=none --env "CACHE_DIGEST=$cache_digest" \
    --entrypoint /bin/sh --volume "$cache_volume:/cache" \
    --volume "$bundle/assets/hippunfold-cache-files.sha256:/run/cache-files.sha256:ro,z" \
    "$helper_image" -c 'set -e; umask 077; cp /run/cache-files.sha256 \
      /cache/.meld7t-cache-files.sha256; chmod 0444 /cache/.meld7t-cache-files.sha256; \
      printf "%s\n" "$CACHE_DIGEST" > /cache/.meld7t-signed-archive-sha256; \
      chmod 0444 /cache/.meld7t-signed-archive-sha256'
  podman volume export "$cache_volume" \
    | python3 "$script_dir/cache-manifest.py" verify - \
      "$bundle/assets/hippunfold-cache-files.sha256" \
    || die "installed HippUnfold cache failed final file-closure verification"
  podman volume rm "$cache_tmp" >/dev/null
  cache_tmp=
  cache_final_incomplete=false
fi

chmod -R a-w "$target_partial"
mv -- "$target_partial" "$target"
trap - EXIT
ln -sfn "$target" "$release_root/staged.new"
mv -Tf "$release_root/staged.new" "$release_root/staged"
printf 'staged release: %s\n' "$target"
printf 'not activated; run production preflight, backup, migration, and the controlled activation step\n'
