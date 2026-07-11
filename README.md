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
| Host worker | ARQ consumer that launches digest-pinned sibling containers; two slots, one Redis-fenced GPU job |
| PostgreSQL | Authoritative workflow, provenance, result, and transactional audit mirror |
| Redis | Password-protected persistent broker, status cache, worker heartbeat, and GPU lease |
| Orthanc | Dedicated research-cohort DICOM store; never a general clinical PACS |
| immudb | Independently verified append-only audit copy with persistent client trust roots |

The production Quadlets separate `meld-edge`, `meld-data-net`, `meld-net`, and
`meld-registry-net`. Only Caddy publishes the browser ports. PostgreSQL, Redis, Orthanc, immudb,
and the optional registry also bind loopback ports because the host worker needs them. Orthanc
uses a separate internal credential; browser identities never receive it.

Active compute state lives on encrypted local NVMe under `~/meld7t-state`. Durable import and
backup storage lives under `/var/mnt/meld7t`; production NFS defaults to `sec=krb5p`. The local
registry is an optional cache—the signed offline bundle is release authority. The API and worker
share a release-bound HMAC worker heartbeat, and readiness rejects a missing, stale, mismatched, or
capacity-exhausted consumer. The default admission contract reserves 300 GiB free for two jobs
(`50 GiB` floor plus `100 GiB` DICOM and `25 GiB` output headroom per slot); tune those explicit
limits only from measured site workloads.

Roles are `submitter`, `reviewer`, `admin`, `auditor`, and `service`. Server case intake is
admin/service-led and may be assigned to a named submitter. Reviewer access is read-only except for
append-only adjudication. Full profile inventory and selector details are admin/auditor-only.
Static Caddy Basic Auth is a closed-network fallback, not institutional MFA/SSO.

## Research workflow

1. Import an approved research study into the dedicated Orthanc cohort.
2. An admin/service identity creates a case for the exact Study Instance UID and assigns it.
3. QIDO discovers the current series set. A researcher explicitly confirms every series role.
4. Minimized but still protected acquisition metadata creates a site-keyed, schema-versioned
   scanner/protocol fingerprint. Coil, bandwidth, acceleration, matrix, phase-encoding,
   reconstruction, software, geometry, and pixel-scaling fields are included when present.
5. The researcher explicitly assigns an immutable, active harmonization profile to every current
   MELD/MAP detector/source target. The case remains partial until coverage is complete.
6. A versioned recipe binds exact source/companion Series Instance UIDs, fingerprints, profile
   document hashes, parameters, and a stable input-contract hash.
7. A durable outbox and claim leases deliver work idempotently. Claim acceptance binds the signed
   release, runtime images, deadlines, and input contract. Before scientific execution or external
   publication, the worker re-hashes the selected profile, detector code/assets, and model cache.
8. The worker retrieves only contracted SOPs, checks study/patient/count/acquisition identity and
   hashes, then atomically publishes a closed local staging tree.
9. Digest-pinned detector containers run with no network. GPU ownership is token-fenced; process
   groups are terminated on timeout/cancellation, and one whole-run deadline covers all stages.
10. Scientific output schemas and finite values are validated before publication. Completion
    commits result, clusters, provenance, output hashes, completion-bundle hash, and audit event
    together.
11. MELD creates derived MR plus DICOM-SEG with deterministic UIDs. STOW responses, QIDO identities,
    WADO pixel/geometry semantics, and a retained per-SOP hash manifest are checked.
12. Reviewers append adjudications. Corrections link to and supersede an earlier record; no review
    is overwritten. A terminal failed/reviewed/adjudicated case may be audited back to series
    confirmation after an approved Orthanc reimport or role correction, but never while a run is
    active; historical recipes, inputs, results, and adjudications remain immutable.

### Detector status

| Detector | Status | Compute | Harmonization | Browser result |
|---|---|---:|---|---|
| MELD-FCD | Built; site acceptance required | GPU | `meld_distributed_combat` | Report, frames, derived T1 and SEG |
| MAP | Experimental MAP-inspired implementation | CPU | `map_normative` | Findings; no DICOM overlay |
| HippUnfold | Exploratory | CPU | No validated interface yet | Subfield volumes/asymmetry; no overlay |
| qT2 | Pending | — | — | — |
| AID-HS | Pending | — | — | — |

MAP is not claimed to be MAP07-equivalent. HippUnfold's fixed asymmetry threshold is not a
normative age/sex/ICV/site model. Detector metrics are explicitly non-comparable and spatial
concordance is reported as unavailable until at least two eligible detectors emit common spatial
keys. Selector overrides and unharmonized server runs are admin-only, require a reason, and are
excluded from concordance.

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

Profile construction requires at least 20 controls, normalized demographic variation, a
release-workstation-only cohort HMAC key, exact build images, and a signed scientific-validation
summary with QC/exclusions and positive/negative/control holdout evidence. One administrator may
validate a draft; a different administrator activates it. Production migration can perform the
equivalent controlled activation only for documents named by the signed expected-active inventory.
Release export/import rejects a profile whose build images differ from the signed detector image
lock, and the worker repeats that comparison before execution.
Readiness periodically re-hashes the complete active artifact closure and fails when the cached
scan is stale or differs from that inventory.

See [ops/harmonization/README.md](ops/harmonization/README.md) for exact MELD and MAP commands,
validation-report schema, inventory generation, verification, release, and assignment procedures.
Every release must retain artifacts needed by active profiles and same-release retryable historical
runs. Retire a profile before removing its assets.

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

cd ../../platform/web
npm ci --ignore-scripts --no-audit --no-fund
npm run build
```

`just e2e [map|hippunfold|meld_fcd]` is a live-stack, real-detector test and is intentionally
skipped in the service-free unit suite. It requires approved deidentified DICOM, local services,
licenses, signed assets, and the exact detector images. It is an engineering test, not the site
scientific acceptance suite.

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

The controlled migration verifies a fresh signed/encrypted backup, runs Alembic once, and imports
only signed expected harmonization profiles. Activation switches release/config/Quadlet symlinks,
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
  minute. Forward its JSON failure result and persistent user journal to hospital monitoring.
- Alert on service/readiness, worker heartbeat/release mismatch, backup age, local/NFS capacity,
  PostgreSQL, Orthanc, immudb, Redis, GPU/CDI, certificate expiry, clock drift, UPS, and RAID.
- Backups are streaming, encrypted, signed, and include databases, Orthanc, immudb server/client
  trust state, Redis, configuration/TLS/secrets, model cache, and results. `restore-drill` validates
  crypto/catalog/archive readability; it is not a full application restore.
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
- MELD invocation proves the requested code/mount, but upstream output does not yet prove internally
  that it consumed those exact ComBat bytes. MELD classifier/model assets need a proven signed
  offline packaging and preflight path.
- MAP remains experimental and lacks a retained independently reviewable DICOM overlay. Generation
  and validity of normative statistics are governed external methods. HippUnfold asymmetry is
  exploratory and excluded from cross-site concordance without a validated normative interface.
- Enhanced multiframe DICOM may carry relevant frame-level acquisition values not exposed by
  series-level QIDO; every supported scanner/software/protocol needs a golden acquisition-contract
  test.
- PostgreSQL, Orthanc, and immudb cannot share one atomic transaction. Deterministic UIDs,
  per-SOP manifests, WADO verification, fences, and reconciliation reduce risk, but a crash can
  still leave an orphaned immudb event or derived Orthanc study. A durable DICOM publication/purge
  saga remains required for unattended long-term operation.
- The live E2E test is opt-in and this repository cannot supply the hospital's deidentified corpus,
  formal DICOM validator, viewer review, fresh-install/upgrade/restore matrix, or measured DR drill.
- `python-ecdsa` has an unresolved timing advisory and `rsa` is archived transitively through
  `immudb-py`. Current use is public verification, but deployment requires a scoped expiring waiver
  or replacement SDK.
- Caddy Basic Auth is not IAM/MFA and DICOM authorization is cohort-wide for reviewers/admins.
  Orthanc must contain only the approved research cohort.
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
