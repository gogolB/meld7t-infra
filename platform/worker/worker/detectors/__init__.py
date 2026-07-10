"""Detector runner registry (worker-side execution, spec §18/§25.1)."""
from .base import DetectorRunner
from .hippunfold import HippUnfoldRunner
from .meld import MeldRunner

RUNNERS: dict[str, DetectorRunner] = {
    r.detector_id: r for r in (MeldRunner(), HippUnfoldRunner())
}


def get_runner(detector_id: str) -> DetectorRunner | None:
    return RUNNERS.get(detector_id)
