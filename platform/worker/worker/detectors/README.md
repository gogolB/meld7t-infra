# Detector contracts

All detector outputs are research-only and require site scientific acceptance. A `DetectorRunner`
implements compute → ingest → optional DICOM publication. The shared prepare stage binds exact
Study/Series/SOP identities and acquisition fingerprints before a detector can run.

| Detector | Input | Execution | Harmonization | Output limitation |
|---|---|---|---|---|
| `meld_fcd` | confirmed T1; INV companions for UNI | GPU | MELD Distributed ComBat | Derived T1 + SEG; site geometry/golden-case acceptance still required |
| `map` | confirmed T1 | CPU | versioned normative mean/std maps | Experimental MAP-inspired implementation; no DICOM overlay |
| `hippunfold` | confirmed T1 + T2 | CPU | no validated interface yet | Exploratory volumetry/asymmetry only; no DICOM overlay or cross-site concordance |

## Experimental MAP-inspired morphometry

`containers/map/segment.m` runs SPM unified segmentation. `map_morphometry.py` then computes a
hand-crafted junction feature (`4·GM·WM`) and an eroded-white-matter extension feature. With an
approved `map_normative` profile it uses voxelwise control mean/std images; an administrator may
permit an explicitly labeled unharmonized asymmetry run for method development.

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
