# Detector contracts

All detector outputs are research-only and require site scientific acceptance. A `DetectorRunner`
implements compute → ingest → DICOM publication for each current detector. The shared prepare stage
binds exact Study/Series/SOP identities and acquisition fingerprints before a detector can run.

| Detector | Input | Execution | Harmonization | Viewer-ready output |
|---|---|---|---|---|
| `meld_fcd` | confirmed T1; INV companions for UNI | GPU | MELD Distributed ComBat | Derived T1 + SEG; site geometry/golden-case acceptance still required |
| `map` | confirmed T1 | CPU | optional versioned normative mean/std maps | Native-T1 reference, threshold SEG, and junction/extension quantitative z-score Parametric Maps |
| `hippunfold` | confirmed T1 + T2 | CPU | not applicable | Native-T2 reference and bilateral subfield/asymmetry SEG; no cross-site concordance |

All runs in one immutable recipe use one deterministic derived Study Instance UID, separate from the
uploaded source study. The case-level Review Study presents the source and shared derived study
together. Publication retains a hash-bound per-SOP and per-series manifest, and every derived series
records whether an applicable profile was used or the user explicitly confirmed an unharmonized run.

## Experimental MAP-inspired morphometry

`containers/map/segment.m` runs SPM unified segmentation. `map_morphometry.py` then computes a
hand-crafted junction feature (`4·GM·WM`) and an eroded-white-matter extension feature. With an
approved `map_normative` profile it uses voxelwise control mean/std images; a case creator/assignee
may instead confirm an explicitly labeled unharmonized run for method development. SPM's inverse
deformation pulls the threshold and z-score maps onto the exact native T1 grid before DICOM
publication; threshold masks use nearest-neighbour sampling and quantitative maps use linear
sampling. In the unharmonized mode those values are robust within-subject left/right asymmetry
scores, not control-cohort normative z-scores.

This is inspired by published MAP concepts but is not claimed to reproduce or be equivalent to the
reference MAP/MAP07 toolbox. Fixed feature definitions, smoothing, thresholds, and cluster rules
must be validated on independent positive and negative controls before any accepted research use.
MAP and MELD numeric scores are not comparable.

## HippUnfold

The exact image and signed offline cache are immutable execution inputs. The cache is mounted
read-only and networking is disabled. Subfield and tissue label schemes are intentionally handled
separately; left/right outputs must use the same space and scheme.

The asymmetry operating point is an explicit worker setting included in the execution contract
(default 10%). It is not a universal biological threshold. Validate and approve it for the study
protocol, and interpret the reported bilateral volumes even when no thresholded finding is emitted.
The bilateral discrete subfield labels are nearest-neighbour resampled onto the consumed native T2
grid; the published SEG includes an additional flagged-side mask only when the validated asymmetry
finding is present.
