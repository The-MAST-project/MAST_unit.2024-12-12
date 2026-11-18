"""`Acquiring` is raised before the endpoint answers and cleared once, after the outcome (MAST_unit#326).

Raised on the run's thread, the flag left a window after the PUT in which a status read said "not
acquiring" and `/unit/abort` found nothing to stop. Cleared at `do_acquire`'s exits, it was never
cleared when the run raised: on mast01, 2026-10-25, the unit read busy for nine minutes.

A stop still clears the flag from outside; that is how the run learns it was stopped, and how
its outcome is settled as `ABORTED` whatever its failure text says.

Needs no hardware: the unit is a stand-in that only holds activities.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from test_acquisition_slew_wait import _acquisition
from test_acquisition_slew_wait import _Unit as _AcquiringUnit

import acquirer as acquirer_module
from acquirer import Acquirer
from common.activities import UnitActivities
from common.config.rois import FcuVersion
from common.models.latest import AcquisitionState, LatestAcquisition

STOPPED_MID_PHASE = "the acquisition was cancelled during phase 'SPEC'"
PHASE_FAILED = "phase 'SPEC' did not reach tolerance in 20 tries"


class _Unit:
    """Holds activities, and notes the outcome's state each time `Acquiring` is ended."""

    def __init__(self):
        self.active: set = set()
        self.outcome: LatestAcquisition | None = None
        self.state_when_cleared: list = []

    def start_activity(self, activity, **kwargs):
        self.active.add(activity)

    def end_activity(self, activity, **kwargs):
        if activity == UnitActivities.Acquiring and activity in self.active and self.outcome is not None:
            self.state_when_cleared.append(self.outcome.state)
        self.active.discard(activity)

    def is_active(self, activity):
        return activity in self.active


@pytest.fixture(autouse=True)
def _no_folder_release(monkeypatch):
    class _Guardian:
        def release_folder(self, *args, **kwargs):
            pass

    monkeypatch.setattr(acquirer_module, "MoveGuardian", _Guardian)


def _thread_noting(monkeypatch, unit: _Unit) -> list[bool]:
    """Threads that never run; each notes whether `Acquiring` was raised when it was started."""
    noted: list[bool] = []

    class _Thread:
        def __init__(self, name, target, args):
            pass

        def start(self):
            noted.append(unit.is_active(UnitActivities.Acquiring))

    monkeypatch.setattr(acquirer_module, "Thread", _Thread)
    return noted


def _running(unit: _Unit, do_acquire) -> tuple[Acquirer, SimpleNamespace]:
    """An acquirer about to run, `Acquiring` raised as `start()` leaves it."""
    acquirer = object.__new__(Acquirer)
    acquirer.unit = unit
    acquirer.do_acquire = do_acquire
    outcome = LatestAcquisition(state=AcquisitionState.RUNNING).begin()
    unit.outcome = outcome
    acquirer.begin()
    return acquirer, SimpleNamespace(folder="unused", failure=None, latest=outcome)


def test_acquiring_is_raised_before_the_run_is_started(monkeypatch):
    unit = _Unit()
    acquirer = object.__new__(Acquirer)
    acquirer.unit = unit
    noted = _thread_noting(monkeypatch, unit)

    acquirer.start(SimpleNamespace(folder=None))

    assert noted == [True]


@pytest.mark.parametrize("outcome_of_the_run", ["converged", "failed", "raised"])
def test_acquiring_clears_on_every_exit_after_the_outcome_is_settled(outcome_of_the_run):
    unit = _Unit()

    def do_acquire(acquisition):
        if outcome_of_the_run == "raised":
            raise RuntimeError("save_corrections fell over")
        return None if outcome_of_the_run == "converged" else PHASE_FAILED

    acquirer, acquisition = _running(unit, do_acquire)

    acquirer.run_acquisition(acquisition)

    assert not unit.is_active(UnitActivities.Acquiring)
    assert len(unit.state_when_cleared) == 1
    assert unit.state_when_cleared[0] != AcquisitionState.RUNNING, "cleared before the outcome was settled"


@pytest.mark.parametrize("how_it_ends", [STOPPED_MID_PHASE, "raises"])
def test_a_stopped_run_settles_as_aborted_whatever_its_failure_says(how_it_ends):
    unit = _Unit()

    def do_acquire(acquisition):
        unit.end_activity(UnitActivities.Acquiring)  # the stop
        if how_it_ends == "raises":
            raise RuntimeError("the solver read a frame that never came")
        return how_it_ends

    acquirer, acquisition = _running(unit, do_acquire)

    acquirer.run_acquisition(acquisition)

    assert acquisition.latest.state == AcquisitionState.ABORTED
    assert acquisition.failure is not None


def test_a_phase_that_fails_is_failed_not_aborted(monkeypatch):
    """`do_acquire` itself no longer clears `Acquiring` when a phase fails, so the failure is
    not mistaken for a stop."""
    unit = _AcquiringUnit(FcuVersion.v2)
    acquirer = object.__new__(Acquirer)
    acquirer.unit = unit
    acquirer.begin()

    failure = _acquire_and_settle(acquirer, unit, monkeypatch)

    assert failure is not None and "did not reach tolerance" in failure
    assert UnitActivities.Acquiring in unit.activities, "do_acquire cleared the flag itself"


def _acquire_and_settle(acquirer, unit, monkeypatch):
    monkeypatch.setattr(acquirer, "_cancellable_sleep", lambda seconds: not acquirer._cancelled())
    monkeypatch.setattr(acquirer_module, "time", SimpleNamespace(monotonic=unit.clock.monotonic, sleep=lambda s: None))
    return Acquirer.do_acquire(acquirer, _acquisition())
