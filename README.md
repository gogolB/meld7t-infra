# MELD 7T research platform

MELD 7T is a research-only imaging analysis platform. It is not a diagnostic system, medical
device, or substitute for clinical interpretation. Outputs must not be used for diagnosis,
treatment, surgery, or patient management. The controls in this repository do not establish
HIPAA, regulatory, IRB, scientific, or institutional compliance.

Production activation remains conditional on the site acceptance work listed under
[Known blockers and residual risks](#known-blockers-and-residual-risks).

## Supported environments

- **Production:** a dedicated air-gapped Bazzite host with SELinux enforcing, rootless
  Podman/Quadlet, NVIDIA CDI, an institutional TLS certificate, and a dedicated noninteractive
  service account. Bazzite remains the deployment target because its atomic base and integrated
  NVIDIA driver path provide a strong, reviewable starting point.
- **Development, test, image build, and release engineering:** exclusively inside the rootless
  `meld-dev` Distrobox created by [ansible/bootstrap.yml](ansible/bootstrap.yml).
- **Server installation:** only [ansible/production.yml](ansible/production.yml), executed from a
  cryptographically verified staged release. It performs no network downloads.

Production does not use Distrobox, Homebrew, online package installation, registry pulls, or
automatic database migration. Development helpers such as `services-up`, `meld-setup`, `recon`,
editor/AI tooling, and the bootstrap play are not production procedures.

Development bootstrap on a Bazzite workstation:

```bash
ansible-playbook -i ansible/inventory.ini ansible/bootstrap.yml -K
just gpu-check
just dev
```

After `just dev`, keep all source, dependency, test, build, and release work inside `meld-dev`.

## Architecture and trust boundaries

| Component | Production role |
|---|---|
| Caddy | Institutional TLS and authenticated edge on `:9443`; OHIF origin on `:9444` |
| FastAPI | Case/workflow authority; read-only container filesystem and read-only result/profile mounts |
| Host worker | ARQ consumer that launches digest-pinned sibling containers; two case slots, one Redis-fenced GPU job |
| Harmonization builder | Administrative worker and queue for offline cohort estimation; isolated from routine case admission |
| PostgreSQL | Authoritative workflow, provenance, result, and transactional audit mirror |
| Redis | Password-protected persistent broker, status cache, worker heartbeat, and GPU lease |
| Research Orthanc | DICOM store for submitted research cases; never a general clinical PACS |
| Harmonization Orthanc | Separately credentialed DICOM store and volume for deidentified profile-building controls |
| immudb | Independently verified append-only audit copy with persistent client trust roots |

The production Quadlets separate `meld-edge`, `meld-data-net`, `meld-compute-net`, and
`meld-registry-net`. Only Caddy publishes the browser ports. PostgreSQL, Redis, the two Orthanc
instances, immudb, and the optional registry also bind only the interfaces required by their host
workers. Research and harmonization Orthanc use different credentials, databases, volumes, backup
and retention policies. Browser identities never receive either internal Orthanc credential.

Active compute state lives on encrypted local NVMe under `~/meld7t-state`. Durable import and
backup storage lives under `/var/mnt/meld7t`; production NFS defaults to `sec=krb5p`. The local
registry is an optional cache—the signed offline bundle is release authority. The API and worker
share a release-bound HMAC worker heartbeat, and readiness rejects a missing, stale, mismatched, or
capacity-exhausted consumer. The default admission contract reserves 300 GiB free for two jobs
(`50 GiB` floor plus `100 GiB` DICOM and `25 GiB` output headroom per slot); tune those explicit
limits only from measured site workloads.

Roles are `submitter`, `reviewer`, `admin`, `auditor`, and `service`. A submitter or administrator can
upload a routine study; the case creator, assignee, or an administrator can confirm its processing
plan. Every authenticated user has read-only access to the case-level Review Study and its
authenticated OHIF/DICOMweb data. Reviewer/admin identities may additionally append adjudications,
request or retry a combined-report version, and harmonization/profile administration remains
restricted to administrators. Every authenticated user may list and download existing combined
report versions. Full profile inventory and selector details are admin/auditor-only. Static Caddy
Basic Auth is a closed-network fallback, not institutional MFA/SSO.

## Research workflow

1. A submitter uploads one approved research Study Instance UID as a DICOM ZIP. The browser resumes
   the archive in bounded chunks; completion writes a durable queue request instead of processing in
   the API request. Controlled pre-import into Research Orthanc remains supported.
2. The worker checks the archive checksum and limits, rejects unsafe/non-DICOM content, mixed
   patient or study identities, and internally inconsistent series metadata, imports the exact
   instances into Research Orthanc, creates the case, and proposes a role for every uploaded series.
   A proposal never starts analysis.
3. The user reviews every uploaded scan, confirms the role of each usable series, and sees which
   exact study/series will be used for each MAP, MELD, or HS detector input.
4. Minimized but still protected acquisition metadata creates a site-keyed, schema-versioned
   scanner/protocol fingerprint. Coil, bandwidth, acceleration, matrix, phase-encoding,
   reconstruction, software, geometry, and pixel-scaling fields are included when present.
5. The user confirms a matching immutable harmonization profile where one is available. A profile
   is optional: the user may explicitly include an unharmonized MELD or MAP run. The processing plan,
   queue, Review Study, derived DICOM provenance, and every combined PDF then carry a prominent
   unharmonized warning. HippUnfold currently records harmonization as not applicable.
6. The application presents a versioned detector-by-input plan before submission. Confirmation
   binds exact source/companion Series Instance UIDs, fingerprints, profile document hashes (or the
   explicit unharmonized mode), parameters, and a stable input-contract hash.
7. Only after that human confirmation does a durable outbox queue the runnable detectors. Claim
   leases deliver work idempotently. Claim acceptance binds the signed
   release, runtime images, deadlines, and input contract. Before scientific execution or external
   publication, the worker re-hashes the selected profile, detector code/assets, and model cache.
8. The worker retrieves only contracted SOPs, checks study/patient/count/acquisition identity and
   hashes, then atomically publishes a closed local staging tree.
9. Digest-pinned detector compute and ingest containers run with no network. The dedicated DICOM
   packaging container joins only the private data network long enough to STOW and verify derived
   instances in Research Orthanc. GPU ownership is token-fenced; process groups are terminated on
   timeout/cancellation, and one whole-run deadline covers all stages.
10. Scientific output schemas and finite values are validated before publication. Completion
    commits result, clusters, provenance, output hashes, completion-bundle hash, and audit event
    together.
11. Every completed detector publishes viewer-ready output into one deterministic derived Review
    Study without changing the uploaded source Study UID. MELD publishes derived MR plus DICOM SEG;
    MAP publishes a native-T1 reference, threshold SEG, and quantitative z-score Parametric Maps;
    HS publishes a native-T2 reference and a bilateral subfield/asymmetry SEG. STOW responses, QIDO
    identities, WADO pixel/geometry semantics, and retained per-SOP/series manifests are checked.
12. The case-level **Review Study** shows every source series, the shared derived study, all current
    MAP, MELD, and HS findings, harmonization state, and report history. Every authenticated user may
    open its read-only OHIF viewer.
13. A preliminary white-labeled combined PDF is queued when all runnable analyses finish
    successfully. Final reports are available after every runnable result is adjudicated. Each PDF
    is an immutable, hash-bound, versioned snapshot; creating a new version never overwrites an
    earlier report. Detector-native PDFs remain internal scientific evidence and are not exposed as
    an alternate browser download that could bypass the combined report's branding and warnings.
14. Reviewers append adjudications. Corrections link to and supersede an earlier record; no review
    is overwritten. A terminal failed/reviewed/adjudicated case may be audited back to series
    confirmation after an approved Orthanc reimport or role correction, but never while a run is
    active; historical recipes, inputs, results, and adjudications remain immutable.

### Detector status

| Detector | Status | Compute | Harmonization | Browser result |
|---|---|---:|---|---|
| MELD-FCD | Built; site acceptance required | GPU | `meld_distributed_combat` | Frames, derived T1 and SEG |
| MAP | Experimental MAP-inspired implementation | CPU | Optional `map_normative` | Findings, native-T1 SEG, quantitative z-score Parametric Maps |
| HippUnfold (HS) | Exploratory | CPU | Not applicable | Bilateral subfield volumes, asymmetry/atrophy flag, native-T2 SEG |
| qT2 | Pending | — | — | — |
| AID-HS | Pending | — | — | — |

MAP is not claimed to be MAP07-equivalent. HippUnfold's fixed asymmetry threshold is not a
normative age/sex/ICV/site model. Detector metrics are explicitly non-comparable and spatial
concordance is reported as unavailable until at least two eligible detectors emit common spatial
keys. Selector overrides remain an audited administrative action. An assigned case user may instead
confirm an unharmonized processing plan without a profile; unharmonized and selector-override runs
remain visible for research review but are prominently marked and excluded from concordance.
Without a `map_normative` profile, MAP's published threshold and quantitative maps use its
within-subject left/right asymmetry fallback rather than control-cohort normative z-scores.

## Multi-scanner/protocol harmonization

A profile is immutable and specific to detector, scanner/station, software, protocol, source role,
control cohort, processing images, validation evidence, and artifacts. Create a new version after
any change. Active selectors for one detector must be provably disjoint; equal-scoring candidates
require an explicit choice and are never silently selected.

Example selector:

```json
{
  "roles": ["t1_uni"],
  "acquisition": {
    "manufacturer": {"eq": "siemens healthineers"},
    "model": {"eq": "magnetom terra"},
    "field_strength_t": {"target": 7.0, "tolerance": 0.2},
    "protocol_name": {"regex": "mp2rage"},
    "receive_coil_name": {"eq": "site-validated-coil"},
    "acceleration_factor_in_plane": {"target": 3.0, "tolerance": 0.01}
  }
}
```

Profile construction requires at least 20 eligible deidentified controls from one site,
scanner/software version, and protocol, normalized demographic variation, exact build images, and
a signed scientific-validation summary with QC/exclusions and positive/negative/control holdout
evidence. In research/production mode the generic profile create/validate/activate endpoints are
disabled: profiles are installed from the signed expected-active inventory or follow the linked,
audited on-server cohort-build workflow. One authenticated administrator may freeze, build, review,
validate, and activate a candidate; each phase and actor is still retained in the audit history.
Release export/import rejects a profile whose build images differ from the signed detector image
lock, and the worker repeats that comparison before execution.
Readiness periodically re-hashes the complete active artifact closure. Signed release profiles must
still match the expected-active inventory. A locally generated profile is an additional permitted
class only when it is linked to an active on-server build, records its frozen cohort, QC, artifact,
and pinned builder-image hashes, completed validation/activation, and declares
`parameters.storage_scope` as `generated`. Generated profiles additionally bind the SHA-256 of the
reviewed builder adapter across the build row, worker heartbeat, request, QC report, artifact
manifest, scientific-validation report, profile, audit events, and release export. An ad hoc
database profile never satisfies readiness.
For a first-site installation with no profile, export an exactly empty expected inventory with
`--allow-empty-harmonization-bootstrap`. The exporter signs
`MELD7T_HARMONIZATION_COHORT_BOOTSTRAP_ALLOWED=true` into `release.env`; the production installer
propagates that value and does not accept a site-local override. This permits the cohort-builder
control plane before a profile exists. Routine cases may still run only after the user explicitly
confirms an unharmonized plan, which preserves warnings in the queue, Review Study, derived DICOM,
and reports. The next signed release must include the accepted profile and return the authorization
to `false`. An empty-bootstrap
import is rejected once the database contains any signed release-profile history, so it cannot be
used to retire or downgrade an established release inventory.

### On-server MELD cohort builder

The administrative cohort-builder workflow is designed for roughly 20–40 controls per
site/scanner/protocol (20 is the hard minimum, not a scientific guarantee). Controls live in the
separate harmonization Orthanc and never enter the research-case store. Administrators can ingest
them by controlled DICOM transfer/filesystem import or a resumable browser upload, then reconcile a
constrained demographics file keyed by the pseudonymous DICOM PatientID. C-STORE enters Orthanc
quarantine immediately; a completed browser upload first becomes `staged` and reaches quarantine only
after its asynchronous `importing` step succeeds. Neither path creates a cohort member until the
audited admission step selects one MR source series, verifies required geometry, and hashes the
exact Part-10 bytes for every selected SOP
instance. The demographics/cohort subject key must match the PatientID parsed from those bytes.
Scanner/protocol fingerprint inputs and modality are parsed from those same exact bytes;
an earlier Orthanc index response is never their trust root. Admission also requires
`PatientIdentityRemoved=YES`, a declared deidentification method,
`BurnedInAnnotation=NO`, no prohibited direct identifiers, and only exact site-approved private
tags. Duplicate subjects/SOPs, inconsistent acquisitions, protocol outliers, incomplete
demographics, and quota violations fail closed.

Large source hashing is prepared outside a database transaction, then the exact selected byte
closure is re-downloaded and compared while holding the same transaction-scoped mutation fence as
all rollback deletion. Membership and its audit event commit before that fence is released. Thus a
rollback either observes the admitted SOP references and preserves them, or finishes first and
causes admission's final closure check to fail; it cannot leave a manifest pointing at deleted
objects.

Browser ingestion uses an append-only canonical receipt. Its header binds the upload SHA-256,
canonical instance-manifest SHA-256, and instance count; intent records bind SOP UID, byte count,
and file SHA-256; stored records bind the Orthanc instance ID and whether the worker proved
ownership. A canonical digest of that evidence is retained with a rollback failure. The worker
automatically deletes only proven-owned instances. An `AlreadyStored` object is explicitly
pre-existing and is never selected by a later exact-delete approval. An intent-only exact object
whose store response was lost may instead have arrived by C-STORE, so it remains quarantined for
audited resolution. The admin UI verifies and displays the protected receipt before resolution.
`Preserve` requires the digest of the site's external ownership/reference attestation and closes
the gate; `exact delete` requires the canonical receipt digest, no cohort reference or
receipt-integrity failure, and keeps the gate closed until the builder re-verifies it. Any unresolved
rollback globally fences application-controlled upload ingestion, cohort admission, build
admission, and readiness—not just the originating cohort.

The server orchestration is implemented, but DICOM-to-MELD scientific preparation is a
site-accepted adapter rather than guessed application logic. Set
`MELD7T_HARMONIZATION_BUILDER_ADAPTER` and
`MELD7T_HARMONIZATION_BUILDER_ADAPTER_SHA256` together only after that executable passes
golden-cohort validation. Deployment requires a regular non-symlink executable, hashes its actual
bytes, and stamps the same digest into the API environment so admission and the worker agree.
Without both, build admission fails closed while ingestion and cohort preparation remain available.

After the cohort is ready, an administrator explicitly freezes it before submitting a build. The
freeze binds the selected-source Part-10 byte closure, keyed study/subject identities, demographics
hash, selector, and configuration. Build creation also binds its builder image and reviewed adapter
digests. Build admission requires a fresh, release-matching builder heartbeat. The worker re-downloads the frozen
instance closure and compares every byte count/hash
before producing a private snapshot. The digest-bound adapter reads only that snapshot and receives
no Orthanc credentials. A dedicated queue and worker run with explicit CPU/GPU, memory, disk,
timeout, and concurrency limits; the default is one cohort build at a time, and it shares the
fenced GPU lease with case inference. A separate ingestion slot keeps verified uploads moving
without permitting a second live build. Every transition, exclusion, retry, cancellation, artifact,
and approval is audited.

MELD candidates use deterministic five-fold internal cross-validation when the eligible cohort can
support it. Each fold estimates on its training controls and evaluates only its held-out controls.
Every fold must return the configured nonempty, finite metric set and pass its versioned gates.
The final candidate is then fit again using every eligible control and must pass a separate final-fit
metric gate. Cross-validation measures internal stability; it is not independent scientific
validation and does not replace site holdouts, golden cases, or expert review. Successful raw
snapshots/workspaces are removed after QC; failed or interrupted raw workspaces are removed
immediately when safe and never retained beyond the configured 24-hour ceiling unless an atomic
artifact rename has already made publication durable. A `building/publishing` workspace is then
retained beyond the ordinary reaper cutoff, cancellation is refused, and deterministic recovery
must finish the profile/SQL/audit transaction before the build reaches `qc_review`.

Uploads progress through `receiving`, `staged`, `importing`, and `imported`, with `failed` terminal.
Host-level serialization prevents two worker processes from importing or rolling back the same
Orthanc object concurrently, while the second worker slot remains available beside one live build.
Successful uploads expose their StudyUID/pseudonymous-PatientID admission mapping to admins under a
retention setting hard-capped at 24 hours, then redact the plaintext pseudonym on the next
reconciliation cycle. Original workstation filenames and paths are never retained; the server
stores an opaque upload ID and validated format suffix. Upload status/audit errors are bounded
machine codes; uploader-controlled exception text and filesystem paths are not persisted or shown.

Cohorts progress through `draft`, `cohort_ready`, `frozen`, and `archived`. Their builds progress
through `queued`, `building`, `qc_review`, `validated`, and `active`, with terminal `failed` and
`cancelled` outcomes; profiles may later be `retired`. Cohorts and active profiles are immutable.
Adding data or changing a scanner, protocol, selector, method, artifact, or software version creates
a new candidate version. One administrator may initiate, validate, and activate the build; the
separate state transitions and recorded actor/timestamps preserve the audit trail without imposing
multi-person ceremony. MAP will use the same builder contract only after its estimation and
scientific-validation method is implemented; packaging MAP normative-profile artifacts remains an
external governed process. Runtime publication of MAP results as DICOM is implemented separately.

Rejecting a candidate during QC archives that frozen cohort. Correct the data or methodology in a
new cohort and use a new profile version; rejected parameters are never revised or activated.

On-server activation is a local audited promotion, not a modification of the signed release
inventory. Include the generated artifacts, manifests, QC/evidence, and audit records in backup
immediately. For the next release, an administrator/auditor uses
`GET /api/harmonization/builds/{id}/release-export` to obtain the full profile document, exact
expected-inventory entry, and hash-bound artifact copy plan. Copy those bytes into release staging
and sign them through the normal offline release workflow; the endpoint response is preparation,
not a trust root. The only permitted document change is
`parameters.storage_scope: generated → release`. During installation, the signed profile importer
recognizes that exact transformation and promotes the existing active database row; any other
document or artifact change fails closed.

Availability is release-specific. An installed release supports this workflow only when its
cohort API/UI, dedicated builder worker/queue, and harmonization Orthanc Quadlets are all present
and have passed site acceptance. Until then, use the controlled offline CLI procedure documented
below; do not approximate the storage boundary with labels in the research Orthanc.

See [ops/harmonization/README.md](ops/harmonization/README.md) for exact MELD and MAP commands,
validation-report schema, inventory generation, verification, release, and assignment procedures.
Every release must retain artifacts needed by active profiles and same-release retryable historical
runs. Retire a profile before removing its assets.

## Deployment branding, appearance, and report storage

Branding is deployment-wide. The checked defaults identify the installation as **MELD 7T** for
**Houston Methodist** and the **Houston Methodist Research Institute**; the SPA and each newly
created combined-report snapshot read the same validated API settings:

| Setting | Purpose |
|---|---|
| `MELD7T_BRANDING_PRODUCT_NAME` | Application/report product name |
| `MELD7T_BRANDING_INSTITUTION_NAME` | Deployment institution |
| `MELD7T_BRANDING_DEPARTMENT_NAME` | Research institute or department |
| `MELD7T_BRANDING_PRIMARY_COLOR`, `MELD7T_BRANDING_SECONDARY_COLOR` | Six-digit web/PDF accent colors |
| `MELD7T_BRANDING_FOOTER_TEXT` | Combined-report and application footer |
| `MELD7T_BRANDING_LOGO_URL` | Same-origin browser asset; defaults to `/branding/report-logo.png` |
| `MELD7T_BRANDING_LOGO_PATH` | Absolute PNG/JPEG path for reports; blank selects text-only reports |

Branding is captured inside each immutable report snapshot. Changing deployment branding affects
the application after restart and future report versions; it does not rewrite an existing PDF or
its recorded hash. Human-facing PDFs identify the research case by pseudonym and intentionally omit
Study/Series Instance UIDs, internal case/recipe database IDs, and filesystem paths; those remain in
the frozen database evidence, while non-PHI integrity and release hashes remain visible. Reviewer
identity and reviewer-entered notes are reproduced in final reports, so notes must contain only
content approved for that report. Configured report-logo bytes are hash-bound to the snapshot.

The signed release includes the selected Houston Methodist
[Leading Medicine four-color PNG](https://www.houstonmethodist.org/-/media/files/marketing/brand/logos/hospital_and_system_logos/leading_medicine/4c/methodist_leading_medicine_4c_png.ashx?mw=1382&hash=6867B22F0D3D16FED9AB9575C268BC02)
at `containers/config/branding/report-logo.png`. Production installs the same bytes at
`/branding/report-logo.png` for the browser and `/run/branding/report-logo.png` for API report
snapshots; the host PDF worker receives the matching installed path. No manual logo staging is
needed for the default deployment. A white-label deployment can override it with a service-user-owned
`~/meld7t-secrets/production/branding/report-logo.png` before running the production play. Text-only
report branding remains available by omitting `MELD7T_BRANDING_LOGO_PATH`. Production preflight
fully decodes the effective asset with the pinned worker Pillow and accepts one-frame PNG/JPEG files
up to 5 MiB, 8192 pixels per dimension, and 4 million pixels total. Provenance, dimensions, and the
retrieved-file SHA-256 are recorded in
[containers/config/branding/README.md](containers/config/branding/README.md).

The interface supports `system`, `light`, and `dark` appearances. `system` follows the managed
browser/OS preference; a user's explicit choice is stored only in that browser's local storage.
The shared responsive design system applies the same navigation, panels, tables, status/warning
tokens, and accessible focus treatment across intake, planning, queues, and Review Study. Branding
is constant across themes, and operators should acceptance-test the chosen colors/logo for contrast
at supported desktop/tablet widths, in both modes, and in a printed PDF.

Routine upload staging is local active state at `~/meld7t-state/case-uploads` (the API container sees
`/data/case-uploads`). `MELD7T_CASE_UPLOAD_MAX_BYTES`, `MELD7T_CASE_UPLOAD_QUOTA_BYTES`,
`MELD7T_CASE_UPLOAD_CHUNK_BYTES`, and `MELD7T_CASE_UPLOAD_EXPIRY_HOURS` control API admission;
the worker's `MELD7T_CASE_UPLOAD_MAX_FILES`, `MELD7T_CASE_UPLOAD_MAX_EXPANDED_BYTES`, and
`MELD7T_CASE_UPLOAD_MAX_INSTANCE_BYTES` bound archive expansion. API and worker roots/size ceilings
must describe the same host storage. Production installation aggregates the entire routine upload
quota, concurrent extraction/compute workspaces, and Research Orthanc source duplication with the
harmonization reserves on their actual physical filesystems; it refuses activation when free space
does not cover the configured worst case. Both Orthanc stores have separately configured hard caps
and must use `Reject` mode; production validation forbids automatic recycling, and the health timer
warns before either store reaches its cap. Uploads progress through
`receiving → staged → importing → ready` (or terminal `failed`), and successful archives and
receipts are removed; the authoritative source instances remain in Research Orthanc.
Abandoned `receiving` sessions expire after the configured interval. Ordinary failed imports remove
only Orthanc instances proven to have been created by that upload and then clear their staging
files. A `rollback_incomplete` failure is different: its archive and append-only receipt are retained
as reconciliation evidence and are not reaped as ordinary terminal staging. Do not delete that
evidence manually or treat the upload queue as drained until the exact Orthanc objects and receipt
have been reconciled under the site's incident procedure. Routine staging is transient and not a
substitute for Research Orthanc retention or backup.
`MELD7T_CASE_REPORT_TIMEOUT_MINUTES` bounds recovery of a report left in `generating`. Generated
combined PDFs live below
`~/meld7t-state/meld-data/reports/` and are included with `meld-data` in encrypted backups. Database
rows retain each upload transition and each report's immutable snapshot, version, status, artifact
manifest, and audit events. Routine archives never enter Harmonization Orthanc; cohort-control
uploads continue to use its separate staging root, credentials, queue, quota, and retention policy.

## Development verification

Inside `meld-dev`:

```bash
cd platform/api
uv sync --locked --extra dev
uv run --locked pytest

cd ../worker
uv sync --locked --extra dev
uv run --locked pytest

cd ../../containers/pkg
python3 -m unittest discover -s tests -v

cd ../../ops/release
python3 -m unittest discover -s tests -v

cd ../../platform/web
npm ci --ignore-scripts --no-audit --no-fund
npm test
npm run build
```

`just e2e [map|hippunfold|meld_fcd]` is a live-stack, real-detector test and is intentionally
skipped in the service-free unit suite. It requires approved deidentified DICOM, local services,
licenses, signed assets, and the exact detector images. It is an engineering test, not the site
scientific acceptance suite.

## GitHub developer releases

[The developer release workflow](.github/workflows/developer-release.yml) runs a read-only package
preflight for pull requests and pushes to `main`. Pushing a new stable SemVer tag such as `v0.2.0`
runs that same path and enables publication. A release accepts only an exact tagged commit reachable
from `origin/main`, checks that API, worker, web, and lock metadata all equal the tag version, rejects
tracked secret/database/medical-image paths, and runs the complete service-free test suite. It then
publishes a GitHub prerelease containing:

- the exact committed source archive;
- API and worker wheels and source distributions;
- the static web distribution with its source commit marker;
- deterministic metadata, a scope/license notice, and `SHA256SUMS`.

Every action is pinned by full commit SHA. Separate jobs attest with no repository-write permission
and publish with no identity-token/attestation permission. The publishing job receives only the
downloaded build artifact—not a source checkout—rechecks that the tag still resolves to the event
commit, refuses to replace an existing release, and creates a draft first. It compares every uploaded
asset's GitHub SHA-256 digest before making that draft public. Configure a repository ruleset that
prevents update/deletion of `v*` tags and protect the `developer-release` environment before issuing
the first tag.

After merging a versioned change to `main`, create the tag with the repository owner's configured
SSH or GPG signing key and push it once:

```bash
git switch main
git pull --ff-only
git tag -s v0.2.0 -m "MELD7T 0.2.0 research developer packages"
git push origin v0.2.0
```

Do not move or reuse a release tag. The local packager also requires `vVERSION` to resolve exactly to
the clean checked-out commit. To exercise it inside `meld-dev` after creating that local tag:

```bash
just developer-release-check 0.2.0
just developer-release-kit 0.2.0 /tmp/meld7t-developer-release-0.2.0
```

Verify downloaded assets with `sha256sum --check SHA256SUMS` and
`gh attestation verify <asset> --repo <owner/repository>`. These are developer component packages,
not the complete Bazzite/OCI/model release and not a production authorization. The repository does
not currently contain a repository-wide `LICENSE`; publishing a source archive does not imply a
license grant. Select and add an appropriate license before inviting redistribution or external
contributions.

## Signed air-gap release

On a connected release workstation, inside `meld-dev`, start from a clean reviewed commit:

```bash
just api-wheelhouse "$HOME/meld7t-release-input/api"
just worker-wheelhouse "$HOME/meld7t-release-input/worker"
just web-build
just api-build
just pkg-build
just release-lock-check runtime
```

Replace the API/pkg `REQUIRED_REBUILD_CURRENT_SOURCE` entries in
[containers/images.lock](containers/images.lock) with repository manifest digests. Ensure all
runtime images are present by exact digest, the HippUnfold cache is complete, harmonization contains
`profiles/`, artifacts, and `expected-active-profiles.json`, and the required release attestations
exist:

- `approval.txt`
- `sbom.spdx.json`
- `vulnerability-report.json`
- `vulnerability-exceptions.txt` with approver, rationale, and future expiry
- `license-report.json`
- `golden-case-evidence.txt`

Export and independently verify:

```bash
just release-export \
  --output /media/meld7t-0.2.0 \
  --release-id 0.2.0 \
  --signing-key /secure/release-signing.pem \
  --attestations /release/attestations \
  --web-dist platform/web/dist \
  --api-artifacts "$HOME/meld7t-release-input/api" \
  --worker-artifacts "$HOME/meld7t-release-input/worker" \
  --harmonization /release/harmonization

just release-verify /media/meld7t-0.2.0 /trusted/release-signing-public.pem
```

Do not add an extra `--` before exporter arguments; `just` forwards it literally. The private
release key never enters the bundle or production host. Transfer the public key independently.

## Bazzite production installation

Read [ops/deployment/PRODUCTION_PREREQUISITES.md](ops/deployment/PRODUCTION_PREREQUISITES.md)
completely. Provision the split mode-`0400`/`0600` inputs under
`~/meld7t-secrets/production` using [containers/config/production](containers/config/production).
Use an encrypted Ansible inventory/vault for site variables.

Verify and stage without activation:

```bash
just release-verify /media/meld7t-0.2.0 /trusted/release-signing-public.pem
just release-import \
  --bundle /media/meld7t-0.2.0 \
  --trusted-key /trusted/release-signing-public.pem

cd "$HOME/.local/lib/meld7t/staged"
just prod-install \
  -e allowed_client_cidr=10.20.30.0/24 \
  -e nas_host=research-nas.example \
  -e nas_export=/exports/meld7t \
  -e approved_os_checksum=<64-hex-booted-ostree-checksum> \
  -e host_change_control_id=<change-id> \
  -e golden_case_acceptance_id=<acceptance-id> \
  -e nas_encryption_attestation_id=<attestation-id>
```

First installation only:

```bash
just prod-state-init 0.2.0

STAGED="$(readlink -f "$HOME/.local/lib/meld7t/staged")"
just backup \
  /var/mnt/meld7t/backups \
  /trusted/backup-recipient.crt \
  /secure/backup-signing.pem \
  "$STAGED"

just prod-migrate \
  "$STAGED" \
  /var/mnt/meld7t/backups/meld7t-backup-<timestamp> \
  /trusted/backup-signing-public.pem

just prod-activate 0.2.0
```

The controlled migration verifies a fresh signed/encrypted backup and runs Alembic once. For an
upgrade from the preceding schema, this creates the routine `case_uploads` state and immutable
`case_reports` state before importing signed expected harmonization profiles. Do not start the new
API/worker against an unmigrated database, and do not run Alembic automatically at application
startup. Activation switches release/config/Quadlet symlinks,
starts the full stack, checks container health, the release-bound worker heartbeat, cached profile
integrity, storage capacity, and `/readyz`, and restores prior symlinks on failure. It never reverses
a database migration.

Before accepting service, run deidentified golden and negative cases for every supported
scanner/protocol, inspect SEG geometry in the exact OHIF build, verify the audit ledger, take a new
backup, run `just restore-drill`, and perform a complete isolated-host restore with measured RPO/RTO.

For upgrades: pause and drain the queue, import/install the staged release, take and verify a fresh
backup, migrate, then activate. Retained N-1 code must remain schema-compatible or rollback requires
the documented database restore.

## Operations

- `meld7t-health.timer` runs [ops/deployment/healthcheck.sh](ops/deployment/healthcheck.sh) each
  minute. Forward its JSON failure result and persistent user journal to hospital monitoring. It
  alerts when harmonization Orthanc reaches 85% of its independent hard quota; tune the explicit
  `MELD7T_HARMONIZATION_ORTHANC_MAX_USED_PERCENT` service setting only with an approved retention
  and capacity plan.
- Alert on service/readiness, routine upload/report/case and harmonization-builder queue health,
  worker heartbeats, release mismatch, backup age, local/NFS capacity, PostgreSQL, both Orthanc
  stores, immudb, Redis, GPU/CDI, certificate expiry, clock drift, UPS, and RAID.
- Backup format 3 is streaming, encrypted, signed, and includes databases, both Orthanc stores,
  immutable harmonization build evidence/artifacts, immudb server/client trust state, Redis,
  configuration/TLS/secrets, model cache, and results. `restore-drill` validates
  crypto/catalog/archive readability; it is not a full application restore. Migration remains
  able to consume format 2 backups during upgrades, but new backups use format 3.
- Automatic Bazzite updates remain disabled. Every approved offline OS/NVIDIA/firmware update
  requires reboot, approved ostree checksum, regenerated CDI, `just gpu-check`, readiness, and
  golden-case reacceptance.
- Define site policies for retention/legal holds, audited purge, incident response, key rotation,
  removable media, log privacy, and backup immutability. The application currently prevents new
  work at a disk watermark but does not implement a complete evidence-safe purge lifecycle.

## Known blockers and residual risks

At this repository snapshot, do not claim production acceptance until these are resolved or
formally accepted with an owner and expiry:

- API/pkg image locks may still contain `REQUIRED_REBUILD_CURRENT_SOURCE`; no release exports until
  both are rebuilt from the final clean commit and pinned by manifest digest.
- Real-detector site golden cases, negative controls, scientific holdouts, protocol suitability/QC,
  MP2RAGE scaling/background behavior, MELD profile A/B behavior, and human SEG geometry review are
  external acceptance evidence. Engineering checks are not scientific validation.
- The queue, cohort, QC, and publication orchestration is implemented, but this repository does not
  supply the site-specific DICOM-to-MELD harmonization adapter. Build admission remains fail-closed
  until a reviewed absolute executable and SHA-256 pass the site's golden-cohort acceptance.
- MELD invocation proves the requested code/mount, but upstream output does not yet prove internally
  that it consumed those exact ComBat bytes. MELD classifier/model assets need a proven signed
  offline packaging and preflight path.
- MAP remains experimental. Its native-space DICOM SEG and Parametric Maps require site golden-case
  review of the SPM inverse-deformation geometry; generation and validity of normative statistics
  remain governed external methods. HippUnfold subfield/asymmetry DICOM is exploratory and excluded
  from cross-site concordance without a validated normative interface.
- Enhanced multiframe DICOM may carry relevant frame-level acquisition values not exposed by
  series-level QIDO; every supported scanner/software/protocol needs a golden acquisition-contract
  test.
- PostgreSQL, Orthanc, and immudb cannot share one atomic transaction. Deterministic UIDs,
  per-SOP manifests, WADO verification, fences, and reconciliation reduce risk, but a crash can
  still leave an orphaned immudb event or derived Orthanc study. A durable DICOM publication/purge
  saga remains required for unattended long-term operation.
- Runtime storage admission does not yet use one persistent reservation ledger across the routine
  worker and harmonization queues. Aggregate installation reserves, per-job watermarks, both
  Orthanc `Reject` caps, and 85% alarms fail safely for declared workloads, but retained artifacts
  and out-of-band growth still require host monitoring and an approved capacity/retention response.
- The live E2E test is opt-in and this repository cannot supply the hospital's deidentified corpus,
  formal DICOM validator, viewer review, fresh-install/upgrade/restore matrix, or measured DR drill.
- `python-ecdsa` has an unresolved timing advisory and `rsa` is archived transitively through
  `immudb-py`. Current use is public verification, but deployment requires a scoped expiring waiver
  or replacement SDK.
- Caddy Basic Auth is not IAM/MFA. Research Orthanc read authorization is intentionally cohort-wide
  for every authenticated application identity and must therefore contain only approved research
  cases; harmonization Orthanc remains restricted to administrators and the builder service.
- Loopback services, release secrets, and rootless Podman share one Unix-account trust boundary.
  Run no unrelated process as that account; a stronger multi-account boundary needs separate design.
- Full-disk encryption, outbound-deny validation, NFS/NAS encryption, firewall overlap tests,
  immutable retention, complete purge, penetration testing, and incident response remain site
  controls.
- The pkg image still uses rolling OS packages/direct Python installs; its signed SBOM, scan,
  license review, and acceptance are mandatory until the build is snapshot/hash reproducible.

These disclosures are intentional: a green automated preflight means the declared engineering
contracts passed, not that the platform is clinically, scientifically, legally, or operationally
approved.
