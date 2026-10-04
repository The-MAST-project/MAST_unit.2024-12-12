"""Replay Phase-0 triage over the real probe frames of mast00 2026-10-01.

Opt-in: set ``MAST_REPLAY=1``.  It reads nine 94 MB frames from the operational
share (~7 minutes on mast00), which no ordinary test run should do.  ``MAST_REPLAY_ROOT``
overrides where the night's ``Calibration/Focuser`` folder lives.

The labels are the focuser offset from the reference focus (13290) each probe was
taken at.  Within ~500 ticks the frame is a star field; from 2000 out the stars
are large faint donuts (60-240 px), with no bright core at their centroids.
Before the fix every one of these triaged "near".
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from common.config.calibration import FocuserCalibrationSettings
from imaging.hfd import assess_focus_regime, count_significant_sources

ROOT = Path(os.environ.get("MAST_REPLAY_ROOT", r"Z:\MAST\mast00\2026-10-01\Calibration\Focuser"))

# (run folder, probe frame, offset from reference focus)
PROBES = [
    ("0001", "001_FOCUS13767_probe.fits", +477),
    ("0002", "001_FOCUS12767_probe.fits", -523),
    ("0003", "001_FOCUS13790_probe.fits", +500),
    ("0004", "001_FOCUS12790_probe.fits", -500),
    ("0005", "001_FOCUS15290_probe.fits", +2000),
    ("0006", "001_FOCUS11290_probe.fits", -2000),
    ("0007", "001_FOCUS18290_probe.fits", +5000),
    ("0008", "001_FOCUS08290_probe.fits", -5000),
    ("0009", "001_FOCUS23290_probe.fits", +10000),
]

pytestmark = [
    pytest.mark.skipif(not os.environ.get("MAST_REPLAY"), reason="opt-in: set MAST_REPLAY=1"),
    pytest.mark.skipif(not ROOT.is_dir(), reason=f"replay frames not reachable at {ROOT}"),
]


@pytest.mark.parametrize(("run", "frame", "offset"), PROBES, ids=[f"{p[0]}_{p[2]:+d}" for p in PROBES])
def test_triage_on_sky(run, frame, offset):
    st = FocuserCalibrationSettings()
    path = ROOT / run / frame
    n_sig = count_significant_sources(path, min_peak_snr=st.near_min_peak_snr)
    regime = assess_focus_regime(
        path,
        near_hfd_max=st.near_hfd_max_px,
        near_min_stars=st.near_min_stars,
        near_min_peak_snr=st.near_min_peak_snr,
    )
    expected_near = abs(offset) <= 1000  # nothing on record between 523 and 2000
    assert (regime == "near") is expected_near, f"{run} ({offset:+d}): {regime}, {n_sig} significant sources"
