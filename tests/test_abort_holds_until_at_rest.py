"""Abort holds an `Aborting` activity until the device is at rest (#80).

Two decisions are pinned here: the flag is raised only when there was motion to stop, and it
comes down on the controller's own signal rather than on the stop having been sent.

Windows-only, like the rest of the component suite. The components record flag transitions
instead of using `Activities`, whose `start_activity` publishes a notification and wants real
configuration.
"""

from __future__ import annotations

import pytest

pytest.importorskip("win32com", reason="the unit's component modules are Windows-only")

from common.activities import (
    CoverActivities,
    FocuserActivities,
    MountActivities,
    StageActivities,
)
from common.models.statuses import CoversState


class RecordingActivities:
    """Stands in for the `Activities` mixin, recording transitions instead of notifying."""

    def __init__(self, active=()):
        self.active = set(active)
        self.started: list = []
        self.ended: list = []

    def is_active(self, activity):
        return activity in self.active

    def start_activity(self, activity, **kwargs):
        self.active.add(activity)
        self.started.append(activity)

    def end_activity(self, activity, **kwargs):
        self.active.discard(activity)
        self.ended.append(activity)


class FakePw:
    """The PWI4 client surface the abort paths touch."""

    def __init__(self, focuser_is_moving=False):
        self.calls: list[str] = []
        self._focuser_is_moving = focuser_is_moving

    def mount_stop(self):
        self.calls.append("mount_stop")

    def mount_tracking_off(self):
        self.calls.append("mount_tracking_off")

    def focuser_stop(self):
        self.calls.append("focuser_stop")


def _component(cls, recorder, **attributes):
    """A component that runs its real methods over recorded flags and fake hardware."""
    component = object.__new__(cls)
    for name in ("is_active", "start_activity", "end_activity"):
        setattr(component, name, getattr(recorder, name))
    for name, value in attributes.items():
        setattr(component, name, value)
    return component


# --------------------------------------------------------------------------------- mount


def test_mount_abort_raises_aborting_and_then_stops():
    from mount import Mount

    recorder = RecordingActivities(active={MountActivities.Slewing})
    pw = FakePw()
    mount = _component(Mount, recorder, pw=pw)

    mount.abort()

    assert MountActivities.Aborting in recorder.active
    assert MountActivities.Slewing in recorder.ended
    assert pw.calls == ["mount_stop", "mount_tracking_off"]


class FakeMountStatus:
    def __init__(self, is_slewing):
        self.mount = type("_Mount", (), {"is_slewing": is_slewing})()


@pytest.mark.parametrize(
    ("is_moving", "is_slewing", "still_aborting"),
    [
        (True, True, True),
        (True, False, True),  # residual servo motion after a non-slew operation
        (False, True, True),  # PWI4 still reports the commanded slew
        (False, False, False),  # at rest on both signals -- and only then
    ],
)
def test_mount_ends_aborting_only_when_both_signals_say_at_rest(is_moving, is_slewing, still_aborting):
    from mount import Mount

    recorder = RecordingActivities(active={MountActivities.Aborting})
    mount = _component(Mount, recorder, is_moving=is_moving)

    mount._end_abort_when_at_rest(FakeMountStatus(is_slewing))

    assert (MountActivities.Aborting in recorder.active) is still_aborting


# ------------------------------------------------------------------------------- focuser


def test_focuser_abort_flags_and_stops_when_it_was_moving():
    from focuser import Focuser

    recorder = RecordingActivities(active={FocuserActivities.Moving})
    pw = FakePw()
    focuser = _component(Focuser, recorder, pw=pw)

    focuser.abort()

    assert FocuserActivities.Moving in recorder.ended
    assert FocuserActivities.Aborting in recorder.active
    assert pw.calls == ["focuser_stop"]


def test_focuser_abort_over_an_idle_focuser_flags_nothing():
    """A flag raised with nothing to stop would clear on the next tick and mean nothing."""
    from focuser import Focuser

    recorder = RecordingActivities()
    pw = FakePw()
    focuser = _component(Focuser, recorder, pw=pw)

    focuser.abort()

    assert FocuserActivities.Aborting not in recorder.active
    assert pw.calls == []


# -------------------------------------------------------------------------------- covers


def test_covers_abort_flags_when_they_were_moving(monkeypatch):
    import covers as covers_module
    from covers import Covers

    monkeypatch.setattr(covers_module, "ascom_run", lambda *args, **kwargs: type("_R", (), {"failed": False})())

    recorder = RecordingActivities(active={CoverActivities.Opening})
    covers = _component(Covers, recorder)

    covers.abort()

    assert CoverActivities.Opening in recorder.ended
    assert CoverActivities.Aborting in recorder.active


def test_covers_abort_over_idle_covers_flags_nothing(monkeypatch):
    import covers as covers_module
    from covers import Covers

    monkeypatch.setattr(covers_module, "ascom_run", lambda *args, **kwargs: type("_R", (), {"failed": False})())

    recorder = RecordingActivities()
    covers = _component(Covers, recorder)

    covers.abort()

    assert CoverActivities.Aborting not in recorder.active


@pytest.mark.parametrize(
    ("state", "still_aborting"),
    [
        (CoversState.Moving, True),
        (CoversState.Open, False),
        (CoversState.Closed, False),
        # Error and Unknown are equally not-in-motion: the abort is over either way, and the
        # fault is the covers' own problem to report.
        (CoversState.Error, False),
        (CoversState.Unknown, False),
    ],
)
def test_covers_end_aborting_on_any_state_but_moving(state, still_aborting):
    from covers import Covers

    recorder = RecordingActivities(active={CoverActivities.Aborting})
    covers = _component(Covers, recorder)
    type(covers).state = property(lambda self, value=state: value)
    try:
        covers._end_abort_when_at_rest()
    finally:
        del type(covers).state

    assert (CoverActivities.Aborting in recorder.active) is still_aborting


# --------------------------------------------------------------------------------- stage


@pytest.mark.parametrize(("is_moving", "still_aborting"), [(True, True), (False, False)])
def test_stage_ends_aborting_when_the_controller_reports_the_move_finished(is_moving, still_aborting):
    """MVCMD_RUNNING, not `is_stationary` -- that predicate is broken (#150)."""
    from stage import Stage

    recorder = RecordingActivities(active={StageActivities.Aborting})
    stage = _component(Stage, recorder, is_moving=is_moving)

    stage._end_abort_when_at_rest()

    assert (StageActivities.Aborting in recorder.active) is still_aborting
