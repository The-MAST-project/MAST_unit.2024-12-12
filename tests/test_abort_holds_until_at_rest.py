"""Abort holds an `Aborting` activity until the device is at rest (#80).

Two decisions are pinned here: the flag is raised only when there was motion to stop, and it
comes down on the controller's own signal rather than on the stop having been sent.

Windows-only, like the rest of the component suite. The components record flag transitions
instead of using `Activities`, whose `start_activity` publishes a notification and wants real
configuration.
"""

from __future__ import annotations

import pytest

# No platform guard: `conftest` stubs the absent hardware modules, so this runs on a dev
# machine as well as on a unit (#52). Windows keeps the real modules -- only absent ones
# are stubbed -- so nothing here can mask genuine Windows behaviour.
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


class _Response:
    """What `_mirrorcover_command` answers; only `failed` is read on the abort path."""

    failed = False


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


def test_covers_abort_flags_when_they_were_moving():
    from covers import Covers

    recorder = RecordingActivities(active={CoverActivities.Opening})
    covers = _component(Covers, recorder, _mirrorcover_command=lambda verb: _Response())

    covers.abort()

    assert CoverActivities.Opening in recorder.ended
    assert CoverActivities.Aborting in recorder.active


def test_covers_abort_over_idle_covers_flags_nothing():
    from covers import Covers

    recorder = RecordingActivities()
    covers = _component(Covers, recorder, _mirrorcover_command=lambda verb: _Response())

    covers.abort()

    assert CoverActivities.Aborting not in recorder.active


@pytest.mark.parametrize(
    ("state", "still_aborting"),
    [
        (CoversState.Moving, True),
        (CoversState.Open, False),
        (CoversState.Closed, False),
        # Halted between the ends. This is the state an abort actually lands in, and the row
        # that fails against the pre-MAST_unit#164 mapping, which sent it to `Moving`.
        (CoversState.PartlyOpen, False),
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

    covers._end_abort_when_at_rest(state)

    assert (CoverActivities.Aborting in recorder.active) is still_aborting


def test_pwi4_partly_open_is_not_moving():
    """The mapping, not the predicate: PWI4's resting name must not land in `Moving`."""
    from covers import _PWI4_STATE_NAMES, _at_rest

    assert _PWI4_STATE_NAMES["PartlyOpen"] is CoversState.PartlyOpen
    assert _at_rest(_PWI4_STATE_NAMES["PartlyOpen"])
    assert not _at_rest(_PWI4_STATE_NAMES["Opening"])
    assert not _at_rest(_PWI4_STATE_NAMES["Closing"])


def test_pwi4_state_names_cover_every_name_pwi4_emits():
    """An unmapped name falls to `CoversState.Error` loudly; keep the map complete so it does
    not happen for a name PWI4 4.1.6 already sends."""
    from covers import _PWI4_STATE_NAMES

    assert set(_PWI4_STATE_NAMES) == {"Open", "Closed", "Opening", "Closing", "PartlyOpen"}


#: PWI4 4.1.6's `mirrorcover.overall_state` numbering, measured on mast03 2026-09-22
#: (vault: `data/2026-09-22-mast03-mirrorcover-state-names`). 4 was not observed.
PWI4_OVERALL_STATE = {0: "Open", 1: "Closed", 2: "Opening", 3: "Closing", 5: "PartlyOpen"}


def test_a_by_value_cast_of_pwi4s_state_disagrees_with_ours():
    """Pins WHY covers.py maps by name and never by value (#164).

    Casting PWI4's integer into `CoversState` returns a wrong answer instead of raising.
    Two of the five agree, which is exactly what lets the mistake survive a casual test on
    closed or opening covers and then lie on the ones that matter: a CLOSING cover read as
    `Open`, and a HALTED one read as `Error`. If this test ever goes green by accident,
    the agreement is still coincidence -- do not take it as licence to cast.
    """
    from covers import _PWI4_STATE_NAMES

    disagreements = {
        pwi4_int: (pwi4_name, CoversState(pwi4_int).name)
        for pwi4_int, pwi4_name in PWI4_OVERALL_STATE.items()
        if CoversState(pwi4_int) is not _PWI4_STATE_NAMES[pwi4_name]
    }

    assert disagreements == {
        0: ("Open", "NotPresent"),
        3: ("Closing", "Open"),
        5: ("PartlyOpen", "Error"),
    }


def test_every_pwi4_state_name_we_map_is_one_pwi4_actually_emits():
    """The map's keys and the measured numbering must describe the same five states, so a
    name invented on our side cannot sit in the map looking authoritative."""
    from covers import _PWI4_STATE_NAMES

    assert set(_PWI4_STATE_NAMES) == set(PWI4_OVERALL_STATE.values())


@pytest.mark.parametrize(
    ("motion", "lifecycle"),
    [
        (CoverActivities.Opening, CoverActivities.StartingUp),
        (CoverActivities.Closing, CoverActivities.ShuttingDown),
    ],
)
def test_covers_stopped_short_end_their_motion_and_lifecycle_activities(motion, lifecycle):
    """Covers halted between the ends release their waiters instead of holding until a
    terminal state that will never arrive (#164). This is what keeps `powerdown()` from
    waiting out MOVE_TIMEOUT_SECONDS against a cover that has already stopped."""
    from covers import Covers

    recorder = RecordingActivities(active={motion, lifecycle})
    covers = _component(Covers, recorder)

    covers._end_motion_stopped_short(CoversState.PartlyOpen)

    assert motion not in recorder.active
    assert lifecycle not in recorder.active


def test_covers_stopped_short_does_not_claim_the_covers_were_shut():
    """Ending `ShuttingDown` releases the waiter; it must not assert the covers are closed,
    nor power the outlet off -- `powerdown()` owns that, and the covers are not shut."""
    from covers import Covers

    recorder = RecordingActivities(active={CoverActivities.Closing, CoverActivities.ShuttingDown})
    powered_off = []
    covers = _component(
        Covers,
        recorder,
        _was_shut_down=False,
        power_off=lambda: powered_off.append(True),
    )

    covers._end_motion_stopped_short(CoversState.PartlyOpen)

    assert covers._was_shut_down is False
    assert powered_off == []


@pytest.mark.parametrize("state", [CoversState.Closed, CoversState.Open])
def test_covers_not_yet_moving_keep_their_motion_activity(state):
    """The race the mast03 trace exposed: for ~0.3 s after a command PWI4 still reports the
    end state the covers are sitting at, and the covers timer ticks every 2 s. A tick landing
    in that gap must not read "not at the target yet" as "stopped short" and abandon a move
    that is about to begin. Only `PartlyOpen` means stopped between the ends."""
    from covers import Covers

    recorder = RecordingActivities(active={CoverActivities.Opening, CoverActivities.StartingUp})
    covers = _component(Covers, recorder)

    covers._end_motion_stopped_short(state)

    assert CoverActivities.Opening in recorder.active
    assert CoverActivities.StartingUp in recorder.active


# --------------------------------------------------------------------------------- stage


@pytest.mark.parametrize(("is_moving", "still_aborting"), [(True, True), (False, False)])
def test_stage_ends_aborting_when_the_controller_reports_the_move_finished(is_moving, still_aborting):
    """MVCMD_RUNNING, not `is_stationary` -- that predicate is broken (#150)."""
    from stage import Stage

    recorder = RecordingActivities(active={StageActivities.Aborting})
    stage = _component(Stage, recorder, is_moving=is_moving)

    stage._end_abort_when_at_rest()

    assert (StageActivities.Aborting in recorder.active) is still_aborting
