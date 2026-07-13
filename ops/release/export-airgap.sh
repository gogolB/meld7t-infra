#!/usr/bin/env bash
# Create a checksumed and institutionally signed, self-contained production release bundle.
set -Eeuo pipefail
umask 077

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_dir="$(cd "$script_dir/../.." && pwd)"
lock_file="$repo_dir/containers/images.lock"
output= signing_key= attestations= web_dist= worker_artifacts= api_artifacts=
host_artifacts= release_id= include_build=false hippunfold_volume=hippunfold-cache
harmonization= allow_empty_harmonization_bootstrap=false

die() { printf 'export-airgap: %s\n' "$*" >&2; exit 1; }
usage() {
  cat >&2 <<'EOF'
usage: export-airgap.sh --output DIR --release-id ID --signing-key PRIVATE_PEM
       --attestations DIR --web-dist DIR --api-artifacts DIR --worker-artifacts DIR
       [--host-artifacts DIR] [--include-build-images]
       [--harmonization DIR] [--allow-empty-harmonization-bootstrap]
       [--hippunfold-volume NAME] [--lock FILE]

The git worktree must be committed and clean.  PRIVATE_PEM is never copied into the bundle.
The corresponding public key must be provisioned independently on the air-gapped server.
EOF
  exit 64
}
while (($#)); do
  case "$1" in
    --output) output=${2:-}; shift 2 ;;
    --release-id) release_id=${2:-}; shift 2 ;;
    --signing-key) signing_key=${2:-}; shift 2 ;;
    --attestations) attestations=${2:-}; shift 2 ;;
    --web-dist) web_dist=${2:-}; shift 2 ;;
    --worker-artifacts) worker_artifacts=${2:-}; shift 2 ;;
    --api-artifacts) api_artifacts=${2:-}; shift 2 ;;
    --host-artifacts) host_artifacts=${2:-}; shift 2 ;;
    --harmonization) harmonization=${2:-}; shift 2 ;;
    --allow-empty-harmonization-bootstrap) allow_empty_harmonization_bootstrap=true; shift ;;
    --hippunfold-volume) hippunfold_volume=${2:-}; shift 2 ;;
    --include-build-images) include_build=true; shift ;;
    --lock) lock_file=${2:-}; shift 2 ;;
    *) usage ;;
  esac
done
[[ -n $output && -n $release_id && -n $signing_key && -n $attestations \
   && -n $web_dist && -n $worker_artifacts && -n $api_artifacts ]] || usage
[[ $release_id =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] || die "invalid release ID"
[[ -f $signing_key ]] || die "signing key is not a file: $signing_key"
[[ -d $attestations ]] || die "attestations directory is missing: $attestations"
for evidence in approval.txt sbom.spdx.json vulnerability-report.json \
  vulnerability-exceptions.txt license-report.json golden-case-evidence.txt; do
  [[ -s $attestations/$evidence ]] || die "required non-empty attestation is absent: $evidence"
done
for report in sbom.spdx.json vulnerability-report.json license-report.json; do
  python3 -m json.tool "$attestations/$report" >/dev/null \
    || die "attestation is not valid JSON: $report"
done
grep -Eq '^APPROVED_BY=.+$' "$attestations/vulnerability-exceptions.txt" \
  || die "vulnerability exception approval is absent"
grep -Eq '^RATIONALE=.+$' "$attestations/vulnerability-exceptions.txt" \
  || die "vulnerability exception rationale is absent"
exception_expiry=$(sed -n 's/^EXPIRES=//p' "$attestations/vulnerability-exceptions.txt")
[[ $exception_expiry =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ \
   && $(date -u -d "$exception_expiry" +%s) -ge $(date -u +%s) ]] \
  || die "vulnerability exception is missing a future EXPIRES=YYYY-MM-DD"
[[ -d $web_dist && -f $web_dist/index.html ]] || die "built web dist is incomplete: $web_dist"
[[ -f $worker_artifacts/requirements.lock && -d $worker_artifacts/wheelhouse \
   && -f $worker_artifacts/SHA256SUMS ]] || die "worker artifacts are incomplete"
[[ -f $api_artifacts/requirements.lock && -d $api_artifacts/wheelhouse \
   && -f $api_artifacts/SHA256SUMS ]] || die "API artifacts are incomplete"
[[ -z $host_artifacts || -d $host_artifacts ]] || die "host artifacts are not a directory"
[[ -n $harmonization ]] || die "--harmonization DIR is required for every production release"
[[ -d $harmonization ]] || die "harmonization root is not a directory"
[[ ! -e $output ]] || die "refusing to replace existing output: $output"
command -v openssl >/dev/null || die "openssl is required"
command -v podman >/dev/null || die "podman is required"
[[ -z $(git -C "$repo_dir" status --porcelain) ]] \
  || die "git worktree must be clean; build releases only from a reviewed commit"
git_sha=$(git -C "$repo_dir" rev-parse --verify HEAD)
web_revision=
[[ -f $web_dist/.meld7t-git-sha ]] && IFS= read -r web_revision <"$web_dist/.meld7t-git-sha"
[[ $web_revision == "$git_sha" ]] \
  || die "web dist is not marked as built from the release commit"
(cd "$worker_artifacts" && sha256sum --strict --check SHA256SUMS >/dev/null) \
  || die "worker artifact checksums are invalid"
(cd "$api_artifacts" && sha256sum --strict --check SHA256SUMS >/dev/null) \
  || die "API artifact checksums are invalid"

scope=runtime
$include_build && scope=all
"$script_dir/image-lock.sh" --lock "$lock_file" validate "$scope" >/dev/null
"$script_dir/image-lock.sh" --lock "$lock_file" verify-local >/dev/null
for bespoke_role in api pkg; do
  bespoke_ref=$("$script_dir/image-lock.sh" --lock "$lock_file" get "$bespoke_role")
  built_revision=$(podman image inspect "$bespoke_ref" \
    --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}')
  [[ $built_revision == "$git_sha" ]] \
    || die "$bespoke_role image was not built from signed source commit $git_sha"
done

partial="${output}.partial.$$"
trap 'rm -rf -- "$partial"' EXIT
mkdir -p "$partial/images" "$partial/assets"
cp -- "$lock_file" "$partial/images.lock"

source_epoch=$(git -C "$repo_dir" show -s --format=%ct HEAD)
git -C "$repo_dir" archive --format=tar --prefix=source/ HEAD | gzip -n >"$partial/source.tar.gz"

tar --sort=name --mtime="@$source_epoch" --owner=0 --group=0 --numeric-owner \
  -C "$(dirname "$web_dist")" -cf - "$(basename "$web_dist")" \
  | gzip -n >"$partial/assets/web-dist.tar.gz"
tar --sort=name --mtime="@$source_epoch" --owner=0 --group=0 --numeric-owner \
  -C "$worker_artifacts" -cf - requirements.lock wheelhouse SHA256SUMS \
  | gzip -n >"$partial/assets/worker-artifacts.tar.gz"
tar --sort=name --mtime="@$source_epoch" --owner=0 --group=0 --numeric-owner \
  -C "$api_artifacts" -cf - requirements.lock wheelhouse SHA256SUMS \
  | gzip -n >"$partial/assets/api-build-artifacts.tar.gz"
tar --sort=name --mtime="@$source_epoch" --owner=0 --group=0 --numeric-owner \
  -C "$attestations" -cf - . | gzip -n >"$partial/attestations.tar.gz"

if [[ -n $host_artifacts ]]; then
  tar --sort=name --mtime="@$source_epoch" --owner=0 --group=0 --numeric-owner \
    -C "$host_artifacts" -cf - . | gzip -n >"$partial/host-artifacts.tar.gz"
fi
profile_count=0
if find "$harmonization" -type l -print -quit | grep -q .; then
  die "harmonization bundle must not contain symlinks"
fi
if [[ -d $harmonization/profiles ]]; then
  while IFS= read -r -d '' profile; do
    python3 "$repo_dir/ops/harmonization/manage.py" verify \
      --profile "$profile" --harmonization-root "$harmonization" >/dev/null \
      || die "harmonization profile verification failed: $profile"
    python3 "$repo_dir/ops/harmonization/manage.py" verify-runtime-images \
      --profile "$profile" --image-lock "$lock_file" >/dev/null \
      || die "harmonization profile build images differ from release: $profile"
    ((profile_count += 1))
  done < <(find "$harmonization/profiles" -maxdepth 1 -type f -name '*.json' -print0 | sort -z)
fi
[[ -s $harmonization/expected-active-profiles.json ]] \
  || die "harmonization/expected-active-profiles.json is required"
python3 "$repo_dir/ops/harmonization/manage.py" verify-expected-inventory \
  --inventory "$harmonization/expected-active-profiles.json" \
  --profiles "$harmonization/profiles" >/dev/null \
  || die "expected active harmonization inventory is invalid"
tar --sort=name --mtime="@$source_epoch" --owner=0 --group=0 --numeric-owner \
  -C "$harmonization" -cf - . | gzip -n >"$partial/assets/harmonization.tar.gz"
python3 "$script_dir/harmonization_release_policy.py" \
  --inventory "$harmonization/expected-active-profiles.json" \
  --profile-count "$profile_count" \
  --bootstrap-allowed "$allow_empty_harmonization_bootstrap" \
  || die "harmonization release policy validation failed"

if podman volume exists "$hippunfold_volume"; then
  podman volume export --output "$partial/assets/hippunfold-cache.tar" "$hippunfold_volume"
else
  die "required HippUnfold cache volume is absent: $hippunfold_volume"
fi
python3 "$script_dir/cache-manifest.py" create "$partial/assets/hippunfold-cache.tar" \
  "$partial/assets/hippunfold-cache-files.sha256"
hippunfold_cache_sha=$(sha256sum "$partial/assets/hippunfold-cache-files.sha256" | awk '{print $1}')
harmonization_inventory_sha=$(sha256sum \
  "$harmonization/expected-active-profiles.json" | awk '{print $1}')

while read -r role ref; do
  printf 'exporting %-12s %s\n' "$role" "$ref"
  podman save --format oci-archive --output "$partial/images/$role.oci.tar" "$ref"
done < <("$script_dir/image-lock.sh" --lock "$lock_file" list "$scope")

signer_sha256=$(openssl pkey -in "$signing_key" -pubout -outform DER 2>/dev/null | sha256sum | awk '{print $1}')
cat >"$partial/release.env" <<EOF
MELD7T_RELEASE_FORMAT=1
MELD7T_RELEASE_ID=$release_id
MELD7T_GIT_SHA=$git_sha
MELD7T_MAP_SCRIPT_SHA256=$(sha256sum "$repo_dir/containers/map/segment.m" | awk '{print $1}')
MELD7T_HIPPUNFOLD_CACHE_SHA256=$hippunfold_cache_sha
MELD7T_HARMONIZATION_INVENTORY_SHA256=$harmonization_inventory_sha
MELD7T_SOURCE_DATE_EPOCH=$source_epoch
MELD7T_SIGNER_SHA256=$signer_sha256
MELD7T_IMAGE_SCOPE=$scope
MELD7T_HOST_ARTIFACTS=$([[ -n $host_artifacts ]] && printf included || printf external-prerequisite)
MELD7T_HARMONIZATION_PROFILES=$profile_count
MELD7T_HARMONIZATION_COHORT_BOOTSTRAP_ALLOWED=$allow_empty_harmonization_bootstrap
EOF

(cd "$partial" && find . -type f ! -name SHA256SUMS ! -name SHA256SUMS.sig -print0 \
  | sort -z | xargs -0 sha256sum >SHA256SUMS)
openssl dgst -sha256 -sign "$signing_key" -out "$partial/SHA256SUMS.sig" "$partial/SHA256SUMS"
chmod -R a-w "$partial"
mv -- "$partial" "$output"
trap - EXIT
printf 'signed air-gap release written to %s\n' "$output"
printf 'transfer the trusted public key separately; verify before import\n'
