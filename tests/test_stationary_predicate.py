"""`is_stationary` answers the question it was written for (#150, #163).

It was False for every deque ever built: `deque.count` is a *method*, so
`latest_positions.count == latest_positions.maxlen` compares a bound method to an int. The
focuser's deque was never appended to either, so even the repaired comparison would have had
nothing to read.

That mattered twice. It switched off the "stopped short of target, nudge it again" recovery in
both components, and it is the predicate #163 needs: PWI4 keeps `focuser.is_moving` true
indefinitely after `focuser_stop()`, so position stability is the only honest answer to "has it
come to rest?".

Runs on any platform since #52 stubbed the hardware imports; before that it could only have run
on a unit.
"""

from __future__ import annotations

from collections import deque

import pytest

from common.activities import FocuserActivities


class _Recorder:
    def __init__(self, active=()):
        self.active = set(active)

    def is_active(self, activity):
        return activity in self.active

    def end_activity(self, activity, **kwargs):
        self.active.discard(activity)


def _focuser(positions, active=()):
    from focuser import Focuser

    f = object.__new__(Focuser)
    f.latest_positions = deque(positions, maxlen=3)
    recorder = _Recorder(active)
    f.is_active = recorder.is_active
    f.end_activity = recorder.end_activity
    return f, recorder


def _stage(positions):
    from stage import Stage

    s = object.__new__(Stage)
    s.latest_positions = deque(positions, maxlen=3)
    return s


@pytest.mark.parametrize("component", ["focuser", "stage"])
def test_a_full_deque_of_equal_readings_is_stationary(component):
    subject = _focuser([1000, 1000, 1000])[0] if component == "focuser" else _stage([1000, 1000, 1000])

    assert subject.is_stationary is True


@pytest.mark.parametrize("component", ["focuser", "stage"])
def test_a_moving_component_is_not_stationary(component):
    subject = _focuser([1000, 1100, 1200])[0] if component == "focuser" else _stage([1000, 1100, 1200])

    assert subject.is_stationary is False


@pytest.mark.parametrize("component", ["focuser", "stage"])
def test_a_partly_filled_deque_is_not_yet_stationary(component):
    """Fewer samples than the window is "not known yet", not "at rest" -- the case a cleared
    deque produces at the start of every move."""
    subject = _focuser([1000, 1000])[0] if component == "focuser" else _stage([1000, 1000])

    assert subject.is_stationary is False


def test_an_empty_deque_is_not_stationary():
    assert _focuser([])[0].is_stationary is False


def test_the_focuser_ends_aborting_once_it_stops(monkeypatch):
    """#163: the flag comes down on position stability, not on PWI4's `is_moving`."""
    from focuser import Focuser

    f, recorder = _focuser([1000, 1000, 1000], active={FocuserActivities.Aborting})
    # Everything ontimer touches before the abort check, stubbed to no-ops.
    f.unit = None
    monkeypatch.setattr(type(f), "connected", property(lambda self: True))
    monkeypatch.setattr(type(f), "position", property(lambda self: 1000))
    f.known_as_good_position = 1000
    f.target = None

    Focuser.ontimer(f)

    assert FocuserActivities.Aborting not in recorder.active


def test_the_focuser_holds_aborting_while_it_is_still_moving(monkeypatch):
    from focuser import Focuser

    f, recorder = _focuser([1000, 1100, 1200], active={FocuserActivities.Aborting})
    f.unit = None
    monkeypatch.setattr(type(f), "connected", property(lambda self: True))
    monkeypatch.setattr(type(f), "position", property(lambda self: 1300))
    f.known_as_good_position = 1000
    f.target = None

    Focuser.ontimer(f)

    assert FocuserActivities.Aborting in recorder.active
