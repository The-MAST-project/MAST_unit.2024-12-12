"""A phase's ``corrections.json`` is written and handed to the mover once.

Regression: ``Solver.solve_and_correct`` wrote and enqueued ``<phase>/corrections.json``
on an in-tolerance solve, and ``Acquisition.save_corrections`` wrote and enqueued the
same path (with its plot) right after, so whichever move ran second found the file gone
and logged ``failed to move`` on every acquisition (MAST_common#117, defect 3).
``save_corrections`` is the owner: every caller of ``solve_and_correct`` calls it
after the phase, however the phase ended.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import astropy.units as u
import pytest
from astropy.coordinates import Angle

import solving
from acquisition import ApproachMode
from common.corrections import Corrections
from common.interfaces.solving import SolverId, SolvingTolerance
from common.models.statuses import ImagerSettings
from common.utils import Coord

TARGET_RA_HOURS = 5.0
TARGET_DEC_DEGS = 30.0
TOLERANCE_ARCSEC = 1.0


@pytest.fixture
def fake_filer(monkeypatch):
    filer = MagicMock()
    monkeypatch.setattr(solving, "filer", filer)
    return filer


def _in_tolerance_result() -> SimpleNamespace:
    target = Coord(ra=Angle(TARGET_RA_HOURS * u.hourangle), dec=Angle(TARGET_DEC_DEGS * u.deg))
    return SimpleNamespace(
        succeeded=True,
        errors=None,
        solution=SimpleNamespace(
            ra_hours=TARGET_RA_HOURS,
            ra_rads=target.ra.radian,
            dec_rads=target.dec.radian,
        ),
        to_dict=lambda: {},
    )


def _solver(monkeypatch, phase: str) -> solving.Solver:
    corrections = Corrections(
        phase=phase,
        target_ra=TARGET_RA_HOURS,
        target_dec=TARGET_DEC_DEGS,
        tolerance_ra=TOLERANCE_ARCSEC,
        tolerance_dec=TOLERANCE_ARCSEC,
    )
    unit = MagicMock()
    unit.is_active.return_value = True
    unit.acquirer.latest_acquisition.corrections = {phase: corrections}

    solver = object.__new__(solving.Solver)
    solver.unit = unit
    solver.latest_result = None
    monkeypatch.setattr(solver, "solve", lambda **_: _in_tolerance_result())
    return solver


@pytest.mark.parametrize("phase", ["sky", "spec", "guiding"])
def test_solve_and_correct_leaves_corrections_to_the_acquisition(monkeypatch, fake_filer, tmp_path, phase):
    folder = tmp_path / phase
    imager_settings = ImagerSettings(seconds=1, binning=1, image_path=str(folder / "seq=0001.fits"))

    achieved = _solver(monkeypatch, phase).solve_and_correct(
        target=Coord(ra=Angle(TARGET_RA_HOURS * u.hourangle), dec=Angle(TARGET_DEC_DEGS * u.deg)),
        approach_mode=ApproachMode.DISCRETE_STEP,
        solver_id=SolverId.PlaneWaveCli,
        make_corrections=True,
        imager_settings=imager_settings,
        solving_tolerance=SolvingTolerance(Angle(TOLERANCE_ARCSEC * u.arcsec), Angle(TOLERANCE_ARCSEC * u.arcsec)),
        phase=phase,
        max_tries=1,
    )

    assert achieved
    assert not (folder / "corrections.json").exists()
    moved = [call.args[0] for call in fake_filer.move_ram_to_shared.call_args_list]
    assert not [path for path in moved if path.endswith("corrections.json")]
