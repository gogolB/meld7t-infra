# Scanner/protocol harmonization workflow

MELD profiles can be estimated from a deidentified control cohort on the air-gapped Bazzite server.
The workflow is administrative and research-only: it must not ingest from a clinical PACS directly,
and its outputs are not evidence of diagnostic or scientific validity. One authenticated
administrator may operate every state transition; immutable inputs, explicit QC/evidence, and the
append-only audit trail remain required even though separate administrator identities are not.

The server workflow in this document is a release contract. Enable it only when the installed
release contains the cohort API/UI, `meld7t-harmonization-builder.service`, its dedicated queue, and
the separate `harmonization-orthanc` service, and the complete workflow has passed site acceptance.
The manual CLI procedure remains the controlled fallback for releases without all of those pieces.

A profile is immutable and detector-specific. Treat scanner hardware/station, scanner software,
acquisition protocol, source role, reconstruction/detector version, control cohort, build
configuration, and parameter artifacts as part of its identity. Any change creates a new candidate
and profile version; it never updates an active profile in place.

## Storage and ingestion boundary

Use two Orthanc instances on the same host:

- Research Orthanc stores ordinary submitted research cases.
- `harmonization-orthanc` stores only deidentified controls used to build profiles. It has its own
  credential, database, `harmonization-orthanc-storage` volume, retention policy, backup scope, and
  isolated `meld-harmonization-net` network.

The transient builder workspace is separate again at
`$HOME/meld7t-state/harmonization-builds`. It holds a private hash-verified DICOM snapshot and
untrusted fold/final outputs only while a job needs them. Successful workspaces are removed after
QC publication; failed or abandoned raw workspaces are removed immediately when safe and by the
configured reaper no later than 24 hours. That reaper never removes a `building/publishing`
workspace after its artifact directory was durably renamed; deterministic publication
reconciliation owns it until the profile/SQL/audit commit reaches `qc_review`. Minimized frozen
manifests, structured job/QC/resource evidence, and audit records remain in the database. Activated
artifacts are atomically copied into versioned read-only profile storage. Orthanc is a
transport/quarantine store, not the authoritative store for a built profile. Its capacity mode
rejects new REST/DICOM objects at the hard storage cap; it must never recycle an older frozen
control to make room. The production health timer independently queries Orthanc statistics and
alerts at 85% of that cap before rejection begins.

The workflow accepts these controlled ingestion paths:

1. C-STORE or controlled filesystem import followed by administrator selection in the web
   application. Production publishes AE `MELD7T_HARMONIZATION` only on host loopback port `4243`,
   suitable for local `storescu` after controlled media transfer. Receiving directly from a remote
   allowlisted research sender requires a reviewed Quadlet drop-in plus source-scoped firewall
   rule; never expose the unauthenticated DICOM listener generally. Receipt in Orthanc is
   quarantine, not cohort admission.
2. Resumable browser upload through administrator-only create, chunk, and complete operations.
   Uploads must enforce configured total/file limits, quotas, checksums, archive traversal and
   expansion limits, DICOM parsing, supported transfer syntaxes, and cleanup of abandoned chunks.
   The server replaces the workstation filename with an opaque upload ID plus validated format
   suffix; it never persists or reports the uploaded filename/path. Status and audit failures use
   bounded machine codes, dropping uploader-controlled exception text that could expose a path or
   identifier.

   The importer writes a canonical, newline-delimited, append-only receipt and fsyncs every record.
   Its header binds the upload SHA-256, canonical instance-manifest SHA-256, and instance count.
   Each intent binds file SHA-256, SOP Instance UID, and byte count; each stored record binds the
   Orthanc instance ID and a worker-ownership boolean. A sorted canonical projection of those
   records produces `receipt_evidence_sha256`, which is retained with rollback state and checked
   again before reconciliation.

   The worker verifies crash-resumed instances byte-for-byte and automatically rolls back only
   instances it can prove it created if the SQL/audit commit fails. An `AlreadyStored` response is
   explicitly pre-existing and is never selected by a later exact-delete approval. Intent-only
   objects after a lost response are never assumed to be worker-owned because they can race an
   independent C-STORE. They remain in an explicit operator-reconciliation state;
   incomplete rollback is never silently treated as admitted. A host file lock serializes
   import/rollback effects across builder processes, and an `importing` database state is requeued
   after process or host interruption. While ambiguity remains, the protected receipt is retained,
   the builder heartbeat reports rollback pending/storage unavailable, and all
   application-controlled upload ingestion, cohort admission, build admission, and readiness are
   fenced globally. Receipt evidence is not subject to the transient-workspace 24-hour deletion
   ceiling.
   Successful browser imports return protected StudyInstanceUID-to-pseudonymous-PatientID mappings
   that the admin UI can copy into the admission form; no manual demographics alias is accepted.
   Copy/admit them promptly: on the first reconciliation cycle after the configured expiry
   (hard-capped at 24 hours), the API removes the plaintext pseudonym from the upload record while
   retaining Study UIDs and audit evidence. An admin must load the protected receipt evidence in
   the UI and close an ambiguous rollback with a substantive reason and evidence digest.
   `Preserve` is limited to ambiguous or already cohort-referenced objects; its evidence is the
   SHA-256 of the site's external ownership/reference attestation. It leaves the object in
   quarantine and closes the global gate. `Exact delete` is refused for any cohort reference or
   receipt-integrity failure and requires the supplied evidence digest to equal the displayed
   canonical `receipt_evidence_sha256`. It authorizes the builder to re-verify exact
   SOP/size/SHA-256 and delete; candidate-verification or deletion failures keep the gate closed
   until that audited reconciliation succeeds.

Browser upload state is explicit: `receiving → staged → importing → imported`, with `failed` as the
terminal error state. A completion retry is idempotent while staged, importing, or imported.

Never mix controls into Research Orthanc, expose either Orthanc credential to the browser, or use
labels in one Orthanc as a substitute for this storage and retention boundary. A deployment that
has not implemented and tested the complete ingestion path must leave that path disabled.

## Cohort preparation and freezing

Create one cohort for one site, scanner/station, scanner software version, acquisition protocol,
source role, and intended profile code/version. Twenty to forty eligible controls is the normal
operating range; 20 is the hard software minimum for MELD and is not a guarantee of adequate power
or representativeness. More controls may be admitted within the site's configured storage quota.

Before a cohort can become ready, verify:

- Every selected source is MR and has rows, columns, and voxel spacing. Every instance is a DICOM
  Part-10 object whose exact bytes, byte count, and SOP binding are recorded at admission. Modality
  and every scanner/protocol fingerprint field are parsed from those same bytes, must be consistent
  across the series, and are not trusted from a prior QIDO index response.
- Every instance declares `PatientIdentityRemoved=YES`, a nonempty `DeidentificationMethod` or code
  sequence, and `BurnedInAnnotation=NO`; prohibited direct identifiers and person-name values are
  absent. Private tags are rejected unless their exact `GGGG,EEEE` number appears in the shared,
  site-reviewed allowlist (empty by default). These metadata attestations do not replace required
  pixel-PHI review.
- SOP Instance UIDs, subjects, and files are not duplicated; required sequences are present; and
  transfer syntaxes are supported. The exact DICOM pseudonymous PatientID is the cohort subject key
  and remains the only demographics join key. It must be a nonempty, comma-free single line of at
  most 128 characters so it is also unambiguous in the constrained demographics CSV.
- Scanner, field strength, station, software, protocol, acquisition fingerprint, source role, and
  geometry are consistent with the proposed selector. Outliers must be resolved or excluded with
  an audited reason.
- The constrained demographics CSV uses the exact headers `ID,Age,Sex`, has exactly one row per
  included pseudonym, allowed MELD values, no duplicate or unmatched rows, and sufficient
  age/normalized-sex variation for the approved method.
  Each normalized sex stratum must contain at least one control per configured fold (five by
  default), so every held-out fold and every training partition retains categorical coverage.

Scanner/station/protocol fields are minimized but may still identify a site or contain free text.
Treat selectors and demographics as protected research metadata. Start with
`selector.example.json` and make the selector as specific as the validated cohort permits. Every
selector field is required at match time. Include coil, acceleration, bandwidth, matrix,
phase-encoding, reconstruction, and software fields when they distinguish validated protocols.

After validation moves the cohort to `cohort_ready`, an administrator must explicitly call the
freeze operation before creating a build. Freezing binds the selected-source Part-10 byte closure,
keyed subject/study identities, demographics hash, acquisition fingerprints, selector, and builder
configuration. Raw source UIDs remain protected operational references in Orthanc/database while
needed; they are not copied into portable QC/profile evidence. Build creation then binds the
builder image and reviewed adapter digests. No membership or metadata changes are permitted after the freeze. Unfreezing
is not a correction mechanism: exclude or add data in a new cohort/build candidate and retain only
the minimized abandoned record for audit.

Study admission prepares large hashes outside a SQL transaction, then re-downloads and compares
the exact selected closure under the same transaction-scoped database fence used by both initial
and reconciled rollback deletion. The membership/audit commit releases that fence. A rollback that
runs afterward must observe the referenced SOPs and preserve them; a rollback that runs first makes
the final admission recheck fail closed.

## Builder execution and internal cross-validation

Submit frozen cohorts to the dedicated `harmonization-builder` queue consumed by
`meld7t-harmonization-builder.service`. The worker runs separately from routine case inference and
uses an offline, digest-pinned builder image. The API permits build creation only while the builder
publishes a fresh heartbeat for the same release, image, adapter readiness, and storage capacity.
Configure CPU/GPU allocation, memory, disk watermark, whole-job timeout, cancellation, and a
concurrency limit. The production default is one active cohort build. MELD estimation also acquires
the same fenced GPU lease as routine detector execution, preventing overlap on the physical GPU.
A second non-build worker slot lets verified upload ingestion continue while that single build
waits for or uses the GPU; the API still admits only one live build server-wide.

Before estimation, the worker re-queries the selected SOP inventory, re-downloads each Part-10
object into its private workspace, and compares the frozen byte count and SHA-256. Any addition,
deletion, replacement, truncation, or non-Part-10 response fails the build closed.

The orchestrator intentionally does not invent the upstream scientific preprocessing step. A site
must install a reviewed absolute executable and configure both
`MELD7T_HARMONIZATION_BUILDER_ADAPTER` and
`MELD7T_HARMONIZATION_BUILDER_ADAPTER_SHA256`; setting only one is invalid. The worker verifies the
digest immediately before use and invokes it without a shell. Production installation also rejects
a symlink, non-regular/non-executable path, or bytes whose SHA-256 differs, and configures the API
with that same expected digest. The invocation is:

```text
ADAPTER --request REQUEST.json --output NEW_DIRECTORY --mode cross-validation --fold N
ADAPTER --request REQUEST.json --output NEW_DIRECTORY --mode final
```

The request binds the frozen cohort, HMAC subject keys, minimized demographics, selector, fold
membership, acceptance criteria, exact MELD image digest, and workspace-relative snapshot paths
with their hashes. It does not give the adapter source UIDs, Orthanc credentials, or Orthanc
endpoints. The adapter may read only the worker-owned snapshot, prepares accepted MELD inputs, and
launches the request's digest-pinned image. Each invocation writes a bounded `result.json`
containing `{"passed": boolean, "metrics": {...}}`; metrics are finite numeric scalars with safe
names. The final invocation also produces exactly `MELD_<CODE>combat_parameters.hdf5`. Until this
executable passes site golden-cohort acceptance, build admission fails closed as adapter-not-ready
(and the worker independently rejects an unconfigured adapter); ingestion and cohort preparation
remain usable.

MELD builds use deterministic stratified five-fold internal cross-validation by default. Each
control appears in held-out evaluation exactly once; no held-out control may influence that fold's
estimated parameters. Record the deterministic fold assignment as keyed hashes rather than raw
subject identifiers. Each fold must return a nonempty set of finite numeric metrics containing
every value named by the versioned acceptance criteria. Missing, boolean, NaN, or infinite metrics
fail the fold.

If every fold and configured acceptance criterion passes, run a separate final fit using all
eligible controls. The final fit must return a second nonempty finite metric set and pass the
configured final-fit gates (or the explicitly shared gates). That all-control fit, not any fold
artifact, becomes the candidate profile.
Capture the frozen input hashes, configuration, image and adapter digests, resource use, structured
error codes, fold metrics, exclusions, failure/cancellation reason, final artifact hashes, and QC
document hashes in the transactional and append-only audit trails. For an on-server generated
profile, the admitted adapter digest must match the build row, request, QC report, artifact manifest,
scientific-validation report, profile parameters, runtime trust gate, and release export.

Publication first copies into a build-owned pending directory and atomically renames the verified
artifact directory. Before that rename, a permanent copy/fsync error fails normally. After it, the
build remains `building/publishing`, cancellation is refused, and any SQL/profile/audit failure is
recorded as `publication_finalization_pending`; the workspace and durable artifact are retained
until deterministic reconciliation completes `qc_review`.

Internal cross-validation only measures stability within the submitted cohort. It does not replace
independent positive, negative, or control holdouts; protocol suitability review; golden cases; or
expert scientific review. Acceptance thresholds are versioned approved configuration, not silently
chosen by the application. The retained JSON QC report, as rendered in the admin UI, must clearly
separate engineering integrity checks, internal CV, and external evidence.

## Review, activation, and lifecycle

Cohorts progress through `draft`, `cohort_ready`, `frozen`, and `archived`. Builds progress through
`queued`, `building`, `qc_review`, `validated`, and `active`, with `failed` and `cancelled` terminal
outcomes. An active profile may later be `retired`; retirement never deletes its history or
artifacts while retained runs still depend on it.

Rejecting a `qc_review` candidate records the reason, retires the unactivated candidate profile,
and archives the frozen cohort. A corrected attempt requires a new cohort and profile version; the
system never edits or retries rejected scientific parameters in place.

An administrator approves/freezes the cohort and starts its build. At `qc_review`, an administrator
reviews the full QC/evidence package and either rejects it or records validation; activation remains
a subsequent explicit action. The same administrator may perform all of these actions. Activation
still rechecks artifact hashes, detector semantics, selector overlap, builder image and adapter
identity, cohort closure, and QC hashes. Initiator, validator, and activator fields retain the actual
actor and timestamp at each phase, even when they name one person. Failures, retries, cancellations,
exclusions, and temporary overrides remain audited.

Activation on the server is a local audited promotion. The generated profile must be linked to its
active build, declare `parameters.storage_scope` as `generated`, and retain the frozen cohort, QC,
artifact, and pinned builder-image/adapter hashes. This does not rewrite the signed release inventory.
Back up the generated profile and evidence immediately, then export and sign it into the next
release so it can be reproduced during replacement-host installation or disaster recovery.

Before validation, the scientific evidence review must produce a minimized, restricted-access
`validation-report.json`. It binds the exact profile code/version/detector, acquisition
fingerprints, QC inclusion/exclusion counts, positive/negative/control holdouts, immutable build
images, methodology/metric/golden-case evidence hashes, approval ID, reviewer, and timestamp. The
software requires that exact minimized schema, including exact nested QC/holdout keys, and rejects
extra free-text fields so patient/site notes cannot be copied into profiles, runs, or releases. It
validates the evidence contract; it cannot establish scientific validity by itself. On-server
generated MELD reports also require the exact admitted `builder_adapter_sha256`; offline signed MELD
profiles that never used this adapter workflow remain valid without that field, but a partially
present or mismatched adapter binding is always rejected.

## Manual/offline MELD Distributed ComBat

The CLI path is the fallback for a release that does not expose the complete server builder and is
also useful for preparing a profile on a controlled release workstation. Run it inside the
`meld-dev` development container, never against the Research Orthanc. `subjects.txt` contains one
BIDS ID per line; `demographics.csv` contains ID, age, and sex columns. The cohort manifest contains
only counts, eligibility flags, and site-keyed HMACs—not subject rows or demographics. Keep the
HMAC key and source files in the controlled build workspace and never include the key in a release.

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

MAP is not yet an on-server cohort-builder method. It will implement the same frozen-cohort,
dedicated-queue, QC, evidence, versioning, and recorded-approval contract only after its
estimation procedure and acceptance metrics receive scientific review. Do not route a MAP cohort
to the MELD Distributed ComBat adapter.

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

## Promote a generated profile into a signed release

After a locally generated MELD build reaches `active`, an administrator or auditor can call
`GET /api/harmonization/builds/{id}/release-export` or use **Prepare release export** in the cohort
UI. The API re-verifies the linked build evidence and generated artifact closure before returning:

- `profile_document`, the complete release profile document;
- `suggested_profile_path`, its destination below the release harmonization root;
- `expected_inventory_entry`, including the exact document SHA-256;
- `artifact_copy_plan`, with generated/release relative paths, sizes, and SHA-256 values; and
- `release_signing_required: true`.

The response is a staging recipe, not a signed artifact. On the controlled release workstation:

1. Copy every generated artifact to the exact release-relative path in the copy plan and verify its
   size and SHA-256. Do not copy the transient build workspace or raw DICOM snapshot.
2. Write `profile_document` at `suggested_profile_path`. The only permitted generated-to-release
   transformation is `parameters.storage_scope` changing from `generated` to `release`; code,
   version, selector, every other parameter, validation/QC hashes, artifact manifest, and build
   image must remain identical.
3. Add `expected_inventory_entry` to `expected-active-profiles.json`, retaining the other intended
   active profiles, and confirm inventory generation produces the same document digest.
4. Run the profile/inventory and full air-gap verification, then sign/export the complete release
   with the normal release key. Never treat the API/UI export JSON itself as approval or signature.

During the backup-gated migration, `app.profile_import` recognizes only that exact signed
`generated` to `release` trust-root change. It promotes the existing active database row instead of
creating a conflicting profile. Any change to identity, selector, evidence, parameters, or artifact
hashes aborts import. Retain the locally generated artifacts and backup until the signed release has
been independently verified and restored in a drill.

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

Install the minified JSON array as `MELD7T_HARMONIZATION_EXPECTED_PROFILES`. Readiness requires every
release-provided active profile to match that inventory exactly and verifies the complete artifact
closure in the background. It may additionally accept a locally generated active profile only when
that profile satisfies the linked-build, recorded-approval, frozen-input, QC/artifact-hash,
builder-image/adapter, and `storage_scope=generated` contract above. An ad hoc profile row, unlinked
artifact, or partially approved candidate keeps readiness red.

For a virgin site database with no established signed release-profile history, create a signed
empty bootstrap inventory explicitly:

```bash
python ops/harmonization/manage.py expected-inventory \
  --allow-empty-bootstrap \
  --output /release/harmonization/expected-active-profiles.json
```

When exporting that release, pass both `--harmonization /release/harmonization` and
`--allow-empty-harmonization-bootstrap` to `ops/release/export-airgap.sh`. The exporter accepts the
flag only when the bundle contains zero profile documents and the expected inventory is exactly
`[]`; it signs `MELD7T_HARMONIZATION_COHORT_BOOTSTRAP_ALLOWED=true` into `release.env`. Verification
and installation derive the API setting from that signed value, so do not edit the production API
environment to enable it. API readiness then permits cohort administration and building with zero
active profiles. `MELD7T_HARMONIZATION_REQUIRED=true` makes routine recipe construction require an
explicit choice: assign a matching profile or confirm **Include runnable detectors without a
matching harmonization profile**. The latter creates an unharmonized execution contract and
prominent warnings in the plan, queue/status, Review Study, derived DICOM provenance, and every
combined PDF; it does not silently imply harmonization. After the first generated profile is backed
up and promoted, the next signed release must carry a non-empty inventory and the authorization
returns to `false`. The importer rejects an empty bootstrap once any signed release profile has
existed, even if that profile was later retired, so bootstrap cannot downgrade an established
inventory.

The signed air-gap exporter repeats this verification. Include cohort manifests and scientific
approval evidence in the release attestations. Every new release must carry artifacts for all
active profiles and any profile whose failed run remains eligible for same-release retry. Retire a
profile and drain/rebuild affected work before removing its artifacts.

Export, transfer verification, and production installation also compare each profile's
`parameters.build_images` with the signed `images.lock`. A profile built with a different MELD,
SPM, or pkg image must become a new validated profile/release combination; the worker repeats this
identity check before applying it.

For a development-mode server only, create a draft row from the final JSON using an administrator
identity. Research and production modes reject these generic mutation endpoints and require either
signed inventory import or the linked audited cohort-build workflow. The JSON is already
the body expected by the development API:

```bash
PROFILE=/release/harmonization/profiles/H_SITE_PROTOCOL-v1.json
BASE=https://research-server.example:9443

curl --fail --cacert /trusted/hospital-ca.pem --user "$MELD7T_ADMIN_USER" \
  -H 'Content-Type: application/json' -H 'X-MELD-CSRF: 1' \
  --data-binary "@$PROFILE" "$BASE/api/harmonization/profiles"
```

A (not necessarily different) administrator calls `POST /api/harmonization/profiles/{id}/validate`;
this verifies the artifacts, detector-specific contract, and scientific-validation summary. That
administrator may then call `POST /api/harmonization/profiles/{id}/activate`. For routine cases,
confirm series roles, review the
ranked candidates under **Harmonization**, and explicitly assign a profile for every MELD/MAP
source that will be harmonized before building the recipe. A user may instead confirm an
unharmonized detector entry; this is an explicit, visible processing mode rather than a profile
assignment.

The supported Bazzite production path permits only the explicit signed empty-inventory bootstrap
described above; it never permits ad hoc active profiles. During the backup-gated migration job,
`app.profile_import` validates the signed expected inventory,
profile documents, complete artifact closure, and embedded scientific-review evidence, then
activates exactly that set and appends one release-bound audit event. Release approval is the second
approval boundary. Any database document mismatch, retired-version reactivation, missing artifact,
or API-env/inventory mismatch aborts migration before normal activation.

Selector mismatches remain administrator-only overrides and require a substantive reason. A case
creator/assignee may confirm unharmonized runs without an administrator. Selector-override and
unharmonized runs remain visible for research review, carry their distinct provenance/warnings, and
are excluded from cross-detector concordance.
