# meld7t-infra — task runner (daily driver)
# One-time host bring-up lives in ansible/bootstrap.yml.

set shell := ["bash", "-euo", "pipefail", "-c"]

# --- config (override via env) ---
repo       := justfile_directory()
dev_box    := "meld-dev"
# MELD Graph v2.2.5 (Docker Hub). _gpu = GPU-accelerated FastSurfer + prediction (needs >=20GB VRAM;
# your 3090 Ti's 24GB qualifies). NOTE: confirm this exact tag at pull time — if it 404s, check the
# release page for the GPU tag or fall back to `meldproject/meld_graph:v2.2.5`.
meld_image := env_var_or_default("MELD_IMAGE", "meldproject/meld_graph:v2.2.5_gpu")
pkg_image  := env_var_or_default("PKG_IMAGE", "localhost/meld7t/pkg:0.1.0")  # §2.2 convert+clean
api_image  := env_var_or_default("API_IMAGE", "localhost/meld7t/api:0.1.0")  # §5 FastAPI
meld_data  := env_var_or_default("MELD_DATA", repo + "/meld-data")     # bind-mounted to /data
fs_lic     := repo + "/secrets/license.txt"                            # FreeSurfer license
meld_lic   := repo + "/secrets/meld_license.txt"                       # MELD license

default:
    @just --list

# Verify the GPU is visible to a rootless Podman container. Run this to confirm bring-up
#    (after `ansible-playbook bootstrap.yml`) and after every `ujust update` + reboot. Warns if
#    the driver drifted since the last freeze (a stale CDI spec breaks GPU-in-container even when
#    host nvidia-smi looks fine).
#    (The CUDA tag below is irrelevant to the smoke test — nvidia-smi reflects the HOST driver.)
gpu-check:
    #!/usr/bin/env bash
    set -euo pipefail
    echo "== host driver =="
    nvidia-smi
    cur_drv=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -n1 | tr -d '[:space:]')
    echo "== driver drift check =="
    if [[ -f provenance/driver.lock ]]; then
      last_drv=$(tr -d '[:space:]' < provenance/driver.lock)
      if [[ "$cur_drv" != "$last_drv" ]]; then
        echo ">> WARNING: NVIDIA driver changed since last freeze (${last_drv} -> ${cur_drv})."
        echo ">>          A stale CDI spec will break GPU-in-container. Regenerate it, then re-freeze:"
        echo ">>            just cdi-generate  &&  just freeze"
      else
        echo "   driver unchanged since last freeze (${cur_drv})."
      fi
    else
      echo "   no provenance/driver.lock yet — run 'just freeze' once this passes."
    fi
    echo "== CDI spec =="
    ls -l /etc/cdi/nvidia.yaml 2>/dev/null || { echo ">> no CDI spec — run: just cdi-generate"; nvidia-ctk cdi list || true; }
    echo "== GPU inside a rootless Podman container =="
    podman run --rm --device nvidia.com/gpu=all \
      docker.io/nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi

# Generate the CDI spec if gpu-check reports none (needs sudo; safe to re-run).
cdi-generate:
    sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml
    nvidia-ctk cdi list

# --- Durable data tier: TrueNAS/ZFS over NFS (spec §28) ---
# The persistent path is the systemd automount installed by:
#   ansible-playbook -i ansible/inventory.ini ansible/bootstrap.yml --tags storage -K
# These are manual helpers that mount the SAME export with the SAME §28 options.

# Manually mount the durable data tier at ./data (SELinux context= label, hard, nconnect).
mount-data:
    {{repo}}/mount.sh

# Unmount the durable data tier.
umount-data:
    sudo umount {{repo}}/data

# Verify the mount carries the container_file_t label (not nfs_t) — the §28 SELinux check.
data-check:
    #!/usr/bin/env bash
    set -euo pipefail
    if ! mountpoint -q {{repo}}/data; then echo "not mounted: {{repo}}/data (run: just mount-data)"; exit 1; fi
    echo "== mount options =="; findmnt -no SOURCE,FSTYPE,OPTIONS {{repo}}/data
    echo "== SELinux label (must be container_file_t) =="; ls -Zd {{repo}}/data
    findmnt -no OPTIONS {{repo}}/data | grep -q 'context=' \
      && echo "OK: context= label present" \
      || { echo ">> FAIL: no context= — container_t will be denied. Remount: just umount-data && just mount-data"; exit 1; }

# --- Long-running services: rootless Podman Quadlet (spec §2) ---
# Units + config are installed by `ansible-playbook bootstrap.yml --tags services`.
# These helpers drive the user systemd units (quadlet strips the .container suffix).
svc_units := "postgres redis immudb orthanc registry api caddy"

# Reload user systemd so Quadlet regenerates units after editing containers/systemd/*.
services-reload:
    systemctl --user daemon-reload

# Build the platform api image (FastAPI). Run on the dev machine; push to the internal registry.
api-build:
    podman build -t {{api_image}} -f platform/api/Containerfile platform/api/

# --- Worker (host service, §2.3): Arq queue consumer, GPU-serialized ---
# Create the worker's Python 3.13 venv (uv) + deps. Needs secrets/worker.env (loopback URLs).
worker-setup:
    cd {{repo}}/platform/worker && uv venv --python 3.13 \
      && uv pip install --python .venv arq redis 'sqlmodel==0.0.22' 'psycopg[binary]' \
         pydantic-settings dicomweb-client immudb-py

# Run the worker in the foreground (dev). Prod uses the meld7t-worker.service user unit.
worker-run:
    {{repo}}/platform/worker/run-dev.sh

# Install + enable the worker as a systemd user service (boot-start needs linger).
worker-install:
    install -Dm644 {{repo}}/platform/worker/meld7t-worker.service \
      ~/.config/systemd/user/meld7t-worker.service
    systemctl --user daemon-reload
    systemctl --user enable --now meld7t-worker
    systemctl --user --no-pager status meld7t-worker || true

# Start the long-running services (postgres, redis, orthanc, registry, caddy).
# Needs the images present (internal registry / `podman pull`) and services.env installed.
services-up: services-reload
    systemctl --user start {{svc_units}}

# Stop the long-running services.
services-down:
    systemctl --user stop {{svc_units}} || true

# Show unit state + the running service containers.
services-status:
    systemctl --user --no-pager status {{svc_units}} || true
    podman ps --filter label=PODMAN_SYSTEMD_UNIT

# Follow one service's logs (default caddy):  just services-logs unit=orthanc
services-logs unit="caddy":
    journalctl --user -u {{unit}} -f

# 2) Enter the mutable dev box (created by ansible/bootstrap.yml).
dev:
    distrobox enter {{dev_box}}

# Open a path in LazyVim inside the dev box, so LSP resolves the project + its uv venv.
# Activate the venv first for full import intellisense:  source .venv/bin/activate && just edit
edit path=".":
    distrobox enter {{dev_box}} -- nvim {{path}}

# --- MELD pipeline (containerized, rootless Podman + CDI; translated from the real v2.2.5 repo) ---
# Private helper: the shared podman invocation. `:z` = shared SELinux relabel (Bazzite needs it, same
# reason as the GPU device); no --user (rootless maps container-root -> your host UID, so output is
# yours). GPU via the CDI device we validated. The image's entrypoint sources FreeSurfer, then runs ARGS.
# NB (spec §28): meld-data is LOCAL NVMe (container_file_t), so `:z` is correct here. Any bind mount
# whose source is under ./data (the NFS durable tier) must be mounted WITHOUT `:z` — NFS is nfs_t and
# cannot hold the relabel xattr, so `:z` no-ops and container_t is denied. The NFS mount's own
# context= label handles it instead. Keep hot scratch local; rsync outputs to ./data (host-side).
_meld *ARGS:
    mkdir -p {{meld_data}}
    podman run --rm \
      --device nvidia.com/gpu=all \
      -v {{meld_data}}:/data:z \
      -v {{fs_lic}}:/run/secrets/license.txt:ro,z \
      -v {{meld_lic}}:/run/secrets/meld_license.txt:ro,z \
      -e FS_LICENSE=/run/secrets/license.txt \
      -e MELD_LICENSE=/run/secrets/meld_license.txt \
      {{meld_image}} {{ARGS}}

# 1) Pull the image + download the pretrained model & bundled test data.
#    (v2.2.5 moved data to Figshare; if auto-download fails, fetch meld_data manually per their FAQ.)
meld-setup:
    podman pull {{meld_image}}
    just _meld python scripts/new_patient_pipeline/prepare_classifier.py

# 2) Validate the full pipeline on MELD's bundled test subject (~15 min). Proves image+GPU+licenses.
meld-test:
    just _meld pytest

# 3) Run the FCD pipeline on one subject. --fastsurfer = GPU-accelerated segmentation.
#    Output -> {{meld_data}}/output/predictions_reports/<subject>.  Extra flags pass through
#    (e.g. `just meld-run sub-001 --parallelise`, or `-harmo_code H1` once harmonised).
meld-run subject *flags:
    just _meld python scripts/new_patient_pipeline/new_pt_pipeline.py -id {{subject}} --fastsurfer {{flags}}

# --- Recon: raw DICOM -> BIDS T1w -> MELD FCD (spec §2.2, §6, §16) ---
# Build the pkg image (dcm2niix + O'Brien MP2RAGE clean). Run on the dev machine; push to the
# internal registry for air-gap, then re-pin the digest in containers/images.lock.
pkg-build:
    podman build -t {{pkg_image}} -f containers/pkg/Containerfile containers/pkg/

# Prepare a MELD BIDS T1w input from raw DICOM using the pkg container (--network=none, §27).
#   source = uni (default: O'Brien-cleaned MP2RAGE UNI — the surface-QC winner) | mprage
# Writes meld-data/input/<subject>/anat/<subject>_T1w.nii.gz + a series-provenance sidecar (§16).
# NB: dicom_root must be a LOCAL path. If it is on the NFS durable tier (§28), drop `:z` from the
# /dicom mount (nfs_t can't hold the relabel) — but stage DICOM locally, don't recon off NFS.
recon-prepare subject dicom_root source="uni":
    #!/usr/bin/env bash
    set -euo pipefail
    mkdir -p {{meld_data}}/input
    dicom_abs="$(realpath "{{dicom_root}}")"   # podman -v needs an absolute host path
    podman run --rm --network=none \
      -v "$dicom_abs":/dicom:ro,z \
      -v {{meld_data}}/input:/out:z \
      {{pkg_image}} \
      python3 /opt/pkg/recon_prepare.py \
        --dicom-root /dicom --subject {{subject}} --source {{source}} --out /out

# Full recon: DICOM -> BIDS (convert + clean) -> MELD FCD pipeline. source defaults to uni (§16).
#   just recon sub-s01uni "data/raw/subject 1 clean/DICOM"             # UNI (default)
#   just recon sub-s01mp "data/raw/subject 1 clean/DICOM" mprage       # MPRAGE A/B
recon subject dicom_root source="uni":
    just recon-prepare {{subject}} "{{dicom_root}}" {{source}}
    just meld-run {{subject}}

# Record exact versions + digests for provenance (project plan §6/§8). Run after any env change.
freeze:
    #!/usr/bin/env bash
    set -euo pipefail
    mkdir -p provenance
    ts=$(date -u +%Y%m%dT%H%M%SZ)
    f="provenance/freeze-${ts}.txt"
    {
      echo "# frozen ${ts} (UTC)"
      echo; echo "## OS deployment (rpm-ostree)"; rpm-ostree status || true
      echo; echo "## podman images (with digests)"; podman images --digests || true
      echo; echo "## nvidia driver"; nvidia-smi --query-gpu=name,driver_version --format=csv,noheader || true
    } > "$f"
    # stable pointer for the gpu-check driver-drift guard
    nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -n1 | tr -d '[:space:]' > provenance/driver.lock || true
    echo "wrote ${f} (and updated provenance/driver.lock)"

