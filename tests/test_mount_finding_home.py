"""When `Mount.ontimer` ends `FindingHome`: the homing trajectory done, and the axes stopped.

Recorded on mast00, 2026-10-12, sampling PWI4's /status every 0.25 s through a startup:
homing sweeps each axis out and back at up to 20 deg/s for ~11 s. `is_slewing` stays False
throughout, and no boolean or string field marks the end -- so `not is_slewing` (with a 5 s
grace) ended FindingHome 4.5 s early, axis 0 still at -20 deg/s. What does mark it: each
axis's setpoint velocity returns to exactly 0, and its measured velocity settles below
0.01 deg/s. `TestTheRecordedHoming` replays that run.
"""

from __future__ import annotations

import datetime
import logging
from types import SimpleNamespace

import pytest

from common.activities import MountActivities, Timing
from mount import FINDING_HOME_CEILING_SECONDS, FINDING_HOME_GRACE_SECONDS, Mount

AT_REST = (0.0, 0.0, 0.0, 0.0)  # setpoint0, measured0, setpoint1, measured1


def _status(axes=AT_REST, is_slewing: bool = False):
    sp0, v0, sp1, v1 = axes

    def axis(setpoint, measured):
        return SimpleNamespace(
            rms_error_arcsec=0.0, setpoint_velocity_degs_per_sec=setpoint, measured_velocity_degs_per_sec=measured
        )

    return SimpleNamespace(mount=SimpleNamespace(is_slewing=is_slewing, axis0=axis(sp0, v0), axis1=axis(sp1, v1)))


class Stub:
    """Enough of a Mount to run `ontimer`."""

    def __init__(self, homing_for_seconds: float, axes=AT_REST, is_slewing: bool = False):
        self.unit = SimpleNamespace(unit_shutdown_event=SimpleNamespace(is_set=lambda: False))
        self.connected = True
        self.is_moving = False
        self.target = "Home"
        self.activities = MountActivities(0)
        self.timings: dict = {}
        self._warned_no_axis_velocities = False
        self.status = _status(axes, is_slewing)
        self.pw = SimpleNamespace(status=lambda: self.status)
        self.start_activity(MountActivities.StartingUp)
        self.start_activity(MountActivities.FindingHome)
        self.timings[MountActivities.FindingHome].start_time -= datetime.timedelta(seconds=homing_for_seconds)

    def start_activity(self, activity, details=None):
        self.activities |= activity
        self.timings[activity] = Timing()

    def end_activity(self, activity):
        self.activities &= ~activity

    def is_active(self, activity) -> bool:
        return bool(self.activities & activity)

    def _end_finding_home_when_homed(self, status):
        return Mount._end_finding_home_when_homed(self, status)  # type: ignore[arg-type]

    def _end_abort_when_at_rest(self, status):
        return Mount._end_abort_when_at_rest(self, status)  # type: ignore[arg-type]

    def tick(self, axes=None, is_slewing=None):
        if axes is not None or is_slewing is not None:
            self.status = _status(axes or AT_REST, bool(is_slewing))
        Mount.ontimer(self)  # type: ignore[arg-type]

    @property
    def homing(self) -> bool:
        return self.is_active(MountActivities.FindingHome)


def test_not_ended_within_the_grace_period_even_at_rest():
    """Right after `find_home` the axes have not started yet: at rest is not homed."""
    stub = Stub(homing_for_seconds=1.2)

    stub.tick()

    assert stub.homing
    assert stub.is_active(MountActivities.StartingUp)


def test_ended_with_startingup_once_at_rest_after_the_grace():
    stub = Stub(homing_for_seconds=FINDING_HOME_GRACE_SECONDS + 1)

    stub.tick()

    assert not stub.homing
    assert not stub.is_active(MountActivities.StartingUp)
    assert stub.target is None


@pytest.mark.parametrize(
    ("label", "axes"),
    [
        ("axis 0 still commanded", (-20.0, -19.9, 0.0, 0.0)),
        ("axis 1 still commanded", (0.0, 0.0, 19.98, 19.9)),
        ("trajectory done, axis 0 still decelerating", (0.0, -4.877, 0.0, 0.002)),
        ("trajectory done, just above the threshold", (0.0, 0.0, 0.0, 0.0101)),
    ],
)
def test_not_ended_while_any_axis_is_commanded_or_moving(label, axes):
    stub = Stub(homing_for_seconds=FINDING_HOME_GRACE_SECONDS + 1, axes=axes)

    stub.tick()

    assert stub.homing, label


def test_not_ended_while_pwi4_says_slewing():
    stub = Stub(homing_for_seconds=FINDING_HOME_GRACE_SECONDS + 1, is_slewing=True)

    stub.tick()

    assert stub.homing


class TestTheRecordedHoming:
    """mast00 2026-10-12, one tick per 2 s, as `ontimer` would have seen it. FindingHome began
    at recorder +2.7 s; each tuple is (recorder seconds, setpoint0, measured0, setpoint1, measured1)."""

    STARTED_AT = 2.7
    TICKS = [
        (7.26, -6.316, -1.487, 0.000, 3.307),  # axis 0 reversing, axis 1 still settling
        (9.14, -20.000, -20.029, 0.000, 0.005),  # the old rule ended FindingHome near here
        (11.30, 0.000, -4.877, 0.000, 0.002),  # trajectory just finished, axis 0 decelerating
        (11.56, 0.000, -2.114, 0.000, -0.002),
        (13.47, 0.000, -0.004, 0.000, -0.000),  # at rest
    ]

    def test_it_ends_when_the_mount_comes_to_rest_and_not_before(self):
        stub = Stub(homing_for_seconds=0)
        ended_at = None
        for at, sp0, v0, sp1, v1 in self.TICKS:
            stub.timings[MountActivities.FindingHome].start_time = datetime.datetime.now(datetime.UTC) - datetime.timedelta(
                seconds=at - self.STARTED_AT
            )
            stub.tick(axes=(sp0, v0, sp1, v1))
            if not stub.homing and ended_at is None:
                ended_at = at

        assert ended_at == 13.47, f"ended at recorder +{ended_at}s"


def test_without_axis_velocities_it_falls_back_to_is_slewing_and_warns_once(caplog):
    stub = Stub(homing_for_seconds=FINDING_HOME_GRACE_SECONDS + 1, axes=(None, None, None, None), is_slewing=True)

    with caplog.at_level(logging.WARNING):
        stub.tick()
        stub.tick()
    assert stub.homing
    assert caplog.text.count("reports no axis velocities") == 1

    stub.tick(axes=(None, None, None, None), is_slewing=False)
    assert not stub.homing


def test_past_the_ceiling_it_ends_regardless_with_an_error(caplog):
    stub = Stub(homing_for_seconds=FINDING_HOME_CEILING_SECONDS + 1, axes=(0.004, 0.004, 0.0, 0.0))

    with caplog.at_level(logging.ERROR):
        stub.tick()

    assert not stub.homing
    assert "FindingHome not at rest after" in caplog.text
