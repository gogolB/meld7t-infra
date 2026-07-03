# meld7t-infra — task runner (daily driver)
# One-time host bring-up lives in ansible/bootstrap.yml.

set shell := ["bash", "-euo", "pipefail", "-c"]

# --- config (override via env) ---
repo       := justfile_directory()
data_dir   := env_var_or_default("MELD_DATA", repo + "/data")
fs_license := env_var_or_default("FS_LICENSE", repo + "/secrets/fs_license.txt")
dev_box    := "meld-dev"
# TODO: set to the image from the MELD install docs, then pin the digest with `just freeze`.
meld_image := env_var_or_default("MELD_IMAGE", "ghcr.io/meldproject/meld_graph:latest")

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

# 3) Pull pipeline image(s).
pull:
    podman pull {{meld_image}}
    @echo ">> TODO: pull FreeSurfer/FastSurfer image per MELD install docs, add to containers/images.lock"

# Single-subject run — SKELETON. Internals finalized after the Phase 1/2 pilot decides
# FreeSurfer-vs-FastSurfer and hires-vs-1mm. Shows the container-invocation pattern:
recon subject:
    @echo ">> TODO recon for '{{subject}}': skull-strip (SynthStrip) -> recon-all/FastSurfer -> MELD"
    @echo "   podman run --rm --device nvidia.com/gpu=all \\"
    @echo "     -v {{data_dir}}:/data:Z -v {{fs_license}}:/opt/freesurfer/license.txt:ro,Z \\"
    @echo "     {{meld_image}} <pipeline-cmd> {{subject}}"

# Resumable, concurrency-capped batch over all subjects — SKELETON.
# Real version: enumerate subjects, skip those with a completion marker, run N in parallel.
batch jobs="16":
    @echo ">> TODO: fan 'just recon' across subjects at -P {{jobs}}, skipping completed (resumable)."

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

