#!/usr/bin/env bash
set -Eeuo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
bundle=${1:?usage: verify-airgap.sh BUNDLE_DIR TRUSTED_PUBLIC_KEY_PEM}
trusted_key=${2:?usage: verify-airgap.sh BUNDLE_DIR TRUSTED_PUBLIC_KEY_PEM}
die() { printf 'verify-airgap: %s\n' "$*" >&2; exit 1; }

[[ -d $bundle && -f $trusted_key ]] || die "bundle or trusted public key is missing"
for file in SHA256SUMS SHA256SUMS.sig release.env images.lock source.tar.gz \
  assets/web-dist.tar.gz assets/worker-artifacts.tar.gz assets/hippunfold-cache.tar \
  assets/hippunfold-cache-files.sha256 attestations.tar.gz; do
  [[ -f $bundle/$file ]] || die "required bundle member is absent: $file"
done
[[ -f $bundle/assets/api-build-artifacts.tar.gz ]] || die "API build artifacts are absent"
[[ -f $bundle/assets/harmonization.tar.gz ]] || die "signed harmonization path is absent"
if find "$bundle" -type l -print -quit | grep -q .; then die "bundle must not contain symlinks"; fi

openssl dgst -sha256 -verify "$trusted_key" -signature "$bundle/SHA256SUMS.sig" "$bundle/SHA256SUMS" \
  >/dev/null || die "release signature is invalid"
(cd "$bundle" && sha256sum --strict --check SHA256SUMS >/dev/null) \
  || die "release checksum verification failed"

# shellcheck disable=SC1090
source "$bundle/release.env"
[[ ${MELD7T_RELEASE_FORMAT:-} == 1 ]] || die "unsupported release format"
[[ ${MELD7T_RELEASE_ID:-} =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]] || die "invalid release ID"
[[ ${MELD7T_HARMONIZATION_COHORT_BOOTSTRAP_ALLOWED:-} == true \
   || ${MELD7T_HARMONIZATION_COHORT_BOOTSTRAP_ALLOWED:-} == false ]] \
  || die "signed cohort bootstrap authorization must be true or false"
[[ ${MELD7T_HARMONIZATION_PROFILES:-} =~ ^[0-9]+$ ]] \
  || die "signed harmonization profile count is malformed"
actual_signer=$(openssl pkey -pubin -in "$trusted_key" -outform DER 2>/dev/null | sha256sum | awk '{print $1}')
[[ $actual_signer == "${MELD7T_SIGNER_SHA256:-}" ]] || die "trusted key fingerprint mismatch"
[[ ${MELD7T_GIT_SHA:-} =~ ^[0-9a-f]{40}$|^[0-9a-f]{64}$ \
   && ${MELD7T_MAP_SCRIPT_SHA256:-} =~ ^[0-9a-f]{64}$ \
   && ${MELD7T_HIPPUNFOLD_CACHE_SHA256:-} =~ ^[0-9a-f]{64}$ \
   && ${MELD7T_HARMONIZATION_INVENTORY_SHA256:-} =~ ^[0-9a-f]{64}$ ]] \
  || die "signed source provenance is malformed"

"$script_dir/image-lock.sh" --lock "$bundle/images.lock" validate "${MELD7T_IMAGE_SCOPE:-runtime}" >/dev/null
while read -r role _ref; do
  [[ -f $bundle/images/$role.oci.tar ]] || die "image archive is absent: $role"
done < <("$script_dir/image-lock.sh" --lock "$bundle/images.lock" list "${MELD7T_IMAGE_SCOPE:-runtime}")

tar -tzf "$bundle/source.tar.gz" >/dev/null
tar -tzf "$bundle/assets/web-dist.tar.gz" >/dev/null
tar -tzf "$bundle/assets/worker-artifacts.tar.gz" >/dev/null
tar -tzf "$bundle/assets/api-build-artifacts.tar.gz" >/dev/null
tar -tf "$bundle/assets/hippunfold-cache.tar" >/dev/null
[[ $(sha256sum "$bundle/assets/hippunfold-cache-files.sha256" | awk '{print $1}') \
   == "$MELD7T_HIPPUNFOLD_CACHE_SHA256" ]] || die "signed HippUnfold cache digest is incorrect"
python3 "$script_dir/cache-manifest.py" verify "$bundle/assets/hippunfold-cache.tar" \
  "$bundle/assets/hippunfold-cache-files.sha256" \
  || die "HippUnfold cache file closure differs from its signed manifest"
map_actual=$(tar -xOzf "$bundle/source.tar.gz" source/containers/map/segment.m | sha256sum | awk '{print $1}')
[[ $map_actual == "$MELD7T_MAP_SCRIPT_SHA256" ]] || die "signed MAP script digest is incorrect"
web_marker=$(tar -tzf "$bundle/assets/web-dist.tar.gz" | grep -E '(^|/)\.meld7t-git-sha$' || true)
[[ -n $web_marker && $(wc -l <<<"$web_marker") -eq 1 \
   && $(tar -xOzf "$bundle/assets/web-dist.tar.gz" "$web_marker") == "$MELD7T_GIT_SHA" ]] \
  || die "web bundle is not bound to the signed source revision"

attestation_tmp=$(mktemp -d /tmp/meld7t-attestation-verify.XXXXXX)
trap 'rm -rf -- "$attestation_tmp"' EXIT
tar -xzf "$bundle/attestations.tar.gz" -C "$attestation_tmp"
for evidence in approval.txt sbom.spdx.json vulnerability-report.json \
  vulnerability-exceptions.txt license-report.json golden-case-evidence.txt; do
  [[ -s $attestation_tmp/$evidence ]] || die "signed attestation is absent: $evidence"
done
exception_expiry=$(sed -n 's/^EXPIRES=//p' "$attestation_tmp/vulnerability-exceptions.txt")
[[ $exception_expiry =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ \
   && $(date -u -d "$exception_expiry" +%s) -ge $(date -u +%s) ]] \
  || die "signed vulnerability exception is expired or malformed"
rm -rf -- "$attestation_tmp"
trap - EXIT

harmonization_tmp=$(mktemp -d /tmp/meld7t-harmonization-verify.XXXXXX)
trap 'rm -rf -- "$harmonization_tmp"' EXIT
if tar -tzf "$bundle/assets/harmonization.tar.gz" \
  | grep -Eq '(^/|(^|/)\.\.(/|$))'; then
  die "harmonization archive contains an unsafe path"
fi
tar -xzf "$bundle/assets/harmonization.tar.gz" -C "$harmonization_tmp"
if find "$harmonization_tmp" -type l -print -quit | grep -q .; then
  die "harmonization archive contains a symlink"
fi
profile_count=0
if [[ -d $harmonization_tmp/profiles ]]; then
  while IFS= read -r -d '' profile; do
    python3 "$script_dir/../harmonization/manage.py" verify \
      --profile "$profile" --harmonization-root "$harmonization_tmp" >/dev/null \
      || die "harmonization profile failed verification: $profile"
    python3 "$script_dir/../harmonization/manage.py" verify-runtime-images \
      --profile "$profile" --image-lock "$bundle/images.lock" >/dev/null \
      || die "harmonization profile build images differ from signed release: $profile"
    ((profile_count += 1))
  done < <(find "$harmonization_tmp/profiles" -maxdepth 1 -type f -name '*.json' -print0 | sort -z)
fi
[[ -s $harmonization_tmp/expected-active-profiles.json ]] \
  || die "signed expected-active-profiles.json is absent"
python3 "$script_dir/../harmonization/manage.py" verify-expected-inventory \
  --inventory "$harmonization_tmp/expected-active-profiles.json" \
  --profiles "$harmonization_tmp/profiles" >/dev/null \
  || die "signed expected active profile inventory is invalid"
[[ $(sha256sum "$harmonization_tmp/expected-active-profiles.json" | awk '{print $1}') \
   == "$MELD7T_HARMONIZATION_INVENTORY_SHA256" ]] \
  || die "expected active profile inventory digest differs from release metadata"
[[ $profile_count == "${MELD7T_HARMONIZATION_PROFILES:-0}" ]] \
  || die "harmonization profile count differs from signed manifest"
python3 "$script_dir/harmonization_release_policy.py" \
  --inventory "$harmonization_tmp/expected-active-profiles.json" \
  --profile-count "$profile_count" \
  --bootstrap-allowed "$MELD7T_HARMONIZATION_COHORT_BOOTSTRAP_ALLOWED" \
  || die "signed harmonization release policy is invalid"
rm -rf -- "$harmonization_tmp"
trap - EXIT
printf 'verified release %s (%s)\n' "$MELD7T_RELEASE_ID" "${MELD7T_GIT_SHA:-unknown}"
