"""When `Mount.ontimer` ends `FindingHome`: PWI4 not slewing, and not in the first seconds.

Homing does not raise the axes' RMS error that `is_moving` reads, so ending on `not is_moving`
ended `FindingHome` -- and `StartingUp` with it -- on the first tick, 1.2 s into a homing that
went on (mast00, 2026-10-11). PWI4 reports no homing state, only `is_slewing`, and right after
`find_home` it may not be slewing yet: hence FINDING_HOME_GRACE_SECONDS.
"""

from __future__ import annotations

import datetime
from types import SimpleNamespace

from common.activities import MountActivities, Timing
from mount import FINDING_HOME_GRACE_SECONDS, Mount


def _status(is_slewing: bool):
    axis = SimpleNamespace(rms_error_arcsec=0.0)
    return SimpleNamespace(mount=SimpleNamespace(is_slewing=is_slewing, axis0=axis, axis1=axis))


class Stub:
    """Enough of a Mount to run `ontimer`."""

    def __init__(self, is_slewing: bool, homing_for_seconds: float):
        self.unit = SimpleNamespace(unit_shutdown_event=SimpleNamespace(is_set=lambda: False))
        self.connected = True
        self.is_moving = False
        self.target = "Home"
        self.activities = MountActivities(0)
        self.timings: dict = {}
        self.pw = SimpleNamespace(status=lambda: _status(is_slewing))
        self.start_activity(MountActivities.StartingUp)
        self.start_activity(MountActivities.FindingHome)
        started = self.timings[MountActivities.FindingHome]
        started.start_time -= datetime.timedelta(seconds=homing_for_seconds)

    def start_activity(self, activity, details=None):
        self.activities |= activity
        self.timings[activity] = Timing()

    def end_activity(self, activity):
        self.activities &= ~activity

    def is_active(self, activity) -> bool:
        return bool(self.activities & activity)

    def _end_abort_when_at_rest(self, status):
        return Mount._end_abort_when_at_rest(self, status)  # type: ignore[arg-type]


def test_not_ended_within_the_grace_period_even_when_not_slewing():
    stub = Stub(is_slewing=False, homing_for_seconds=1.2)

    Mount.ontimer(stub)  # type: ignore[arg-type]

    assert stub.is_active(MountActivities.FindingHome)
    assert stub.is_active(MountActivities.StartingUp)


def test_not_ended_while_pwi4_is_slewing():
    stub = Stub(is_slewing=True, homing_for_seconds=FINDING_HOME_GRACE_SECONDS + 30)

    Mount.ontimer(stub)  # type: ignore[arg-type]

    assert stub.is_active(MountActivities.FindingHome)


def test_ended_with_startingup_once_past_the_grace_period_and_not_slewing():
    stub = Stub(is_slewing=False, homing_for_seconds=FINDING_HOME_GRACE_SECONDS + 1)

    Mount.ontimer(stub)  # type: ignore[arg-type]

    assert not stub.is_active(MountActivities.FindingHome)
    assert not stub.is_active(MountActivities.StartingUp)
    assert stub.target is None
