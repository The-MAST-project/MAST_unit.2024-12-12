"""The solving guider's loop: it waits out its cadence each cycle, solves under its own activity, closes its exposure
series once, answers a status, and does not leave the unit looking busy if a solve raises."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import solving_guider as sg
from common.activities import UnitActivities
from common.models.statuses import PHD2GuiderStatus

CADENCE = 5.0


class FakeUnit:
    """Just what `SolvingGuider.start_guiding` touches; `solve` runs the test's script for each cycle."""

    def __init__(self, cycles: int, solve_seconds: float = 0.0):
        self.active: set[UnitActivities] = set()
        self.cycles = cycles
        self.calls: list[dict] = []
        self.clock = [100.0]
        self.solve_seconds = solve_seconds
        self.corrections_saved: list[str] = []
        self.series = SimpleNamespace(series_id="s1")
        self.imager = MagicMock()
        self.imager.start_exposure_series.return_value = self.series
        self.guider = MagicMock()
        self.guider.make_guiding_settings.return_value = "settings"
        acquisition = SimpleNamespace(
            folder="/f/acq",
            target_ra=3.0,
            target_dec=20.0,
            approach_mode="m",
            solver_id="s",
            make_corrections=True,
            save_corrections=self.corrections_saved.append,
        )
        self.acquirer = SimpleNamespace(latest_acquisition=acquisition)
        guiding = SimpleNamespace(cadence_seconds=CADENCE, tolerance=SimpleNamespace(ra_arcsec=1.0, dec_arcsec=1.0))
        self.unit_conf = SimpleNamespace(guiding=guiding)
        self.solver = SimpleNamespace(solve_and_correct=self._solve)

    def _solve(self, **kw):
        self.calls.append(kw)
        self.clock[0] += self.solve_seconds
        if len(self.calls) >= self.cycles:
            self.active.discard(UnitActivities.Guiding)  # the loop's stop condition
        return True

    def start_activity(self, activity):
        self.active.add(activity)

    def end_activity(self, activity):
        self.active.discard(activity)

    def is_active(self, activity) -> bool:
        return activity in self.active


@pytest.fixture
def guider(monkeypatch):
    def make(unit: FakeUnit) -> sg.SolvingGuider:
        sg.SolvingGuider._instance = None
        sg.SolvingGuider._initialized = False
        g = sg.SolvingGuider(unit)
        g.waits: list[float] = []
        monkeypatch.setattr(g._stop, "wait", lambda timeout=None: g.waits.append(timeout) or False)
        monkeypatch.setattr(sg.time, "monotonic", lambda: unit.clock[0])
        return g

    yield make
    sg.SolvingGuider._instance = None
    sg.SolvingGuider._initialized = False


def test_it_waits_out_the_cadence_after_every_solve(guider):
    unit = FakeUnit(cycles=3, solve_seconds=1.5)
    g = guider(unit)

    g.start_guiding()

    assert len(unit.calls) == 3
    assert g.waits == [pytest.approx(CADENCE - 1.5)] * 3  # one wait per cycle, not one after the loop ended


def test_a_cycle_longer_than_the_cadence_is_not_slept_on(guider):
    unit = FakeUnit(cycles=2, solve_seconds=CADENCE + 1.0)
    g = guider(unit)

    g.start_guiding()

    assert len(unit.calls) == 2 and g.waits == []


def test_no_cadence_means_no_wait(guider):
    unit = FakeUnit(cycles=2)
    unit.unit_conf.guiding.cadence_seconds = 0
    g = guider(unit)

    g.start_guiding()

    assert len(unit.calls) == 2 and g.waits == []


def test_the_solve_runs_under_the_guiding_activity(guider):
    unit = FakeUnit(cycles=1)
    guider(unit).start_guiding()

    assert unit.calls[0]["parent_activity"] is UnitActivities.Guiding  # was Acquiring, never active while guiding
    assert unit.calls[0]["phase"] == "guiding" and unit.calls[0]["imager_settings"] == "settings"
    unit.guider.make_guiding_settings.assert_called_once_with("/f/acq/guiding")


def test_the_exposure_series_has_a_purpose_and_ends_once(guider):
    unit = FakeUnit(cycles=1)
    g = guider(unit)

    g.start_guiding()
    g.stop_guiding()  # the Guider wrapper stops it as well; the series must not be ended twice

    unit.imager.start_exposure_series.assert_called_once_with(purpose="guiding (solving)")
    unit.imager.end_exposure_series.assert_called_once_with(unit.series)
    assert g.guiding_exposure_series is None
    assert unit.corrections_saved == ["guiding"]


def test_stop_guiding_wakes_the_wait_and_ends_the_activity(guider):
    unit = FakeUnit(cycles=99)
    g = guider(unit)
    unit.active.add(UnitActivities.Guiding)

    g.stop_guiding()

    assert g._stop.is_set() and not unit.is_active(UnitActivities.Guiding)


def test_a_solve_that_raises_does_not_leave_the_unit_guiding(guider):
    unit = FakeUnit(cycles=99)
    g = guider(unit)

    def boom(**kw):
        raise RuntimeError("the solver fell over")

    unit.solver.solve_and_correct = boom
    with pytest.raises(RuntimeError, match="fell over"):
        g.start_guiding()

    assert not unit.is_active(UnitActivities.Guiding)
    unit.imager.end_exposure_series.assert_called_once_with(unit.series)
    assert unit.corrections_saved == ["guiding"]


def test_it_has_a_status_for_the_guider_wrapper(guider):
    unit = FakeUnit(cycles=1)
    g = guider(unit)

    stopped = g.guider_status()
    unit.active.add(UnitActivities.Guiding)
    guiding = g.status()

    assert isinstance(stopped, PHD2GuiderStatus) and stopped.identifier == "solving"
    assert (stopped.is_guiding, stopped.app_state) == (False, "Stopped")
    assert (guiding.is_guiding, guiding.app_state, guiding.is_settling) == (True, "Guiding", False)
