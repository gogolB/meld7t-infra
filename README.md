# meld7t-infra

Reproducible, documented infrastructure for running the **7T MELD FCD/HS pipeline** on a
Bazzite (Fedora Atomic / Universal Blue) workstation. Everything here is version-controlled so
the environment is auditable and rebuildable — which is a prerequisite for the pipeline being
trial-grade (see the project plan, §6 and §8).

## Design: what runs where

Bazzite's own software hierarchy dictates the architecture. We follow it:

| Layer | Tool | What lives here |
|---|---|---|
| Host (immutable) | Bazzite image | NVIDIA driver + CUDA (baked in), Podman, distrobox. **Nothing layered.** |
| Pipeline | **Podman containers** | MELD, FreeSurfer/FastSurfer — pinned by digest. The reproducible core. |
| Dev / interactive | **distrobox** (`meld-dev`) | Python envs, exploration, debugging. Mutable, isolated from the host. |
| Batch as a service | **Quadlet** (systemd + Podman) | The resumable batch runner, managed as a unit. *(added once recon approach is fixed)* |
| Personal CLI | **Homebrew** (`Brewfile`) | `just`, `jq`, `gh`, etc. **Not the pipeline.** |

### Why the pipeline is NOT in Homebrew
Brew ships with Bazzite and is correct for CLI convenience tools. But `brew upgrade` drifts
versions — reintroducing the OS-drift problem an atomic OS exists to prevent, one layer up. The
scientific stack is pinned by **container image digest** instead (`containers/images.lock`), which
is the whole point of "documented and trackable."

### Why not rpm-ostree layering
Layering scientific packages onto the immutable base is discouraged (it can block future OS
upgrades) and defeats reproducibility. We never do it.

## Repo layout

```
meld7t-infra/
├── README.md                 # this file
├── justfile                  # daily-driver task runner (gpu-check, dev, pull, recon, batch, freeze)
├── mount.sh                  # manual fallback: mount the NFS durable data tier with §28 options
├── Brewfile                  # personal CLI tools (optional; not the pipeline)
├── .gitignore                # keeps PHI/data and secrets out of git
├── ansible/
│   ├── bootstrap.yml         # one-time host bring-up (atomic-aware: no dnf on host)
│   └── inventory.ini         # localhost
├── shell/
│   └── modern-cli.sh         # interactive aliases (ls→eza, cat→bat, vim→nvim); deployed to ~/.bashrc.d/
├── nvim/
│   └── lua/plugins/extras.lua # tracked LazyVim language extras (python/yaml/ansible/docker/…); → ~/.config/nvim/
└── containers/
    ├── images.lock           # pinned image digests (source of truth for the software env)
    ├── systemd/              # Quadlet units: long-running services + network + volumes (§2)
    │   ├── meld-net.network
    │   ├── *.volume          # orthanc-storage / postgres-data / redis-data / caddy-data / registry-data
    │   └── *.container       # postgres / redis / orthanc / registry / caddy
    ├── config/               # config the units mount (Caddyfile, orthanc.json, postgres init)
    │   └── services.env.example  # secrets template → copy to secrets/services.env (gitignored)
    └── pkg/                  # §2.2 pkg image: dcm2niix + O'Brien MP2RAGE clean (recon input)
        ├── Containerfile
        ├── recon_prepare.py  # DICOM → BIDS T1w (series-match, convert, clean, provenance)
        └── clean_uni.py      # O'Brien INV1×INV2 robust background removal
```

## Shell / CLI

`shell/modern-cli.sh` (deployed by bootstrap to `~/.bashrc.d/50-modern-cli.sh`) aliases modern
replacements: `ls`→`eza`, `cat`→`bat --paging=never`, `vim`→`nvim`, and sets `$EDITOR`/`$VISUAL`
to `nvim` so programs that spawn an editor (git, `sudoedit`) use it too. Each is `command -v`-guarded,
so the file is safe on the host and inside the distrobox alike.

The original tools are always available: `command ls` / `\ls` bypass an alias for one call,
`/usr/bin/ls` is the real binary, and aliases don't expand in scripts. `bat` is pipe-aware
(falls back to plain output when piped), so pipelines are unaffected.

> **Nerd Font required for icons.** eza's `--icons=auto` (and Nerded editor UIs) render as boxes
> without one. Install e.g. JetBrainsMono Nerd Font into `~/.local/share/fonts` + `fc-cache -f`,
> set it in your terminal, or drop `--icons=auto` from `shell/modern-cli.sh`.

## Editor (LazyVim)

Bootstrap installs **LazyVim inside the `meld-dev` distrobox**. It lives there (not on the host)
for two reasons: Mason needs a toolchain (gcc/node/pip) the immutable host lacks, and — more
importantly — `pyright`/`ruff` must run in the same environment as the code to resolve the
pipeline's `uv` venv imports. The config at `~/.config/nvim` is shared via `$HOME`, so host `nvim`
sees it too (fine for quick edits); authoritative code editing happens in the box.

Language support (`nvim/lua/plugins/extras.lua`) is tracked as code: Python, YAML, Ansible, Docker,
TOML, JSON, Markdown — the pipeline stack. Edit that file and re-run `--tags editor` to change it.

```bash
# edit pipeline code with full LSP (activate the venv so pyright sees project deps):
cd <project> && source .venv/bin/activate && just edit
# or a specific file:
just edit path=pipeline/run_subject.sh
```

First interactive launch finishes any remaining Mason LSP installs; check with `:LazyHealth` /
`:Mason`. Requires a Nerd Font (see note above). To rebuild just the editor on a box:
`ansible-playbook -i ansible/inventory.ini ansible/bootstrap.yml --tags editor`.

### AI CLIs (Claude Code + Codex)

Installed by bootstrap (`--tags ai-cli`) via the vendors' **native, Node-free install scripts**
(`claude.ai/install.sh`, `chatgpt.com/codex/install.sh`) — not brew or npm. Both are self-contained
binaries that land in `~/.local/bin`, which is shared into the distrobox, so **one install serves
host and box**. (Brew's `--cask codex` can install the desktop app rather than the CLI, and the
unscoped `codex` npm package is an unrelated project — the native installers avoid both traps.)
First run of `claude` / `codex` prompts for sign-in (Claude Pro/Max or API key; ChatGPT plan or API
key) — that step can't be automated.

## Runbook

Prereq (Bazzite ships brew): `brew install just ansible`.

```bash
# 1. Host bring-up — RUN THIS FIRST on a fresh box. Idempotent. Configures the CDI spec,
#    the SELinux device boolean, working dirs, and the dev box, then smoke-tests the GPU.
#    (Also installs `just` via the Brewfile, so no separate step for it.)
#    -K prompts once for your sudo password: two tasks touch host root (CDI spec + setsebool).
ansible-playbook -i ansible/inventory.ini ansible/bootstrap.yml -K
#    (The tagged subsets below do NOT need -K — their only sudo is inside the distrobox.)

# 2. Verify GPU-in-container — confirms bring-up, and is the command to re-run after every
#    `ujust update` + reboot (the driver-drift guard lives here).
just gpu-check
#    First run prints "no driver.lock yet" — expected; `freeze` below creates the baseline.

# 3. Pull the pipeline image(s), then snapshot provenance (digests + OS checksum + driver.lock).
just pull
just freeze

# 4. Interactive dev when you need it.
just dev             # enter the meld-dev distrobox

# 5. Recon a subject: raw DICOM → BIDS T1w (convert + clean) → MELD FCD pipeline.
just pkg-build                                              # once, on the dev machine
just recon sub-s01uni "data/raw/subject 1 clean/DICOM"      # UNI source (default)
```

## Updating the host

`ujust update` stages a new Bazzite **image** (plus flatpak/brew updates); the OS change is **not
live until you reboot**. Standing rule:

```
ujust update   →   reboot   →   just gpu-check
```

If the image bump moved the **NVIDIA driver**, any existing CDI spec still points at the *old*
driver's library paths and goes stale — GPU-in-container then breaks *after* the reboot even though
host `nvidia-smi` looks fine. `gpu-check` detects this automatically (it diffs the running driver
against `provenance/driver.lock`) and tells you to fix it:

```
just cdi-generate   &&   just freeze
```

Re-`freeze` afterward so the pinned OS checksum and driver match the deployment you actually run.
**Rule of thumb: regenerate CDI after any `ujust update` that bumps the driver.**

## Platform API (spec §5) — Phase 1 of the buildout

`platform/api/` is the FastAPI orchestration brain: it owns the case → recipe → run → result
workflow and the **detector-plural** data model (§8/§15/§25.1 — MELD is one detector among many).
Backed by the `meld` Postgres DB (Alembic migrations) and the **immudb** audit ledger (§26).

**Workflow the API drives:**
1. `POST /api/cases` — create a case (optionally referencing an Orthanc study).
2. `POST /api/cases/{id}/series/sync` — QIDO Orthanc, **propose** a role per series (§16).
3. `POST /api/cases/{id}/series/confirm` — submitter **confirms/overrides** roles (never silent-auto).
4. `POST /api/cases/{id}/recipe` (`workup=fcd|hs|both`) — build the job plan: one run per
   detector × source. **Tandem** = MELD on every viable T1 present (UNI *and* MPRAGE); MAP/HS appear
   as `pending` slots (§25.7). `POST …/recipe/confirm` materializes the runs.
5. `POST /api/runs/{id}/adjudication` — append-only reviewer read (also to immudb).
6. `GET /api/system`, `GET /api/audit/verify` — status + hash-chain verification.

**Audit ledger:** every consequential event is appended to **immudb** (Merkle-backed, tamper-proof)
with a mirror row in Postgres carrying an application hash chain (`H(payload ‖ prev_hash)`) and the
immudb tx id — verifiable with `GET /api/audit/verify` (§26). Corrections are new appended entries.

**Build + run:** `just api-build` (dev machine) → the `api` Quadlet unit runs it on `meld-net`;
Caddy proxies `/api/*` to it. The entrypoint runs `alembic upgrade head` before serving (§22).
Tests: `python -m pytest platform/api/tests` (schema, tandem recipe, workflow, audit chain).

### Worker + GPU-serialized queue (§2.3, §18)

`platform/worker/` is a **host** systemd user service (not a container — so it launches podman
sibling jobs with direct rootless GPU access, avoiding podman-in-podman nesting). It's an **Arq**
Redis-queue consumer with **`max_jobs=1` — a single global GPU semaphore**, so runs pipeline one
after another on the one GPU. Per run: `recon_prepare` (pkg, `--network=none`) → MELD
(`--fastsurfer`, GPU) → ingest clusters/report into Postgres. Live status streams to Redis; the
dashboard reads `GET /api/queue` and `GET /api/system` (in-use run, GPU, per-status counts). Admin
`POST /api/admin/pause|resume` halts the queue between jobs (§18). OOM is classified explicitly
(`failed_oom`), never a silent CPU retry (§6).

Setup: `just worker-setup` (uv venv + deps) → `just worker-install` (systemd user unit), or
`just worker-run` foreground for dev. Reaches Postgres/Redis/immudb/Orthanc over loopback.

### Packaging → Orthanc (§10, §17)

After MELD, the worker runs the pkg container (`package_dicom.py`, `highdicom`) to build — in the
input-T1 frame of reference — a **base T1 MR series** and a **DICOM-SEG** of the discrete clusters,
and STOW-RS them to Orthanc as one derived study. The study/series UIDs are written to `results`, so
OHIF (Phase 4) opens the study over `/dicom-web/*` and overlays the SEG on the T1. The **continuous
parametric probability series** (§17, the threshold-slider source) needs surface→volume reprojection
of MELD's per-vertex hdf5 probability (not emitted as a volume by default) — the tracked follow-up.

### Frontend + OHIF (§9)

`platform/web/` is a React/Vite SPA shell (air-gap: everything bundled, no CDN, §9.4) served static
by Caddy at `/`: **Dashboard** (live cases, GPU queue, audit-chain status), **Submit**, **CaseView**
(series-role confirm §16 → recipe builder §25.1 → runs), **Review** (OHIF iframe + clusters +
append-only adjudication), **Admin** (pause queue, detector registry, audit verify). Talks to the API
same-origin via `/api`. Build: `just web-build` → `platform/web/dist`.

The **OHIF viewer** runs as a pinned container on its own same-origin port (`:8444`, so its
root-absolute assets don't collide with the shell); it reads Orthanc over same-origin `/dicom-web`
(no CORS) and the Review screen iframes it with the packaged study UID. Verified end-to-end with
headless screenshots: the shell renders live data and OHIF loads the packaged **MR + DICOM-SEG**
studies from Orthanc.

> **Buildout status:** Phases 1–4 done — submit → series-confirm → recipe (tandem) → GPU queue →
> MELD → DICOM-SEG in Orthanc → OHIF review + adjudication, all in the browser, verified live.
> Next: Phase 5 (MDT summary, concordance view). Follow-ups: continuous parametric map (§17),
> OHIF deep-link straight into the study, real MAP/HS detectors.

## Recon pipeline (DICOM → MELD)

`just recon SUBJECT DICOM_ROOT [source]` runs the full FCD recon: the **pkg** container
(`containers/pkg/`, spec §2.2) converts DICOM→NIfTI (`dcm2niix`), background-cleans the source,
and writes a BIDS `sub-<id>/anat/sub-<id>_T1w.nii.gz` (+ a series-provenance sidecar, §16); then
`just meld-run` runs MELD Graph with `--fastsurfer`. The pkg step runs `--network=none` (§27) — it
only touches mounted volumes.

**Source series — provisional default: `uni`.** This site's protocol offers two viable T1 sources
(§16). A first real-subject A/B (see `provenance/` and the pilot notes) measured them head-to-head:

| Source | prep | surface defects (FreeSurfer holes) | note |
|---|---|---|---|
| **`uni`** (cleaned MP2RAGE UNI) | O'Brien INV1×INV2 background clean | **32** (lh20/rh12) | B1-robust; 0.7 mm isotropic conforms cleanly |
| `mprage` (AX conventional) | none | 45 (lh27/rh18) | zero-prep; anisotropic 0.375×0.375×0.7, residual 7T B1⁺ |

The cleaned UNI recons a **topologically cleaner surface** (the §16 decision criterion), so it is the
default. This is **n=1** — ratify across more pilot cases before hard-committing; both sources stay
runnable (`just recon SUBJECT DICOM_ROOT mprage`). **Both sources also produced *different, likely
false-positive* clusters** on this single unharmonized subject — a live reminder that outputs are
research/hypothesis-generating and need the §25.2 control cohort (harmonization) + human adjudication.

The **UNI clean is O'Brien (2014) regularization, not a mask** (`clean_uni.py`): a spatial mask
clips the brain; O'Brien recovers the signed numerator from the scanner UNI and suppresses the
salt-and-pepper background via a β term, preserving tissue (esp. the inferior temporal lobes). No
skull-strip — MELD does its own brain extraction (§7).

**VRAM (measured, §14):** prediction peaks at **~21.2 GB** (fixed icosphere, source-invariant),
segmentation ~9 GB; total ≈ prediction, fits the 24 GB card with ~3.4 GB headroom.

## Provenance
`just freeze` writes a timestamped record of the **OS image checksum** (`rpm-ostree status`),
**container digests**, and **driver version** to `provenance/`, and updates `provenance/driver.lock`
— the stable pointer the `gpu-check` drift guard compares against. Run it whenever the environment
changes. On an atomic OS this captures the *entire* stack, OS included — satisfying the plan's
reproducibility requirement.

## Durable data tier (TrueNAS/ZFS over NFS)

Pipeline intermediates are retained durably on the air-gapped TrueNAS NAS, mounted at `./data`
(gitignored — it is PHI-bearing). The split is **hot scratch = local NVMe** (the live recon;
small-file storms never touch NFS) and **durable tier = NFS** (bulk sequential rsync of completed
runs). See the project plan §28.

**Bring it up (persistent, version-controlled):**
```bash
ansible-playbook -i ansible/inventory.ini ansible/bootstrap.yml --tags storage -K
```
This installs a systemd **`.automount`** for `./data` (mount-on-access, so a down NAS never hangs
boot) with the full §28 option set, and unmounts any stale ad-hoc mount first. Override the NAS
target with `-e nas_host=… -e nas_export=…`. Manual fallback: `just mount-data` / `just umount-data`
(both call `mount.sh`); verify with `just data-check`.

**The one option that must never be dropped — SELinux `context=`.** NFS presents every file as
type `nfs_t`, which cannot hold the `security.selinux` xattr that a Podman `:z`/`:Z` bind-relabel
writes. So bind-mounting an NFS path into a container with `:z` **silently no-ops** and the confined
`container_t` domain is **denied** — the same failure *class* as the GPU/CDI SELinux issue, a
different fix. The mount therefore carries a whole-export fixed label
(`context="system_u:object_r:container_file_t:s0"`); compute containers get a **plain** bind-mount
of the host path (**never `:z`**, never NFS-inside-the-container). `just data-check` asserts the
label is `container_file_t`, not `nfs_t`.

Full mount options: `vers=4.2,hard,noatime,nconnect=4,sec=sys,context="…container_file_t",_netdev`
(`hard`, not `soft`, because durability requires writes to eventually succeed, not fail silently).

## Long-running services (rootless Podman Quadlet)

The platform's always-on services (spec §2) are declared as **Quadlet** systemd units in
`containers/systemd/` and version-controlled here. The off-the-shelf tier is wired up now —
**postgres, redis, orthanc, registry, caddy**; the `api` (built FastAPI image) and the host
`worker` carry bespoke code and drop in behind the same pattern once built. Everything runs
**rootless** as *user* systemd services; `linger` lets them boot-start without a login.

**Bring it up:**
```bash
# 1. Provide secrets (never in git): copy the template, fill in real passwords.
cp containers/config/services.env.example secrets/services.env && $EDITOR secrets/services.env

# 2. Install the units + config into the user Quadlet path, enable linger, daemon-reload.
ansible-playbook -i ansible/inventory.ini ansible/bootstrap.yml --tags services -K

# 3. Once the images are present (internal registry, or `podman pull` on a connected box):
just services-up          # start postgres, redis, orthanc, registry, caddy
just services-status      # unit state + running containers
just services-logs unit=orthanc   # follow one service
```

**How it fits together:**
- All services share the `meld-net` podman network; only Caddy publishes to the host
  (`8443`→443, `8080`→80 by default — rootless-safe high ports). Orthanc, Postgres, and Redis
  are **not** published — reachable only on `meld-net` (§4). The registry publishes on
  `127.0.0.1:5000` for dev-transfer pushes only.
- Caddy proxies same-origin paths (§3): `/dicom-web/*`→Orthanc, `/api/*`→FastAPI (502 until
  `api` exists), `/`→the static SPA (mount the built bundle at `/srv/spa` when staged).
- Postgres hosts **two** databases created on first init: `orthanc` (DICOM index, §4) and
  `meld` (results/metadata, §8). Orthanc keeps DICOM **files** on the `orthanc-storage`
  volume and only its **index** in Postgres.
- TLS is Caddy's **internal CA** (`tls internal`, §3) — no ACME. Distribute the CA root (in
  the `caddy-data` volume) to intranet clients, or swap in institutional PKI in the Caddyfile.

**Editing a unit:** change the file under `containers/systemd/`, re-run `--tags services` (or
`just services-reload`), then `just services-up`. **Secrets** live only in `secrets/services.env`
(gitignored); bootstrap installs them to `~/.config/meld7t/services.env` (mode 0600).

**Serving on 443/80 instead of 8443/8080** (optional): lower the rootless port floor —
`echo 'net.ipv4.ip_unprivileged_port_start=80' | sudo tee /etc/sysctl.d/50-unprivileged-ports.conf && sudo sysctl --system`
— then change Caddy's `PublishPort=` lines to `443:443` / `80:80` and `just services-reload && just services-up`.

## Troubleshooting

**`Failed to initialize NVML: Insufficient Permissions` inside a container** (host `nvidia-smi`
works, CDI spec present, `--device nvidia.com/gpu=all` accepted). Bazzite's enforcing SELinux is
blocking the confined `container_t` domain from the `/dev/nvidia*` nodes. Fix (persistent):

```bash
sudo setsebool -P container_use_devices on
```

Confirm with `getsebool container_use_devices` (should read `--> on`); see the denial with
`sudo ausearch -m avc -ts recent | grep -i nvidia`. `bootstrap.yml` sets this automatically, so a
fresh box won't hit it — this note is for manual runs. If it persists after the boolean, compare
rootless vs. `sudo podman` to isolate confined-domain access from a stale CDI spec / driver issue.

## Open decisions
Resolved:
- **MELD image + digest** — pinned in `containers/images.lock`; validated end-to-end on real 7T data.
- **Recon recipe** — FastSurfer (`--fastsurfer`), **conform-to-1 mm** (MVP default), source **`uni`**
  (cleaned) — wired into `just recon`. See the Recon section above.

Still open:
- **Source series is n=1** — ratify `uni` vs `mprage` across more pilot cases (spec's 40-case A/B).
- **0.8 mm `-hires` variant** — not run; its segmentation VRAM is still unmeasured (§14). The default
  conforms to 1 mm; hires is a configurable variant, a distinct choice.
- **PHI boundary** — `data/` is gitignored and never leaves the host; if features are offloaded for
  GPU training later, export only de-identified surface features (plan §6).
