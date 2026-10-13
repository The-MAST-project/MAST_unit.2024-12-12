"""What the unit publishes about its latest acquisition, and the cancel a solve honors (MAST_unit#326)."""

from __future__ import annotations

from astropy.coordinates import Angle

import acquisition as acquisition_module
from acquisition import Acquisition, ApproachMode
from common.activities import UnitActivities
from common.interfaces.solving import SolverId, SolvingTolerance
from common.models.statuses import ImagerSettings
from common.utils import Coord
from solving import Solver


def _acquisition(folder: str) -> Acquisition:
    a = object.__new__(Acquisition)
    a.folder = folder
    a.target_ra = 5.5
    a.target_dec = -12.0
    a.succeeded = False
    a.failure = "the acquisition was cancelled during phase 'SPEC'"
    return a


def test_the_record_is_published_with_the_share_folder(monkeypatch, tmp_path):
    """The filer's roots are POSIX-spelled on a unit, and the folder comes from pathlib."""
    from common.filer import FilerTop

    ram, shared = tmp_path / "ram", tmp_path / "shared"

    def change_top_to(top, path):
        # As the real one does: a plain prefix test against a posix-spelled root.
        assert top is FilerTop.Shared
        return path.replace(ram.as_posix(), shared.as_posix()) if path.startswith(ram.as_posix()) else None

    monkeypatch.setattr(acquisition_module.filer, "change_top_to", change_top_to)
    ram_folder = str(ram / "2026-10-13" / "Acquisitions" / "seq=0001")  # backslashes on Windows

    status = _acquisition(ram_folder).status()

    assert status.folder == (shared / "2026-10-13" / "Acquisitions" / "seq=0001").as_posix()
    assert status.target_ra_j2000_hours == 5.5
    assert status.target_dec_j2000_degs == -12.0
    assert status.succeeded is False
    assert status.failure == "the acquisition was cancelled during phase 'SPEC'"


class _Unit:
    def __init__(self) -> None:
        self.activities: set = set()
        self.acquirer = None

    def start_activity(self, activity, **kwargs) -> None:
        self.activities.add(activity)

    def end_activity(self, activity, **kwargs) -> None:
        self.activities.discard(activity)

    def is_active(self, activity) -> bool:
        return activity in self.activities


def test_a_cancelled_solve_ends_solving():
    """Left raised, `Solving` keeps the unit busy and an abort never settles."""
    solver = object.__new__(Solver)
    solver.unit = _Unit()

    achieved = Solver.solve_and_correct(
        solver,
        target=Coord(ra=Angle(5.5, unit="hour"), dec=Angle(-12.0, unit="deg")),
        approach_mode=ApproachMode.GRADUAL_BY_RATE,
        solver_id=SolverId.PlaneWaveCli,
        make_corrections=True,
        imager_settings=ImagerSettings(seconds=1, binning=1, save=False),
        solving_tolerance=SolvingTolerance(Angle(1, unit="arcsec"), Angle(1, unit="arcsec")),
        phase="spec",
        cancelled=lambda: True,
    )

    assert achieved is False
    assert not solver.unit.is_active(UnitActivities.Solving)
