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
    └── images.lock           # pinned image digests (source of truth for the software env)
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

# 5. Run the pipeline (skeletons until Phase 1/2 decisions land).
just recon SUBJECT
just batch 16
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

## Provenance
`just freeze` writes a timestamped record of the **OS image checksum** (`rpm-ostree status`),
**container digests**, and **driver version** to `provenance/`, and updates `provenance/driver.lock`
— the stable pointer the `gpu-check` drift guard compares against. Run it whenever the environment
changes. On an atomic OS this captures the *entire* stack, OS included — satisfying the plan's
reproducibility requirement.

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

## Open decisions (resolved during Phase 1/2, then wired in here)
- **MELD image path + digest** — set `meld_image` in the `justfile` from the MELD install docs, then pin via `just freeze`.
- **FreeSurfer vs FastSurfer** container, and **0.8 mm `-hires` vs conform-to-1 mm** — decided by the 40-case pilot A/B; the winning recipe becomes the `recon` implementation.
- **PHI boundary** — `data/` is gitignored and never leaves the host; if features are offloaded for GPU training later, export only de-identified surface features (plan §6).
