"""focus_on_star's run: what it refuses, where it leaves the focuser, and what it never writes.

No hardware.  The steps that drive hardware or the catalog (`_select_star`, `_goto`,
`_acquire`, `_probe`, `_sweep`, `_move_focuser`) are replaced on the run class, so what is
tested is the sequence around them: the outcome, the focuser's final position, the
activity flag, and that the config DB is never written.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import focus_on_star.session as session_mod
from calibration.analysis.models import HFDAutofocusResult, HFDAutofocusStatus
from common.activities import UnitActivities
from common.config.calibration import CalibrationConfig, FocuserCalibration
from common.config.rois import FcuVersion
from focus_on_star.session import FocusOnStarParams, FocusOnStarRun, FocusOnStarSession
from focus_on_star.star_selection import FocusStar

START = 20000


class FakeUnit:
    """Enough of a Unit: real activity bookkeeping, mocked components."""

    def __init__(self, *, focus_product=True):
        self.activities = UnitActivities.Idle
        for name in ("mount", "pw", "imager", "stage", "solver", "acquirer", "guider"):
            setattr(self, name, MagicMock(name=name))
        self.imager.camera_x_size, self.imager.camera_y_size = 8288, 5644
        self.focuser = SimpleNamespace(position=START)
        self.required_focuser = self.focuser
        self.required_imager = self.imager
        self.required_mount = self.mount
        self.required_pw = self.pw
        self.fcu_version = FcuVersion.v2
        calibration = CalibrationConfig()
        if focus_product:
            calibration.products.focuser = FocuserCalibration(best_position=START, timestamp="t")
        self.unit_conf = SimpleNamespace(
            calibration=calibration,
            guiding=SimpleNamespace(rois={FcuVersion.v2: SimpleNamespace(fiber_x=4100, fiber_y=2750)}),
            imager=SimpleNamespace(pixel_scale_at_bin1=0.2616),
        )

    def is_active(self, flag):
        return bool(self.activities & flag)

    def start_activity(self, flag):
        self.activities |= flag

    def end_activity(self, flag):
        self.activities &= ~flag

    @property
    def activities_verbal(self):
        return str(self.activities)


def _status(best=None):
    result = HFDAutofocusResult(
        has_solution=best is not None, best_focus_position=best, best_focus_star_diameter=None if best is None else 9.0
    )
    return HFDAutofocusStatus(message="test", analysis_result=result)


@pytest.fixture
def scripted(monkeypatch):
    """Replace the hardware steps; return the list of focuser moves and a knob for the analysis."""
    moves: list[int] = []
    script = {"analysis": _status(best=20040), "sweep": [(1.0, None)] * 5}

    star = FocusStar(
        ra_deg=90.0, dec_deg=30.0, mag=10.1, separation_deg=0.1, search_radius_deg=0.5, n_candidates=9, n_rejected=0
    )
    monkeypatch.setattr(FocusOnStarRun, "_sky_position", lambda self, params: (6.0, 30.0))
    monkeypatch.setattr(FocusOnStarRun, "_select_star", lambda self, *a: star)
    monkeypatch.setattr(FocusOnStarRun, "_position_stage", lambda self: None)
    monkeypatch.setattr(FocusOnStarRun, "_goto", lambda self, ra, dec: True)
    monkeypatch.setattr(FocusOnStarRun, "_acquire", lambda self, star, ls: True)
    monkeypatch.setattr(FocusOnStarRun, "_probe", lambda self, st, position, box, ls, folder: st)
    monkeypatch.setattr(FocusOnStarRun, "_sweep", lambda self, seed, st, folder: script["sweep"])

    def move(self, position, st):
        moves.append(position)
        self.unit.focuser.position = position

    monkeypatch.setattr(FocusOnStarRun, "_move_focuser", move)

    def persist(*a, **k):
        raise AssertionError("focus_on_star must never persist a calibration product")

    monkeypatch.setattr(FocusOnStarRun, "_persist", persist)
    monkeypatch.setattr(session_mod, "analyze_focus_samples", lambda samples, **kw: script["analysis"])
    config = MagicMock()
    monkeypatch.setattr("calibration.phases.focuser.Config", config)
    return moves, script, config


def _run(unit):
    session = FocusOnStarSession(unit)
    unit.start_activity(UnitActivities.FocusingOnStar)
    session.do_focus_on_star(FocusOnStarParams(), folder=None)
    return session.state


def test_a_solved_run_leaves_the_focuser_at_best_focus_and_writes_nothing(scripted):
    moves, _, config = scripted
    unit = FakeUnit()
    state = _run(unit)
    assert state.outcome == "solved"
    assert moves == [20040]
    assert state.start_position == START and state.best_position == 20040 and state.final_position == 20040
    assert not config.mock_calls  # no Config().set_unit / update_unit
    assert state.focus_center == (4100.0, 2750.0)
    x0, x1, y0, y1 = state.box
    assert (x1 - x0, y1 - y0) == (829, 564)
    assert not unit.is_active(UnitActivities.FocusingOnStar)
    unit.mount.stop_tracking.assert_not_called()  # a solved run keeps tracking the star


def test_an_unsolved_run_returns_the_focuser_to_where_it_was(scripted):
    moves, script, _ = scripted
    script["analysis"] = _status(best=None)
    unit = FakeUnit()
    state = _run(unit)
    assert state.outcome == "failed"
    assert moves == [START]
    assert state.final_position == START
    assert not unit.is_active(UnitActivities.FocusingOnStar)
    unit.mount.stop_tracking.assert_called()


def test_an_implausibly_broad_best_focus_is_not_taken(scripted):
    moves, script, _ = scripted
    broad = _status(best=20040)
    broad.analysis_result.best_focus_star_diameter = 80.0  # > max_best_hfd_px
    script["analysis"] = broad
    state = _run(FakeUnit())
    assert state.outcome == "failed"
    assert moves == [START]


def test_an_abort_during_the_sweep_leaves_the_focuser_alone(scripted):
    moves, script, _ = scripted
    script["sweep"] = None  # _sweep returns None when the flag was cleared
    unit = FakeUnit()
    state = _run(unit)
    assert state.outcome == "aborted"
    assert moves == []
    assert not unit.is_active(UnitActivities.FocusingOnStar)


def test_an_exception_in_the_run_still_ends_the_flag(scripted, monkeypatch):
    def boom(self, star, ls):
        raise RuntimeError("solver exploded")

    monkeypatch.setattr(FocusOnStarRun, "_acquire", boom)
    unit = FakeUnit()
    state = _run(unit)
    assert state.outcome == "failed"
    assert any("solver exploded" in e for e in state.errors)
    assert not unit.is_active(UnitActivities.FocusingOnStar)


@pytest.mark.parametrize(
    ("params", "reason"),
    [
        (FocusOnStarParams(zenith=True, ra_j2000_hours=6.0, dec_j2000_degs=30.0), "zenith=true"),
        (FocusOnStarParams(zenith=False, ra_j2000_hours=6.0), "both"),
        (FocusOnStarParams(steps=4), "odd"),
        (FocusOnStarParams(steps=1), "odd"),
        (FocusOnStarParams(fov_percent=0), "fov_percent"),
    ],
)
def test_bad_parameters_are_refused(params, reason):
    assert reason in FocusOnStarSession(FakeUnit()).require_can_start(params)


def test_a_unit_without_a_focus_calibration_is_refused():
    refusal = FocusOnStarSession(FakeUnit(focus_product=False)).require_can_start(FocusOnStarParams())
    assert "/calibrate/focuser" in refusal


def test_a_busy_unit_is_refused():
    unit = FakeUnit()
    unit.start_activity(UnitActivities.Guiding)
    assert "busy" in FocusOnStarSession(unit).require_can_start(FocusOnStarParams())


def test_a_missing_fibre_pixel_is_refused():
    unit = FakeUnit()
    unit.fcu_version = FcuVersion.v1
    assert "fiber_x" in FocusOnStarSession(unit).require_can_start(FocusOnStarParams())


def test_abort_clears_the_flag():
    unit = FakeUnit()
    unit.start_activity(UnitActivities.FocusingOnStar)
    FocusOnStarSession(unit).abort()
    assert not unit.is_active(UnitActivities.FocusingOnStar)
