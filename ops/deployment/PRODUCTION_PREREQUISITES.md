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
  production installation. Export and installation reject an empty profile set or a profile whose
  build images differ from the signed runtime image lock.
- The production server imports OCI archives directly. The local registry is an optional cache and
  is never the release authority.

## Host acceptance

- Provision reviewed offline builds of `ansible-core` and `just` before the host enters the
  air-gap; the production play performs no package installation. Bazzite must already provide the
  remaining host commands checked by `ansible/production.yml`, including Podman/Quadlet,
  Python 3.13, OpenSSL, firewalld, and NVIDIA CDI tooling.
- Run the play as the dedicated, noninteractive `meld7t-svc` account (or the explicitly configured
  `required_service_user`) and use stable `~/meld7t-state`. The play rejects root/interactive
  accounts, NFS/CIFS active staging, local filesystems without a LUKS/dm-crypt ancestor, and less
  than 300 GiB free for the default two-slot worst-case scratch reserve. Durable NFS defaults to
  `sec=krb5p`; `sec=sys` requires explicit risk acceptance.
  NAS encryption at rest remains site-controlled and requires a recorded
  `nas_encryption_attestation_id`.
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

## Identity and privacy boundary

- The stock three shared Caddy accounts are a bring-up fallback only. Hospital activation requires
  unique per-person institutional identities, explicit role rows, an IAM/security approval record,
  and a matching DICOM allow/deny row. The validator rejects the shared fallback names. Only
  reviewer/admin roles can reach DICOMweb or the OHIF origin; auditors and submitter-only identities
  receive HTTP 403. OHIF study-list browsing is disabled.
- Caddy strips caller identity headers and supplies authenticated identity, roles, and a 32+ character
  proxy secret to the API over the isolated edge network. The API/data networks use separate Podman
  subnets. Redis requires a distinct password and no-eviction persistence policy.
- Browser DICOMweb accepts only GET/HEAD/OPTIONS, adds `private, no-store`, and passes a separate
  internal service credential to Orthanc. Orthanc also authenticates API/worker loopback access;
  browser identities never receive that credential. Do not run unrelated processes as the service
  account because that one UID can still read release secrets and control rootless Podman.
- Supply separate Postgres, Orthanc, Redis, immudb, proxy, audit-HMAC, backup-recipient, and release-
  signing secrets. API and worker share only the runtime values they require. Create a restricted
  immudb runtime principal after first boot instead of retaining the bootstrap administrator. Set
  `IMMUDB_AUTH=true`, `IMMUDB_DEVMODE=false`, and `IMMUDB_MAINTENANCE=false`. Provision an EC signing
  private key as the rootless Podman secret and pin its public key. API and worker use distinct
  persistent root-state files (`api-immudb-state` and `~/meld7t-state/audit/worker.root`).
- The production input directory must contain mode-0400/0600 files owned by the service account:
  the seven split env files, Redis config, TLS pair, two licenses, three Caddy identity maps plus
  approval, and `immudb-signing-private.pem`, `immudb-signing-public.pem`,
  `backup-signing-public.pem`, and `release-signing-public.pem`.

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
   starts only PostgreSQL, Redis, immudb, and Orthanc. Interactively use `immuadmin` as the bootstrap
   administrator to create the exact non-admin/readwrite runtime principal named in API/worker envs.
   Never place the administrator credential in either runtime env.
4. Take an encrypted signed baseline backup. `migrate.sh STAGED_RELEASE BACKUP_DIR TRUSTED_KEY`
   verifies the signature, checks the signed timestamp (not mutable mtime), host, and current-release
   binding, and rejects a backup outside the 24-hour window.
5. Run `activate-release.sh RELEASE --confirm-migrated`. It atomically switches code, config, and
   Quadlet symlinks; restarts the complete data/edge/worker image set; waits up to three minutes for
   container health and `/readyz`; and restores all prior symlinks/services on failure. It never
   reverses a database migration.
6. Run the golden de-identified DICOM case, verify audit-chain/Orthanc/OHIF output, take another
   backup, run the crypto/archive restore drill, and perform a full isolated-host application restore
   before recording production acceptance.

## Upgrade and recovery

- Pause and drain the queue, import/install the staged release, take and verify a fresh encrypted
  backup, run the one-shot migration, then activate. Migrations must remain expand/contract compatible
  with the retained N-1 API image; otherwise rollback requires the documented database restore.
- Streaming CMS backup envelopes contain PostgreSQL globals and both databases, Orthanc blobs,
  immudb server state, both independent immudb client roots, Redis, Caddy data, model cache,
  configuration/TLS/secrets, audit state, and MELD results. Schedule encrypted off-host
  replication and immutable retention to the site RPO. Run `restore-drill.sh` regularly and a full
  isolated-host restore at the hospital DR cadence; record measured RPO/RTO and evidence.
- Integrate the one-minute `meld7t-health.timer` failure result and JSON journal output with the
  hospital monitoring system. Set persistent journald forwarding and alerts for backup age, disks,
  NFS, PostgreSQL, Orthanc, immudb, Redis, GPU, certificate expiry, clock drift, and UPS/RAID.

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
