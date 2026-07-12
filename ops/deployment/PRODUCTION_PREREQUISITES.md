# Production deployment prerequisites and acceptance gates

This path targets a dedicated Bazzite host and performs no network downloads. A hospital activation
is blocked until the following site-owned inputs and approvals exist.

## Release intake

- Commit all source changes, regenerate and review both `platform/api/uv.lock` and
  `platform/worker/uv.lock`, build wheel-only hash-locked wheelhouses, then rebuild the API and pkg
  images. Replace both `REQUIRED_REBUILD_CURRENT_SOURCE` entries in `containers/images.lock` with
  their repository manifest digests.
- Supply non-empty `approval.txt`, `sbom.spdx.json`, `vulnerability-report.json`,
  `vulnerability-exceptions.txt`, `license-report.json`, and `golden-case-evidence.txt` release
  attestations. The exception file requires `APPROVED_BY`, `RATIONALE`, and an unexpired `EXPIRES`
  date. This is where the no-fix `immudb-py` transitive `python-ecdsa`/`rsa` findings and their narrow
  verification-only exposure must remain explicitly accepted until those dependencies are replaced.
  Export signs `SHA256SUMS`; provision that public verification key separately.
- Provide the populated HippUnfold cache and a versioned harmonization root. Every
  `profiles/*.json` is rechecked against every artifact before signing, after transfer, and during
  production installation. Export and installation reject profiles whose build images differ from
  the signed runtime image lock. A first-install release may carry an exactly empty `[]` inventory
  only when export used the explicit empty-bootstrap flag; that authorization is signed into the
  release and cannot be enabled locally. Import also refuses an empty bootstrap after any signed
  release-profile history exists in the database.
- The production server imports OCI archives directly. The local registry is an optional cache and
  is never the release authority.

## Host acceptance

- Provision reviewed offline builds of `ansible-core` and `just` before the host enters the
  air-gap; the production play performs no package installation. Bazzite must already provide the
  remaining host commands checked by `ansible/production.yml`, including Podman/Quadlet,
  Python 3.13, OpenSSL, firewalld, and NVIDIA CDI tooling.
- The hardened compute network is named `meld-compute-net`; it does not reuse the legacy
  `meld-net` topology. After activation and acceptance, remove an unattached legacy `meld-net`
  through normal change control. The installer refuses same-name subnet mutation and any overlap
  with another existing rootless Podman network.
- Run the play as the dedicated, noninteractive `meld7t-svc` account (or the explicitly configured
  `required_service_user`) and use stable `~/meld7t-state`. The play rejects root/interactive
  accounts, NFS/CIFS active staging, or local filesystems without a LUKS/dm-crypt ancestor. The
  play computes routine-worker, routine DICOM ZIP staging, and harmonization reserves from the
  supplied limits. Routine upload staging defaults to `~/meld7t-state/case-uploads`; the API and
  worker limits must agree, and successful imports are removed after their exact instances and case
  metadata commit to Research Orthanc/PostgreSQL. Combined reports are durable under
  `~/meld7t-state/meld-data/reports` and therefore travel with the encrypted `meld-data` backup.
  With the checked-in example ceilings, the aggregate check requires about 2.81 TiB on a separate
  state filesystem and 4.10 TiB on the Podman/Orthanc filesystem; when they share a filesystem, it
  requires about 6.81 TiB. This includes the 500 GiB routine ZIP quota, two concurrent archive
  expansions or detector workspaces, the 500 GiB harmonization upload/expansion plus 1 TiB build
  ceilings, and both example 2 TiB Orthanc hard caps. Lower the declared ceilings to the reviewed
  20–40-study workload if the host is intentionally smaller; runtime admission still fails closed.
  Durable NFS defaults to `sec=krb5p`; `sec=sys` requires explicit risk acceptance.
  NAS encryption at rest remains site-controlled and requires a recorded
  `nas_encryption_attestation_id`.
- The API Quadlet deliberately uses `UserNS=keep-id:uid=10001,gid=10001`: the image remains
  non-root while its UID maps to the service account that owns mode-0700 results, upload, generated
  profile, audit-root, and trust-key paths shared with the host workers. Preserve that mapping in
  any site override; do not compensate with world-readable permissions or subuid ownership.
- Record an approved booted Bazzite ostree checksum in encrypted inventory. The play compares it to
  the running deployment and rejects drift. Configure an internal hospital time source, UPS,
  storage monitoring, and persistent user-journal forwarding/retention.
- Bazzite automatic update timers must remain disabled. OS, Podman, NVIDIA, or firmware updates are
  offline institution-controlled change events: stage the approved ostree/NVIDIA media, update and
  reboot, regenerate CDI, run the locked GPU test, compare the new booted checksum to the newly
  approved value, run service/readiness checks, and reaccept the site golden DICOM case before
  restoring research-service availability. Record the change-control and golden-case acceptance IDs passed
  to `ansible/production.yml`.
- Provision the institutional TLS certificate/key and a constrained source CIDR (not either `/0`).
  The play reconciles stale MELD-zone sources/ports and rejects 9443/9444 in other zones. Independently
  test inbound denial and the worker's `IPAddressDeny=any` policy (localhost plus `10.89.30.0/24`)
  with the real package/STOW golden case before acceptance.

## Identity, study access, and privacy boundary

- The stock shared Caddy accounts are a bring-up fallback only. Hospital activation requires
  unique per-person institutional identities, at least one institutional administrator, explicit
  role rows, and an IAM/security approval record. One administrator
  may freeze/start, validate, and activate a harmonization build; the application retains each
  action, actor, timestamp, evidence hash, and outcome in both audit stores. Every authenticated
  browser identity receives read-only DICOMweb/OHIF access and may review all approved Research
  Orthanc studies. Browser writes remain denied and OHIF study-list browsing remains disabled.
- Caddy strips caller identity headers and supplies authenticated identity, roles, and a 32+ character
  proxy secret to the API over the isolated edge network. The API/data networks use separate Podman
  subnets. Redis requires a distinct password and no-eviction persistence policy.
- Browser DICOMweb accepts only GET/HEAD/OPTIONS, adds `private, no-store`, and passes a separate
  internal service credential to Orthanc. Orthanc also authenticates API/worker loopback access;
  browser identities never receive that credential. Do not run unrelated processes as the service
  account because that one UID can still read release secrets and control rootless Podman.
- Every authenticated identity may list and download existing combined case-report versions.
  Reviewer/admin identities may request or retry a version; detector-native PDFs are retained only
  as internal scientific evidence and are not exposed through the browser.
- Supply separate application and harmonization Postgres/Orthanc secrets, plus Redis, immudb,
  proxy, audit-HMAC, backup-recipient, and release-signing secrets. API and workers share only the
  runtime values they require. Create a restricted
  immudb runtime principal after first boot instead of retaining the bootstrap administrator. Set
  `IMMUDB_AUTH=true`, `IMMUDB_DEVMODE=false`, and `IMMUDB_MAINTENANCE=false`. Provision an EC signing
  private key as the rootless Podman secret and pin its public key. API and worker use distinct
  persistent root-state files (`api-immudb-state` and `~/meld7t-state/audit/worker.root`).
- Routine browser intake is available to `submitter` and `admin` roles. It accepts one patient and
  one Study Instance UID per resumable DICOM ZIP, queues bounded validation/import, and never
  confirms a series mapping or starts a detector. The creator/assignee (or an administrator) must
  review the proposed series roles, exact detector inputs, and harmonization status before queuing
  the processing plan. Every authenticated identity may then open the case-level Review Study.
- Configure reviewed `ORTHANC__MAXIMUM_STORAGE_SIZE` values for both Research and harmonization
  Orthanc. Production validation requires `MaximumStorageMode=Reject`: neither store may recycle
  older research studies to make room. The local health timer warns at 85% of each independent cap
  by default, in addition to its physical-filesystem threshold; operators must expand capacity or
  apply the approved retention policy before Reject mode begins refusing new uploads.
- `receiving` routine uploads expire according to `MELD7T_CASE_UPLOAD_EXPIRY_HOURS`. A successful
  import or an ordinary fully rolled-back failure removes its archive/receipt; its database/audit
  history remains. If exact deletion of worker-owned partial Orthanc objects cannot be proved or
  completed, the upload remains `failed` with phase `rollback_incomplete`, and the archive/receipt
  are deliberately retained as incident evidence. Do not manually remove them, admit them as a
  case, or call the upload queue drained until the site has reconciled the exact Orthanc objects and
  receipt. Source DICOM for a ready case follows the Research Orthanc retention/backup policy, not
  the transient upload-staging policy.
- The production input directory must contain mode-0400/0600 files owned by the service account:
  the ten split env files, Redis config, TLS pair, two licenses, Caddy
  users/roles maps plus identity approval, and `immudb-signing-private.pem`,
  `immudb-signing-public.pem`,
  `backup-signing-public.pem`, and `release-signing-public.pem`.
- When enabling on-server MELD estimation, install the site-accepted builder adapter as an absolute,
  regular, non-symlink executable readable by the service account. Configure its lowercase SHA-256
  beside the path in `harmonization-builder.env`; Ansible hashes the file and copies that digest into
  `api.env`. A missing adapter leaves cohort ingestion/preparation available but build admission
  closed.

## Deployment-wide branding and appearance

- The production example defaults to product `MELD 7T`, institution `Houston Methodist`, and
  department/footer `Houston Methodist Research Institute`. Review and set the
  `MELD7T_BRANDING_*` values in `api.env` before installation. The same validated snapshot is used by
  the SPA and every new combined preliminary/final report version. A branding change affects future
  versions only; existing PDFs and their hash-bound branding snapshots remain immutable.
- The signed release ships the selected Houston Methodist
  [Leading Medicine four-color PNG](https://www.houstonmethodist.org/-/media/files/marketing/brand/logos/hospital_and_system_logos/leading_medicine/4c/methodist_leading_medicine_4c_png.ashx?mw=1382&hash=6867B22F0D3D16FED9AB9575C268BC02)
  and installs it automatically at `branding/report-logo.png`. The configured browser URL is
  `/branding/report-logo.png`, the API container path is `/run/branding/report-logo.png`, and the
  production play stamps the corresponding absolute host path into the worker environment.
  Provenance and the expected SHA-256 are recorded beside the asset.
- A white-label deployment may put replacement files in the service-user-owned
  `~/meld7t-secrets/production/branding/` directory; those files overlay the signed default without
  rebuilding the SPA. Omit the API report-logo path for deliberate text-only report branding. The
  effective report file must be a fully decodable one-frame PNG/JPEG no larger than 5 MiB, 8192
  pixels on either axis, or 4 million pixels total. Preflight also rejects links,
  special/hardlinked files, foreign owners, and group/world-writable entries anywhere in the served
  branding tree. A missing browser-only override falls back to the bundled mark and text identity.
- Acceptance-test the deployment colors/logo in the SPA's `system`, `light`, and `dark` modes and in
  a printed combined PDF. Theme preference is per browser (local storage); it is not a server-side
  user record and does not alter report rendering.

## First installation

1. Transfer the independently trusted release public key and signed release. Run
   `verify-airgap.sh`, then `import-airgap.sh`. Import stages code, attestations, wheelhouses, and
   runtime provenance atomically. HippUnfold import uses a temporary volume, verifies the signed
   per-file closure before and after installation, and writes its completion marker only after the
   cache is fully copied. The worker verifies that complete closure again before each HippUnfold run.
2. Place split secret/TLS/identity inputs under `~/meld7t-secrets/production` and run
   `ansible/production.yml` with the approved OS checksum, source CIDR, NAS settings, change-control
   ID, golden-case ID, and NAS-encryption attestation. The play stages release-specific config,
   Quadlets, user units, worker venv, and acceptance receipt without changing live symlinks.
3. On a new host only, run `initialize-first-install.sh RELEASE --confirm-new-host`; it exposes and
   starts only the application and harmonization PostgreSQL/Orthanc services, Redis, and immudb.
   Interactively use `immuadmin` as the bootstrap
   administrator to create the exact non-admin/readwrite runtime principal named in API/worker envs.
   Never place the administrator credential in either runtime env.
4. Take an encrypted signed baseline backup. `migrate.sh STAGED_RELEASE BACKUP_DIR TRUSTED_KEY`
   verifies the signature, checks the signed timestamp (not mutable mtime), host, and current-release
   binding, and rejects a backup outside the 24-hour window. The release's one-shot Alembic chain
   creates routine upload and versioned combined-report state before the application is activated;
   `MELD7T_AUTO_MIGRATE` remains `false`.
5. Run `activate-release.sh RELEASE --confirm-migrated`. It atomically switches code, config, and
   Quadlet symlinks; restarts the complete data/edge/worker image set; waits up to three minutes for
   container health and `/readyz`; and restores all prior symlinks/services on failure. It never
   reverses a database migration.
6. Run a golden de-identified one-study DICOM ZIP through resumable intake, inspect and confirm the
   proposed study/series-to-detector plan, exercise harmonized and explicitly unharmonized paths, and
   verify the queue, audit chain, source plus MAP/MELD/HS derived output in Review Study, and
   preliminary/final white-labeled PDFs. Repeat read-only OHIF/DICOM access with every configured
   application role, verify light/dark rendering, take another backup, run the crypto/archive restore
   drill, and perform a full isolated-host application restore before recording production
   acceptance.

## Upgrade and recovery

- Pause new intake and drain detector, routine-upload, report, and harmonization queues. A routine
  upload drain means there is no row in `receiving`/`staged`/`importing`, no
  `failed:rollback_incomplete` evidence awaiting reconciliation, and no report in `generating`.
  Import and install the staged release, take and verify a fresh encrypted backup, run the one-shot
  migration, then activate. Migrations must remain expand/contract compatible with the retained N-1
  API image; otherwise rollback requires the documented database restore.
- Format-3 streaming CMS backup envelopes contain globals from both PostgreSQL clusters; the MELD,
  application Orthanc, and harmonization Orthanc databases; both Orthanc blob volumes; staged
  harmonization uploads, transient recovery workspaces, and generated profiles; immudb server
  state; both independent immudb client roots; Redis; Caddy data; model cache;
  configuration/TLS/secrets (including installed branding assets); audit state; generated combined
  reports and other MELD results. Routine upload ZIPs are transient staging and are intentionally
  drained/removed rather than treated as authoritative backup data; a retained
  `rollback_incomplete` archive/receipt must be reconciled before taking the upgrade backup.
  Schedule encrypted off-host replication and immutable retention to the site RPO. Run
  `restore-drill.sh` regularly and a full
  isolated-host restore at the hospital DR cadence; record measured RPO/RTO and evidence.
- Integrate the one-minute `meld7t-health.timer` failure result and JSON journal output with the
  hospital monitoring system. Set persistent journald forwarding and alerts for backup age, disks,
  NFS, both PostgreSQL and Orthanc services, harmonization rollback/storage health, immudb, Redis,
  GPU, certificate expiry, clock drift, and UPS/RAID. The included health gate alerts at 85% of the
  harmonization Orthanc hard quota, independently of aggregate Podman filesystem utilization.

## Site-owned acceptance gates not automated here

- Replace static Basic Auth with hospital IAM/MFA, revocation, throttling, and shared-workstation
  session controls, or formally approve the compensating controls.
- Put backups under a separate immutable-retention identity/snapshot policy. The included drill
  verifies signatures, decryption, dumps, and archives; production still requires a measured full
  bare-host restore, role/ownership checks, workflow validation, and recorded RPO/RTO.
- Decide whether one rootless-Podman Unix UID is an acceptable trusted-compute boundary. Stronger
  isolation requires a separately designed multi-account/system-service topology.
- Independently validate LUKS recovery escrow, NAS encryption, offline OS/GPU media, firewall ingress
  and egress, DICOM cache behavior on managed browsers, log/PHI retention and purge, and penetration
  testing. Do not activate merely because the automated preflight passes.
- Acceptance-test routine ZIP rejection for mixed patient/study, unsafe archives, duplicate SOPs,
  interrupted/resumed chunks, full quotas, and a worker restart during import. Confirm that no
  processing begins before the human series/role/detector plan confirmation and that all uploaded
  source series remain visible in Review Study.
- Confirm unharmonized warnings on the processing plan, queue/status surfaces, Review Study,
  derived-DICOM provenance, and every preliminary/final PDF version. Validate MAP native-space SEG
  and quantitative Parametric Map geometry and HS bilateral subfield/asymmetry SEG geometry against
  site golden data in the exact deployed OHIF build.
