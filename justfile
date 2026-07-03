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

