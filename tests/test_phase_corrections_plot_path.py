"""A phase's corrections plot reads the acquisition from its folder, whatever the separators.

PathMaker hands out forward-slashed folders since MAST_common 942e26d, and `save_corrections`
joins `<phase>\\corrections.json` onto one with `os.path.join`, so on Windows the path mixes
both. A pattern that knew only backslashes raised on every acquisition that reached tolerance,
inside `do_acquire`, which failed the acquisition (mast01, 2026-10-25 17:48Z).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from common.corrections import Correction, Corrections
from plotting import plot_phase_corrections

ACQUISITION = "2026-10-25/Acquisitions/seq=0004,time=17-47-29_951,target=0.5929,26.841"


def _corrections() -> Corrections:
    return Corrections(
        phase="spec",
        target_ra=0.5929,
        target_dec=26.841,
        tolerance_ra=0.5,
        tolerance_dec=0.5,
        sequence=[
            Correction(time="2026-10-25T17:47:56.000Z", ra_delta=2.4, dec_delta=1.8),
            Correction(time="2026-10-25T17:48:49.000Z", ra_delta=-0.22, dec_delta=0.29),
        ],
    )


@pytest.mark.parametrize(
    "tail",
    ["/spec/corrections.json", "\\spec\\corrections.json"],
    ids=["forward-slashed", "mixed-separators"],
)
def test_plots_from_a_forward_slashed_acquisition_folder(tmp_path: Path, tail: str):
    folder = tmp_path / ACQUISITION
    (folder / "spec").mkdir(parents=True)
    file = folder.as_posix() + tail

    plot_phase_corrections(phase="spec", corrections=_corrections(), file=file)

    assert Path(file.replace(".json", ".png")).exists()


def _horizontal_lines() -> dict[str, float]:
    import matplotlib.pyplot as plt

    return {
        line.get_label(): line.get_ydata()[0]
        for line in plt.gca().get_lines()
        if len(set(line.get_ydata())) == 1 and not str(line.get_label()).startswith("_")
    }


def test_combined_plot_draws_each_phases_dec_tolerance(tmp_path: Path):
    """Once the RA tolerance is plain RA scaled by 1/cos(Dec) (#191), RA and Dec differ."""
    import matplotlib.pyplot as plt

    plt.close("all")
    folder = tmp_path / ACQUISITION
    folder.mkdir(parents=True)
    corrections = _corrections()
    corrections.tolerance_ra = 2.86
    plot_phase_corrections(
        phase="acquisition",
        corrections=corrections,
        file=str(folder) + "/corrections.json",
        tolerances={"sky": {"ra": 1.0, "dec": 1.0}, "spec": {"ra": 2.86, "dec": 0.5}},
    )

    lines = _horizontal_lines()
    assert lines["'spec' RA tolerance, plain (2.86 arcsec)"] == 2.86
    assert lines["'spec' Dec tolerance (0.5 arcsec)"] == 0.5
    assert lines["'sky' Dec tolerance (1 arcsec)"] == 1.0
