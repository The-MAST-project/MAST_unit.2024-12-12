"""The acquisition's published outcome, and the handover to guiding that decides it.

`FullUnitStatus.acquisition` is how MAST_control's multi-unit acquisition learns whether a
unit made it to guiding (mast-claude-config plans/control_acquisition_design.md, section 5).
"Made it" means locked on a star -- PHD2's state exactly "Guiding" -- not merely told to
guide.

Needs no hardware: the guider, mount and unit are stand-ins, and the waits do not sleep.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import acquirer as acquirer_module
from acquirer import ACQUISITION_CANCELLED, GUIDING_NEVER_LOCKED, Acquirer
from common.activities import UnitActivities
from common.canonical import CanonicalResponse, CanonicalResponse_Ok
from common.models.latest import AcquisitionState, LatestAcquisition
from guiding import Guider
from phd2.phd2 import PHD2Connector


@pytest.fixture(autouse=True)
def _no_waiting_and_no_folders(monkeypatch):
    monkeypatch.setattr(acquirer_module.time, "sleep", lambda seconds: None)

    class _Guardian:
        def release_folder(self, *args, **kwargs):
            pass

    monkeypatch.setattr(acquirer_module, "MoveGuardian", _Guardian)


def _phd2(app_state: str = "Stopped", connected: bool = True) -> PHD2Connector:
    """A PHD2Connector holding just the state `is_guiding` / `is_locked` read."""
    phd2 = object.__new__(PHD2Connector)
    phd2._connected = connected
    phd2.conn = SimpleNamespace(is_connected=lambda: connected)
    phd2.app_state = app_state
    phd2.avg_dist = 0.0
    return phd2


class _Guider:
    """Answers `start_guiding`, then reports the unit's scripted PHD2 state as `is_locked`."""

    def __init__(self, unit: _Unit, response: CanonicalResponse):
        self._backend = object.__new__(PHD2Connector)
        self.unit = unit
        self.response = response

    def start_guiding(self) -> CanonicalResponse:
        if self.response.succeeded:
            self.unit.start_activity(UnitActivities.Guiding)
        return self.response

    @property
    def is_locked(self) -> bool:
        return self.unit.phd2_state == "Guiding"


class _Unit:
    """Plays out a guiding session, one scripted PHD2 state per pass of the wait loop.

    Each `is_active(Guiding)` -- the loop's condition -- moves to the next state. When the
    script runs out, guiding stops, as `stop_guiding()` would stop it. `"<cancelled>"`
    ends `Acquiring` and then `Guiding`, as `stop_acquisition_and_guiding()` does.
    """

    def __init__(self, script: list[str]):
        self.active: set[UnitActivities] = {UnitActivities.Acquiring}
        self.script = list(script)
        self.phd2_state = "Stopped"
        self.observed: list[str] = []  # the states the loop actually saw
        self.mount = SimpleNamespace(stop_tracking=lambda: None)
        self.required_mount = self.mount
        self.imager = None
        self.required_guider: _Guider | None = None

    def is_active(self, activity) -> bool:
        if activity == UnitActivities.Guiding and activity in self.active:
            if not self.script:
                self.end_activity(UnitActivities.Guiding)
            elif self.script[0] == "<cancelled>":
                self.script.pop(0)
                self.end_activity(UnitActivities.Acquiring)
                self.end_activity(UnitActivities.Guiding)
            else:
                self.phd2_state = self.script.pop(0)
                self.observed.append(self.phd2_state)
        return activity in self.active

    def start_activity(self, activity) -> None:
        self.active.add(activity)

    def end_activity(self, activity) -> None:
        self.active.discard(activity)


def _handover(response: CanonicalResponse, script: list[str]):
    unit = _Unit(script)
    unit.required_guider = _Guider(unit, response)
    acquirer = object.__new__(Acquirer)
    acquirer.unit = unit
    outcome = LatestAcquisition(state=AcquisitionState.RUNNING).begin()
    failure = acquirer._hand_over_to_guider("test", outcome)
    return failure, outcome, unit


# -- the handover -----------------------------------------------------------------------


def test_a_lock_makes_the_outcome_guiding_and_guiding_ending_is_not_a_failure():
    failure, outcome, unit = _handover(CanonicalResponse_Ok, ["Calibrating", "Guiding", "LostLock", "Guiding"])

    assert failure is None
    assert unit.observed == ["Calibrating", "Guiding", "LostLock", "Guiding"]
    # GUIDING from the first lock; a later LostLock does not take it back.
    assert outcome.state == AcquisitionState.GUIDING
    assert UnitActivities.Acquiring not in unit.active


def test_still_calibrating_is_still_running():
    outcome = LatestAcquisition(state=AcquisitionState.RUNNING).begin()
    unit = _Unit(["Calibrating", "Calibrating"])
    seen: list[AcquisitionState] = []

    class _Watching(_Guider):
        @property
        def is_locked(self) -> bool:
            seen.append(outcome.state)
            return super().is_locked

    unit.required_guider = _Watching(unit, CanonicalResponse_Ok)
    acquirer = object.__new__(Acquirer)
    acquirer.unit = unit
    acquirer._hand_over_to_guider("test", outcome)

    assert seen == [AcquisitionState.RUNNING, AcquisitionState.RUNNING]


def test_calibrating_and_lost_lock_are_not_a_lock():
    failure, outcome, _ = _handover(CanonicalResponse_Ok, ["Calibrating", "LostLock", "Selected"])

    assert failure == GUIDING_NEVER_LOCKED
    assert outcome.state == AcquisitionState.RUNNING  # run_acquisition settles it to FAILED


def test_a_refused_handover_is_a_failure_not_a_success():
    # The MAST_unit bug this fixes: PHD2 refused, returned errors without raising
    # Guiding, the wait fell through, and the acquisition reported success.
    refusal = CanonicalResponse(errors=["requested_binning=2 does not match self.profile_binning=1, cannot guide"])

    failure, outcome, unit = _handover(refusal, [])

    assert failure is not None
    assert failure.startswith("handover to guiding failed: ")
    assert "cannot guide" in failure
    assert outcome.state == AcquisitionState.RUNNING
    assert UnitActivities.Acquiring not in unit.active  # unwound like a failed phase


def test_a_cancellation_before_a_lock_is_a_cancellation():
    failure, _, unit = _handover(CanonicalResponse_Ok, ["Calibrating", "<cancelled>"])

    assert failure == ACQUISITION_CANCELLED
    assert not unit.active


# -- the outcome's lifecycle ------------------------------------------------------------


@pytest.fixture
def no_thread(monkeypatch):
    started: list = []

    class _Thread:
        def __init__(self, name, target, args):
            started.append((target, args))

        def start(self):
            pass

    monkeypatch.setattr(acquirer_module, "Thread", _Thread)
    return started


def test_start_publishes_a_fresh_running_outcome_before_the_thread_runs(no_thread):
    acquirer = object.__new__(Acquirer)
    previous = LatestAcquisition(state=AcquisitionState.FAILED, errors=["old"]).begin()
    acquirer.latest = previous
    acquisition = SimpleNamespace(folder="unused", failure=None)

    acquirer.start(acquisition)

    assert acquirer.latest is not previous
    assert acquirer.latest.state == AcquisitionState.RUNNING
    assert acquirer.latest.errors == []
    assert acquisition.latest is acquirer.latest
    assert len(no_thread) == 1  # and the run was handed to a thread, not run here


@pytest.mark.parametrize(
    ("failure", "state"),
    [
        (None, AcquisitionState.ENDED),
        (ACQUISITION_CANCELLED, AcquisitionState.ABORTED),
        ("phase 'sky' did not reach tolerance after 3 tries", AcquisitionState.FAILED),
        (GUIDING_NEVER_LOCKED, AcquisitionState.FAILED),
    ],
)
def test_run_acquisition_settles_the_outcome(monkeypatch, failure, state):
    acquirer = object.__new__(Acquirer)
    monkeypatch.setattr(acquirer, "do_acquire", lambda acquisition: failure, raising=False)
    outcome = LatestAcquisition(state=AcquisitionState.GUIDING).begin()
    acquisition = SimpleNamespace(folder="unused", failure=None, latest=outcome)

    acquirer.run_acquisition(acquisition)

    assert outcome.state == state
    assert outcome.ended is not None
    assert outcome.errors == ([] if failure is None else [failure])


def test_an_acquisition_that_raises_settles_as_failed(monkeypatch):
    acquirer = object.__new__(Acquirer)

    def boom(acquisition):
        raise RuntimeError("the solver fell over")

    monkeypatch.setattr(acquirer, "do_acquire", boom, raising=False)
    outcome = LatestAcquisition(state=AcquisitionState.RUNNING).begin()
    acquisition = SimpleNamespace(folder="unused", failure=None, latest=outcome)

    acquirer.run_acquisition(acquisition)

    assert outcome.state == AcquisitionState.FAILED
    assert "the solver fell over" in outcome.errors[0]


def test_a_refused_request_leaves_the_previous_outcome_alone(no_thread):
    acquirer = object.__new__(Acquirer)
    acquirer.unit = SimpleNamespace(mount=None, guider=object(), imager=object(), solver=object(), stage=object())
    previous = LatestAcquisition(state=AcquisitionState.ENDED).begin()
    acquirer.latest = previous

    response = acquirer.endpoint_start_acquisition_and_guiding(ra_j2000_hours="1", dec_j2000_degs="2")

    assert response.failed
    assert acquirer.latest is previous
    assert not no_thread


# -- the guider -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("app_state", "locked", "guiding"),
    [
        ("Guiding", True, True),
        ("LostLock", False, True),
        ("Calibrating", False, False),
        ("Selected", False, False),
        ("Looping", False, False),
        ("Paused", False, False),
        ("Stopped", False, False),
    ],
)
def test_phd2_is_locked_only_when_guiding(app_state, locked, guiding):
    phd2 = _phd2(app_state)

    assert phd2.is_locked is locked
    assert phd2.is_guiding is guiding


def test_phd2_disconnected_is_not_locked():
    assert _phd2("Guiding", connected=False).is_locked is False


def test_guider_start_guiding_passes_the_backends_refusal_on():
    guider = object.__new__(Guider)
    refusal = CanonicalResponse(errors=["not connected"])
    guider._backend = SimpleNamespace(start_guiding=lambda: refusal)

    assert guider.start_guiding() is refusal


def test_guider_start_guiding_without_a_backend_is_refused():
    guider = object.__new__(Guider)
    guider._backend = None

    assert guider.start_guiding().failed
