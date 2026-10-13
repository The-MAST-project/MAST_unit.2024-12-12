"""An acquisition records how it ended, and `Acquiring` says when it has (MAST_unit#316, #326).

`Acquiring` clears on every exit -- converged, a phase out of tolerance, cancelled -- so the flag
alone cannot say which. Flux metering read it as success and walked the spiral from an
unconverged pointing (MAST_unit#316). The flag is raised before the request returns and cleared
only once the outcome is recorded, so a caller can wait on it and then read the record (#326).

Needs no hardware: every path here returns before touching a component.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from acquirer import ACQUISITION_CANCELLED, REQUIRED_COMPONENTS, Acquirer
from common.activities import UnitActivities
from common.canonical import CanonicalResponse

SHARE_FOLDER = "Z:/MAST/mast01/2026-10-13/Acquisitions/seq=0001"


class _Unit:
    def __init__(self, missing=()):
        for name in REQUIRED_COMPONENTS:
            setattr(self, name, None if name in missing else object())
        self.errors: list[str] = []
        self.reference_image = None
        self.activities: set = set()

    def start_activity(self, activity, **kwargs) -> None:
        self.activities.add(activity)

    def end_activity(self, activity, **kwargs) -> None:
        self.activities.discard(activity)

    def is_active(self, activity) -> bool:
        return activity in self.activities


def _acquirer(missing=()):
    a = object.__new__(Acquirer)
    a.unit = _Unit(missing)
    a.latest_acquisition = None
    a._cancel = threading.Event()
    return a


def _acquisition(failure=None):
    return SimpleNamespace(folder="unused", share_folder=SHARE_FOLDER, failure=failure, succeeded=None)


@pytest.fixture(autouse=True)
def _no_folder_release(monkeypatch):
    import acquirer as acquirer_module

    class _Guardian:
        def release_folder(self, *args, **kwargs):
            pass

    monkeypatch.setattr(acquirer_module, "MoveGuardian", _Guardian)


def test_do_acquire_returns_why_it_did_not_acquire():
    a = _acquirer(missing={"solver"})

    failure = Acquirer.do_acquire(a, acquisition=None)

    assert failure is not None
    assert failure.endswith("these components did not initialize: solver")


def test_run_acquisition_records_the_failure_on_the_acquisition():
    a = _acquirer(missing={"stage"})
    acquisition = _acquisition(failure="stale")

    Acquirer.run_acquisition(a, acquisition)

    assert acquisition.failure is not None
    assert "stage" in acquisition.failure


def test_run_acquisition_records_success_as_no_failure(monkeypatch):
    a = _acquirer()
    monkeypatch.setattr(a, "do_acquire", lambda acquisition: None, raising=False)
    acquisition = _acquisition(failure="stale")

    Acquirer.run_acquisition(a, acquisition)

    assert acquisition.failure is None


def test_an_acquisition_that_raises_is_recorded_as_failed(monkeypatch):
    a = _acquirer()

    def boom(acquisition):
        raise RuntimeError("the solver fell over")

    monkeypatch.setattr(a, "do_acquire", boom, raising=False)
    acquisition = _acquisition()

    Acquirer.run_acquisition(a, acquisition)

    assert acquisition.failure is not None
    assert "the solver fell over" in acquisition.failure


def test_prepare_refuses_without_building_an_acquisition():
    a = _acquirer(missing={"mount"})

    prepared = Acquirer.prepare_acquisition(a)

    assert isinstance(prepared, CanonicalResponse)
    assert prepared.errors is not None
    assert "mount" in prepared.errors[0]


def test_launch_raises_acquiring_and_sets_the_record_before_it_returns(monkeypatch):
    """The race behind #316: a poll right after the response must not read "finished"."""
    a = _acquirer()
    release = threading.Event()
    monkeypatch.setattr(a, "do_acquire", lambda acquisition: release.wait(5) and None, raising=False)
    acquisition = _acquisition()

    folder = Acquirer.launch_acquisition(a, acquisition)

    assert folder == SHARE_FOLDER
    assert a.unit.is_active(UnitActivities.Acquiring)
    assert a.latest_acquisition is acquisition
    assert acquisition.succeeded is None

    release.set()
    deadline = time.monotonic() + 5
    while a.unit.is_active(UnitActivities.Acquiring) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not a.unit.is_active(UnitActivities.Acquiring)
    assert acquisition.succeeded is True


def test_acquiring_clears_only_after_the_outcome_is_recorded(monkeypatch):
    a = _acquirer()
    monkeypatch.setattr(a, "do_acquire", lambda acquisition: "phase 'SPEC' did not reach tolerance", raising=False)
    acquisition = _acquisition()
    seen_at_clear = []
    end_activity = a.unit.end_activity

    def spy(activity, **kwargs):
        if activity == UnitActivities.Acquiring:
            seen_at_clear.append(acquisition.succeeded)
        end_activity(activity, **kwargs)

    a.unit.end_activity = spy

    Acquirer.run_acquisition(a, acquisition)

    assert seen_at_clear == [False]


def _raise(acquisition):
    raise RuntimeError("the solver fell over")


@pytest.mark.parametrize(
    ("missing", "do_acquire", "succeeded"),
    [
        ((), lambda acquisition: None, True),
        ((), lambda acquisition: "phase 'SKY' did not reach tolerance in 3 tries", False),
        ((), _raise, False),
        ({"imager"}, None, False),
    ],
    ids=["converged", "out-of-tolerance", "raised", "missing-components"],
)
def test_every_exit_records_an_outcome_and_clears_acquiring(monkeypatch, missing, do_acquire, succeeded):
    a = _acquirer(missing)
    if do_acquire is not None:
        monkeypatch.setattr(a, "do_acquire", do_acquire, raising=False)
    acquisition = _acquisition()

    Acquirer.run_acquisition(a, acquisition)

    assert acquisition.succeeded is succeeded
    assert (acquisition.failure is None) is succeeded
    assert not a.unit.is_active(UnitActivities.Acquiring)


def test_a_cancel_is_recorded_as_one_even_when_the_unwind_raises(monkeypatch):
    """Stopping a solve's exposure can make the solve raise on the frame that never came."""
    a = _acquirer()

    def cancelled_then_raised(acquisition):
        a.cancel()
        raise FileNotFoundError("the frame was never written")

    monkeypatch.setattr(a, "do_acquire", cancelled_then_raised, raising=False)
    acquisition = _acquisition()

    Acquirer.run_acquisition(a, acquisition)

    assert acquisition.succeeded is False
    assert acquisition.failure.startswith(ACQUISITION_CANCELLED)


def test_cancel_stops_a_stage_wait_without_clearing_acquiring():
    """The flag stays up until the run has unwound; the cancel travels on its own signal."""
    a = _acquirer()
    a.unit.required_stage = SimpleNamespace(is_moving=True)
    Acquirer._begin(a, _acquisition())

    a.cancel()

    assert Acquirer._await_stage(a) is False
    assert a.unit.is_active(UnitActivities.Acquiring)


def test_a_new_acquisition_starts_uncancelled():
    a = _acquirer()
    a.cancel()

    Acquirer._begin(a, _acquisition())

    assert not Acquirer._cancelled(a)


def test_a_raise_after_convergence_is_not_read_as_a_cancel(monkeypatch):
    """Stopping guiding is how a guided acquisition ends, and it sets the cancel too."""
    a = _acquirer()

    def converged_guided_then_raised(acquisition):
        acquisition.succeeded = True
        a.cancel()
        raise RuntimeError("the corrections plot fell over")

    monkeypatch.setattr(a, "do_acquire", converged_guided_then_raised, raising=False)
    acquisition = _acquisition()

    Acquirer.run_acquisition(a, acquisition)

    assert acquisition.succeeded is False
    assert acquisition.failure.startswith("the acquisition raised")


class _Guider:
    def __init__(self, unit: _Unit, raises_guiding: bool):
        self.unit = unit
        self.raises_guiding = raises_guiding
        self.stops = 0

    def start_guiding(self) -> None:
        if self.raises_guiding:
            self.unit.start_activity(UnitActivities.Guiding)

    def stop_acquisition_and_guiding(self) -> None:
        self.stops += 1
        self.unit.end_activity(UnitActivities.Guiding)


def _guiding_acquirer(raises_guiding: bool):
    a = _acquirer()
    a.unit.required_guider = _Guider(a.unit, raises_guiding)
    a.unit.required_mount = SimpleNamespace(tracking_stopped=False)
    a.unit.required_mount.stop_tracking = lambda: setattr(a.unit.required_mount, "tracking_stopped", True)
    return a


def test_a_stop_during_the_handover_stops_the_guiding_that_started_after_it():
    """The stop found no `Guiding` to end; waiting on it would hold `Acquiring` up for good."""
    a = _guiding_acquirer(raises_guiding=True)
    a.cancel()

    waiter = threading.Thread(target=Acquirer._guide_until_stopped, args=[a], daemon=True)
    waiter.start()
    waiter.join(timeout=5)

    assert not waiter.is_alive(), "still waiting on a guiding session nothing will stop"
    assert a.unit.required_guider.stops == 1
    assert not a.unit.is_active(UnitActivities.Guiding)
    assert a.unit.required_mount.tracking_stopped


def test_a_handover_that_never_guided_returns_at_once():
    a = _guiding_acquirer(raises_guiding=False)

    Acquirer._guide_until_stopped(a)

    assert a.unit.required_guider.stops == 0
    assert a.unit.required_mount.tracking_stopped
