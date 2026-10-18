"""`Solver.solve_and_correct` ends `Solving` on every exit, and its own-acquisition branch runs.

Entered at the method itself with a fake unit: the solve, the imager and the mount are never
reached, or are replaced by a `solve` that raises.
"""

from types import SimpleNamespace

import astropy.units as u
import pytest
from astropy.coordinates import Angle

import solving
from acquisition import ApproachMode
from common.activities import Activities, UnitActivities
from common.interfaces.solving import SolverId, SolvingTolerance
from common.models.statuses import ImagerSettings
from common.utils import Coord
from solving import Solver

TARGET = Coord(ra=Angle(6 * u.hourangle), dec=Angle(30 * u.deg))  # type: ignore
TOLERANCE = SolvingTolerance(Angle(1 * u.arcsecond), Angle(1 * u.arcsecond))  # type: ignore


class _Unit(Activities):
    def __init__(self, latest_acquisition=None):
        super().__init__()
        self.activities = UnitActivities.Idle
        self.errors: list[str] = []
        self.acquirer = SimpleNamespace(latest_acquisition=latest_acquisition)


def _solver(unit) -> Solver:
    solver = Solver.__new__(Solver)
    solver.unit = unit
    return solver


def _call(solver: Solver, **overrides) -> bool:
    kwargs = {
        "target": TARGET,
        "approach_mode": ApproachMode.DISCRETE_STEP,
        "solver_id": SolverId.AstrometryDotNet,
        "make_corrections": True,
        "imager_settings": ImagerSettings(seconds=3.0, binning=1, save=False),
        "solving_tolerance": TOLERANCE,
        "phase": "spec",
        "parent_activity": UnitActivities.Acquiring,
        "max_tries": 3,
    } | overrides
    return solver.solve_and_correct(**kwargs)


def _acquisition():
    return SimpleNamespace(corrections={})


class TestSolvingEndsOnEveryExit:
    def test_parent_activity_cancelled(self):
        """The parent (Acquiring) is not active, so the first try returns at once."""
        unit = _Unit(latest_acquisition=_acquisition())

        assert _call(_solver(unit)) is False
        assert not unit.is_active(UnitActivities.Solving)

    def test_solve_raises(self):
        unit = _Unit(latest_acquisition=_acquisition())
        unit.start_activity(UnitActivities.Acquiring)
        solver = _solver(unit)

        def boom(**_):
            raise RuntimeError("solver backend died")

        solver.solve = boom  # type: ignore[method-assign]
        with pytest.raises(RuntimeError):
            _call(solver)

        assert not unit.is_active(UnitActivities.Solving)
        assert not unit.is_active(UnitActivities.Correcting)
        assert unit.is_active(UnitActivities.Acquiring), "the parent activity is the caller's"


class TestOwnAcquisition:
    """With no `latest_acquisition`, the method builds one. It used to raise TypeError."""

    def test_it_is_built_in_hours_and_degrees(self, monkeypatch):
        built = {}

        def fake_acquisition(**kwargs):
            built.update(kwargs)
            return _acquisition()

        monkeypatch.setattr(solving, "Acquisition", fake_acquisition)
        unit = _Unit(latest_acquisition=None)

        assert _call(_solver(unit)) is False  # cancelled at once: Acquiring is not active

        assert "conf" not in built
        assert built["target_ra"] == pytest.approx(6.0)
        assert built["target_dec"] == pytest.approx(30.0)
        assert built["exposure"] == 3.0
        assert unit.acquirer.latest_acquisition is not None
        assert "spec" in unit.acquirer.latest_acquisition.corrections

    def test_target_from_the_mount(self, monkeypatch):
        built = {}

        def fake_acquisition(**kwargs):
            built.update(kwargs)
            return _acquisition()

        monkeypatch.setattr(solving, "Acquisition", fake_acquisition)
        unit = _Unit(latest_acquisition=None)
        status = SimpleNamespace(ra_j2000_hours=12.5, dec_j2000_degs=-10.0)
        unit.required_mount = SimpleNamespace(status=lambda: status)

        _call(_solver(unit), target=None)

        assert built["target_ra"] == pytest.approx(12.5)
        assert built["target_dec"] == pytest.approx(-10.0)
