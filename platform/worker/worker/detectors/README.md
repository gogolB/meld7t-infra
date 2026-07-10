# Detectors

Each detector is a `DetectorRunner` (see `base.py`): a versioned `compute → package → ingest`
triple that the worker dispatches by `detector_id`. The DICOM→BIDS `prepare` step is shared
(`pipeline.run_prepare`); MELD is one runner among many (spec §18, §25.1).

| detector_id  | runner            | input          | finding |
|--------------|-------------------|----------------|---------|
| `meld_fcd`   | `MeldRunner`      | T1 (UNI/MPRAGE) | FCD clusters (GNN), DICOM-SEG overlay |
| `hippunfold` | `HippUnfoldRunner`| T1 + T2 SPACE  | hippocampal subfield volumes + L/R asymmetry |
| `map`        | `MapRunner`       | T1 (MPRAGE/UNI) | FCD candidates — SPM junction/extension morphometry |

## MAP (FCD) — SPM voxel morphometry + the normative gap

MAP is the Huppertz "Morphometric Analysis Program" (MAP07) method: an SPM-based voxel morphometry
detector, methodologically independent of MELD's surface GNN. Engine = stock **SPM12 Standalone
r7771 + MATLAB MCR** (`spmcentral/spm`, pinned in `images.lock`); no MATLAB licence, air-gappable.

Pipeline (`map.py` + `containers/map/segment.m` + `containers/pkg/map_morphometry.py`):
1. **compute** — gunzip the T1 → `/work/T1.nii`, run `spm12 script segment.m` (unified
   segmentation). Produces native `c1/c2/c3`, MNI `wc1/wc2` (unmodulated GM/WM) + `mwc1/mwc2`, and
   the `y_/iy_` deformations. ~90 s on a clean 7T UNI. CPU-only (`uses_gpu=False`).
2. **ingest** — `map_morphometry.py` builds two FCD feature maps from the MNI GM/WM probabilities:
   *junction* (`4·GM·WM`, GM/WM boundary blurring) and *extension* (GM deep inside WM), then
   converts them to candidate clusters.

**The normative gap (§25.2) — read before trusting any MAP output.** MAP07's feature maps are only
clinically meaningful as z-scores against a control cohort. That cohort is **not staged**, so:
- **NORMATIVE mode** (preferred): if `meld-data/normative/map/<feature>_{mean,std}.nii.gz` exists
  (built from 7T controls on the SPM MNI grid), z is voxelwise `(feat−mean)/std` → `harmo_code`
  set. This is real MAP07. *Not active yet.*
- **ASYMMETRY mode** (current fallback): with no cohort, a raw single-subject z is ill-posed — the
  junction band is high along *every normal* boundary and the extension map is zero-inflated (its
  MAD→0 makes robust-z explode). So we z-score inter-hemispheric **asymmetry** (`feat − mirror`
  across the MNI mid-sagittal plane): a feature elevated on one side vs its mirror is the candidate.
  Control-free and robust, but blind to symmetric/bilateral disease. `harmo_code="none"`,
  `saliency.single_subject=true`. **Hypothesis-generating, for adjudication only — not detections.**

No viewer overlay yet: the feature maps are in MNI space; warping thresholded clusters back through
`iy_T1.nii` to the T1 frame for a DICOM-SEG is a follow-up (findings already render in MDT/
concordance). When the control cohort lands, drop the templates in `normative/map/` and MAP becomes
a real z-scored detector with no code change.

## HippUnfold (HS) — air-gap prerequisites

HippUnfold is a BIDS App run as a sibling `podman` job. Three things must be true for it to run
offline on this hardware; all three were runtime failures we hit and fixed:

1. **Model/template cache volume** (`hippunfold-cache`, mounted at `/root/.cache/hippunfold`).
   HippUnfold otherwise downloads the nnU-Net model, the CITI168/upenn templates, and the
   multihist7 atlas at run time — impossible across the air-gap (spec §11). Pre-stage once on an
   internet host, then carry the volume. Current contents: `atlas/ model/ template/`.

2. **nnU-Net model tar rewritten to owner 0.** The bundled model tar carries files owned by a
   high host uid (e.g. 3050834). Under rootless podman, `tar -xf` as container-root tries to
   `chown` to that uid, which is outside the user-namespace map → fatal under Snakemake's strict
   mode (`Cannot change ownership … Invalid argument`). Rewrite the cached tar with
   `tar --owner=0 --group=0 --numeric-owner` before staging it in the cache volume.

3. **CPU-only inference (no `--device`).** HippUnfold's bundled torch (py3.9) has no compiled
   kernels for Ampere (sm_86) — a 3090 Ti gives `CUDA error: no kernel image is available`.
   nnU-Net therefore runs on CPU; the hippocampal crop is small so this is acceptable
   (~30–40 min wall-clock for a bilateral run on this box). MELD keeps the GPU.

## Subfield label schemes (gotcha)

HippUnfold emits two *different* volumetric labelmaps with *incompatible* label numbering
(confirmed from the image's `snakebids.yml` `tissue_atlas_mapping`):

- **`desc-subfields …_dseg`** (multihist7 atlas, native T2w): `1=Sub 2=CA1 3=CA2 4=CA3 5=CA4
  6=DG 7=SRLM 8=Cyst`. This is the correct source for per-subfield volumes. **Preferred.**
- **`desc-postproc`/`desc-tissue …_dseg`** (corobl space): a *tissue* scheme where `dg=8,
  srlm=2, cyst=7`. Only a grey-matter *total* is meaningful here (exclude SRLM=2, cyst=7); the
  labels are **not** subfields. Used by `hippunfold_summarize.py` only as a fallback for the
  asymmetry total when the subfield dseg is absent.

`hippunfold_summarize.py` (in the pkg image, ≥0.3.1) picks the scheme by filename, computes
grey-matter volume per hemisphere, and reports the asymmetry index `AI = 100·(L−R)/(½(L+R))`,
flagging `|AI| ≥ 10%`. The finding is emitted as a first-class cluster on the atrophic side so it
slots into the result model and the MDT concordance view — no viewer overlay required.

Validated on the pilot subject: **L 1672 mm³ · R 1752 mm³ · AI −4.69% (symmetric, not flagged).**
