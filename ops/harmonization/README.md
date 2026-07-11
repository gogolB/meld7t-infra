# Scanner/protocol harmonization workflow

Build harmonization profiles only inside the `meld-dev` development container. Production may
verify, import, activate, assign, and apply signed profiles, but it must never estimate new
parameters from research subjects.

A profile is immutable and detector-specific. Treat scanner hardware/station, scanner software,
acquisition protocol, source role, reconstruction/detector version, control cohort, and parameter
artifacts as part of its identity. Any change creates a new profile version.

## Cohort and selector inputs

Use one control cohort acquired on one scanner with one protocol. Both supported profile builders
require at least 20 controls by default, more than one age, and more than one normalized sex category.
`subjects.txt` contains one BIDS ID per line; `demographics.csv` contains ID, age, and sex columns.
The output cohort manifest contains only counts, eligibility flags, and site-keyed HMACs—not
subject rows or demographics. The HMAC key and source files remain sensitive and must stay in the
controlled build workspace; never include the key in a release.

Scanner station/protocol fields are minimized but may still identify a site or contain free text.
Treat selectors as protected research metadata. Start with `selector.example.json` and make it as
specific as the validated cohort permits. Every selector field is required at match time. Include
coil, acceleration, bandwidth, matrix, phase-encoding, reconstruction, and software fields when
they distinguish validated protocols.

Before either finalizer runs, an independent scientific review must produce a minimized,
restricted-access `validation-report.json`. It binds the exact profile code/version/detector, acquisition
fingerprints, QC inclusion/exclusion counts, positive/negative/control holdouts, immutable build
images, methodology/metric/golden-case evidence hashes, approval ID, reviewer, and timestamp. The
software validates this evidence contract; it cannot establish scientific validity by itself.

## MELD Distributed ComBat

Resolve the accepted MELD image by digest and prepare a versioned draft:

```bash
MELD_IMAGE="$(ops/release/image-lock.sh get meld_graph)"

python ops/harmonization/manage.py prepare \
  --code H_SITE_PROTOCOL \
  --version 1 \
  --name "Site protocol v1" \
  --subjects /controlled/cohort/subjects.txt \
  --demographics /controlled/cohort/demographics.csv \
  --selector /controlled/cohort/selector.json \
  --evidence-hmac-key-file /secure/cohort-evidence-hmac.key \
  --image "$MELD_IMAGE" \
  --output /controlled/build/H_SITE_PROTOCOL-v1
```

Run the exact `container_command` emitted in `cohort-manifest.json` with the same cohort mounted at
`/data`, the accepted licenses mounted read-only, and `--network=none`. The current MELD command is:

```text
python scripts/new_patient_pipeline/new_pt_pipeline.py \
  -harmo_code H_SITE_PROTOCOL \
  -ids /data/subjects.txt \
  -demos /data/demographics.csv \
  --harmo_only
```

Finalize the one expected ComBat parameter file:

```bash
python ops/harmonization/manage.py finalize \
  --profile-draft /controlled/build/H_SITE_PROTOCOL-v1/profile-draft.json \
  --artifact-source /controlled/meld-output \
  --output /release/harmonization/artifacts/H_SITE_PROTOCOL/v1 \
  --manifest-prefix artifacts/H_SITE_PROTOCOL/v1 \
  --validation-report /controlled/acceptance/H_SITE_PROTOCOL-v1-validation.json \
  --final-profile /release/harmonization/profiles/H_SITE_PROTOCOL-v1.json
```

Finalization requires exactly `MELD_H_SITE_PROTOCOLcombat_parameters.hdf5` below the source tree.
The versioned output and profile paths must not already exist.

MELD’s current guidance calls for a same-scanner/same-protocol cohort with at least 20 subjects and
age/sex variation. Review the pinned MELD version’s documentation during scientific acceptance;
the upstream reference is [MELD Graph’s installation guide](https://meld-graph.readthedocs.io/).

## MAP normative profile

Generation and scientific validation of MAP control statistics is a governed external analysis
step. This repository packages and validates the resulting profile; it does not claim that a set
of mean/std images is scientifically suitable merely because its files are well formed.

The artifact source must contain exactly the required statistics at these canonical paths:

```text
normative/map/junction_mean.nii.gz
normative/map/junction_std.nii.gz
normative/map/extension_mean.nii.gz
normative/map/extension_std.nii.gz
```

All four images must be finite, non-empty 3D NIfTI files on one identical grid. Standard-deviation
maps must be non-negative and contain at least one usable positive value. Package them with the
exact SPM and pkg images used to build the statistics:

```bash
python ops/harmonization/manage.py map-finalize \
  --code MAP_SITE_PROTOCOL \
  --version 1 \
  --name "MAP site protocol v1" \
  --subjects /controlled/cohort/subjects.txt \
  --demographics /controlled/cohort/demographics.csv \
  --selector /controlled/cohort/selector.json \
  --artifact-source /controlled/map-control-statistics \
  --output /release/harmonization/artifacts/MAP_SITE_PROTOCOL/v1 \
  --manifest-prefix artifacts/MAP_SITE_PROTOCOL/v1 \
  --final-profile /release/harmonization/profiles/MAP_SITE_PROTOCOL-v1.json \
  --cohort-manifest /release/attestations/MAP_SITE_PROTOCOL-v1-cohort.json \
  --validation-report /controlled/acceptance/MAP_SITE_PROTOCOL-v1-validation.json \
  --evidence-hmac-key-file /secure/cohort-evidence-hmac.key \
  --spm-image "$(ops/release/image-lock.sh get spm)" \
  --pkg-image "$(ops/release/image-lock.sh get pkg)"
```

## Verify, release, and activate

Verify every final profile before release export:

```bash
python ops/harmonization/manage.py verify \
  --profile /release/harmonization/profiles/H_SITE_PROTOCOL-v1.json \
  --harmonization-root /release/harmonization
```

Build the signed expected-active inventory from exactly the scanner/protocol profiles that this
site will activate (repeat `--profile` for each distinct profile code):

```bash
python ops/harmonization/manage.py expected-inventory \
  --profile /release/harmonization/profiles/H_SITE_PROTOCOL-v1.json \
  --profile /release/harmonization/profiles/MAP_SITE_PROTOCOL-v1.json \
  --output /release/harmonization/expected-active-profiles.json
```

Install the minified JSON array as `MELD7T_HARMONIZATION_EXPECTED_PROFILES`. Readiness remains red
until the database's exact active document hashes match this signed inventory and a background
integrity scan verifies the complete artifact closure.

The signed air-gap exporter repeats this verification. Include cohort manifests and scientific
approval evidence in the release attestations. Every new release must carry artifacts for all
active profiles and any profile whose failed run remains eligible for same-release retry. Retire a
profile and drain/rebuild affected work before removing its artifacts.

Export, transfer verification, and production installation also compare each profile's
`parameters.build_images` with the signed `images.lock`. A profile built with a different MELD,
SPM, or pkg image must become a new validated profile/release combination; the worker repeats this
identity check before applying it.

For a development/research server outside the controlled production installer, create the draft row
from the final JSON using an administrator identity. The JSON is already the body expected by the
API:

```bash
PROFILE=/release/harmonization/profiles/H_SITE_PROTOCOL-v1.json
BASE=https://research-server.example:9443

curl --fail --cacert /trusted/hospital-ca.pem --user "$MELD7T_ADMIN_USER" \
  -H 'Content-Type: application/json' -H 'X-MELD-CSRF: 1' \
  --data-binary "@$PROFILE" "$BASE/api/harmonization/profiles"
```

A different administrator must call `POST /api/harmonization/profiles/{id}/validate`; this verifies
the artifacts, detector-specific contract, and signed scientific-validation summary. The validating
administrator cannot activate it. A second administrator then calls
`POST /api/harmonization/profiles/{id}/activate`. In each case, confirm series roles, review the
ranked candidates under **Harmonization**, and explicitly assign a profile for every MELD/MAP
source before building the recipe. The case stays partial until every current target is assigned.

The supported Bazzite production path does not open an unready server for manual bootstrapping.
During the backup-gated migration job, `app.profile_import` validates the signed expected inventory,
profile documents, complete artifact closure, and embedded independent-review evidence, then
activates exactly that set and appends one release-bound audit event. Release approval is the second
approval boundary. Any database document mismatch, retired-version reactivation, missing artifact,
or API-env/inventory mismatch aborts migration before normal activation.

Selector mismatches and unharmonized server runs are administrator-only overrides and require a
substantive reason. Selector-override runs remain visible for research review but are excluded from
cross-detector concordance.
