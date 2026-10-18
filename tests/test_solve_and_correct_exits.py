"""`Solver.solve_and_correct` ends `Solving` on every exit, and its own-acquisition branch runs.

Entered at the method itself with a fake unit built on `activity_spy`: the real
`Activities.start_activity` sends a UI notification, which reads the bootstrap config the CI
runners do not have. The solve, the imager and the mount are never reached, or are replaced by
a `solve` that raises.
"""

from pathlib import Path
from types import SimpleNamespace

import astropy.units as u
import pytest
from astropy.coordinates import Angle

import solving
from acquisition import ApproachMode
from common.activities import UnitActivities
from common.interfaces.solving import SolverId, SolvingResult, SolvingTolerance
from common.models.statuses import ImagerSettings
from common.utils import Coord
from solving import Solver

TARGET = Coord(ra=Angle(6 * u.hourangle), dec=Angle(30 * u.deg))  # type: ignore
TOLERANCE = SolvingTolerance(Angle(1 * u.arcsecond), Angle(1 * u.arcsecond))  # type: ignore


def _unit(spy, latest_acquisition=None):
    spy.errors = []
    spy.acquirer = SimpleNamespace(latest_acquisition=latest_acquisition)
    return spy


def _solver(unit) -> Solver:
    # object.__new__, not Solver.__new__: Solver is a singleton, and a stub assigned in one
    # test would otherwise stay on the instance every later test gets.
    solver = object.__new__(Solver)
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
    def test_parent_activity_cancelled(self, activity_spy):
        """The parent (Acquiring) is not active, so the first try returns at once."""
        unit = _unit(activity_spy, _acquisition())

        assert _call(_solver(unit)) is False
        assert not unit.is_active(UnitActivities.Solving)

    def test_solve_raises(self, activity_spy):
        unit = _unit(activity_spy, _acquisition())
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

    def test_it_is_built_in_hours_and_degrees(self, activity_spy, monkeypatch):
        built = {}

        def fake_acquisition(**kwargs):
            built.update(kwargs)
            return _acquisition()

        monkeypatch.setattr(solving, "Acquisition", fake_acquisition)
        unit = _unit(activity_spy)

        assert _call(_solver(unit)) is False  # cancelled at once: Acquiring is not active

        assert "conf" not in built
        assert built["target_ra"] == pytest.approx(6.0)
        assert built["target_dec"] == pytest.approx(30.0)
        assert built["exposure"] == 3.0
        assert unit.acquirer.latest_acquisition is not None
        assert "spec" in unit.acquirer.latest_acquisition.corrections

    def test_target_from_the_mount(self, activity_spy, monkeypatch):
        built = {}

        def fake_acquisition(**kwargs):
            built.update(kwargs)
            return _acquisition()

        monkeypatch.setattr(solving, "Acquisition", fake_acquisition)
        unit = _unit(activity_spy)
        status = SimpleNamespace(ra_j2000_hours=12.5, dec_j2000_degs=-10.0)
        unit.required_mount = SimpleNamespace(status=lambda: status)

        _call(_solver(unit), target=None)

        assert built["target_ra"] == pytest.approx(12.5)
        assert built["target_dec"] == pytest.approx(-10.0)


class _Imager:
    """Starts an exposure and 'saves' it: writes the file, or does not (aborted/failed)."""

    def __init__(self, on_saved=None):
        self.on_saved = on_saved
        self.settings = None

    def start_exposure(self, settings):
        self.settings = settings

    def wait_for_image_saved(self):
        if self.on_saved is not None:
            self.on_saved(self.settings)


class _Backend:
    name = "astrometry.net"

    def __init__(self):
        self.calls = 0

    def solve(self, **_):
        self.calls += 1
        return SolvingResult(succeeded=False, errors=["no match"])


def _exposing_solver(spy, imager, monkeypatch) -> tuple[Solver, _Backend]:
    monkeypatch.setattr(solving, "safety_get_sensor", lambda *a, **k: None)
    unit = _unit(spy, _acquisition())
    unit.imager = imager
    solver = _solver(unit)
    backend = _Backend()
    solver._backend = backend  # type: ignore[assignment]
    return solver, backend


class TestNoImageIsNotSolved:
    """`wait_for_image_saved` returns for an aborted or failed exposure too. The backend used to
    open the missing file, and the FileNotFoundError ended focus_on_star as "failed" (mast00,
    2026-10-18)."""

    def test_an_aborted_exposure_is_reported_not_solved(self, activity_spy, monkeypatch, tmp_path):
        solver, backend = _exposing_solver(
            activity_spy, _Imager(on_saved=lambda _: activity_spy.end_activity(UnitActivities.Solving)), monkeypatch
        )
        activity_spy.start_activity(UnitActivities.Solving)

        result = solver.solve(ImagerSettings(seconds=3.0, binning=1, base_folder=str(tmp_path)), TARGET, "spec")

        assert result is not None and not result.succeeded
        assert result.errors == ["the exposure was aborted"]
        assert backend.calls == 0

    def test_a_failed_exposure_is_reported_not_solved(self, activity_spy, monkeypatch, tmp_path):
        solver, backend = _exposing_solver(activity_spy, _Imager(), monkeypatch)
        activity_spy.start_activity(UnitActivities.Solving)

        result = solver.solve(ImagerSettings(seconds=3.0, binning=1, base_folder=str(tmp_path)), TARGET, "spec")

        assert result is not None and result.errors == ["the exposure produced no image"]
        assert backend.calls == 0

    def test_a_saved_image_is_solved(self, activity_spy, monkeypatch, tmp_path):
        def write(settings):
            Path(settings.image_path).parent.mkdir(parents=True, exist_ok=True)
            Path(settings.image_path).write_bytes(b"")

        solver, backend = _exposing_solver(activity_spy, _Imager(on_saved=write), monkeypatch)
        activity_spy.start_activity(UnitActivities.Solving)

        solver.solve(ImagerSettings(seconds=3.0, binning=1, base_folder=str(tmp_path)), TARGET, "spec")

        assert backend.calls == 1

    def test_an_abort_during_the_exposure_ends_the_try_without_writing(self, activity_spy, tmp_path):
        """The parent cancelled mid-exposure: the try ends before its debug JSON is written."""
        unit = _unit(activity_spy, _acquisition())
        unit.start_activity(UnitActivities.Acquiring)
        solver = _solver(unit)
        image = tmp_path / "released" / "frame.fits"

        def aborted(**_):
            unit.end_activity(UnitActivities.Acquiring)
            return SolvingResult(succeeded=False, errors=["the exposure was aborted"])

        solver.solve = aborted  # type: ignore[method-assign]

        assert _call(solver, imager_settings=ImagerSettings(seconds=3.0, binning=1, image_path=str(image))) is False
        assert not (tmp_path / "released" / "frame-solver_result.json").exists()
        assert not unit.is_active(UnitActivities.Solving)
