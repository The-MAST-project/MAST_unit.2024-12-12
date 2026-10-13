"""The ZWO backend's cooler: a settable set point, cooler on at startup, off at shutdown.

The set point used to be a hard-coded -5 written by `startup()`; now the `Imager` wrapper
applies `imager.cooler_set_point` through `set_cooler_set_point` just before, and holds
`CoolingDown` until the camera gets there. Shutdown switches the cooler off (the `TargetTemp =
10` that preceded it did nothing once the cooler was off), and ends the cool-down.

The backend is never constructed (that opens the camera through the ASI SDK); it runs on a
bare instance with `set_control` recorded and the activity methods stubbed.
"""

from __future__ import annotations

import importlib

import pytest

from common import asi
from common.activities import ImagerActivities

zwo = importlib.import_module("zwo")


class _Parent:
    def __init__(self):
        self.ended = 0

    def end_cooling_down(self):
        self.ended += 1


def _zwo(fail_on=None):
    class _Zwo(zwo.ZWOImager):
        def set_control(self, control, value):
            self.sent.append((control, value))
            if control == fail_on:
                raise RuntimeError("SDK refused")

        def __del__(self):
            """Not the real one: it closes a camera this instance never opened."""

    backend = object.__new__(_Zwo)
    backend.cam_id = 0
    backend.errors = []
    backend.sent = []
    backend.parent_imager = _Parent()
    flags: set = set()
    backend.is_active = lambda activity: activity in flags
    backend.start_activity = lambda activity, **_: flags.add(activity)
    backend.end_activity = lambda activity, **_: flags.discard(activity)
    return backend


def test_it_can_set_the_set_point_in_whole_degrees():
    backend = _zwo()

    assert backend.can_set_cooler_set_point is True
    backend.set_cooler_set_point(5.4)

    assert backend.sent == [(asi.Control.TargetTemp, 5)]


def test_startup_switches_the_cooler_on_and_no_longer_hard_codes_the_target():
    backend = _zwo()

    assert backend.startup().succeeded
    assert backend.sent == [(asi.Control.CoolerOn, True)]


def test_shutdown_ends_the_cool_down_and_switches_the_cooler_off():
    backend = _zwo()

    assert backend.shutdown().succeeded
    assert backend.parent_imager.ended == 1
    assert backend.sent == [(asi.Control.CoolerOn, False)]
    assert not backend.is_active(ImagerActivities.ShuttingDown)


def test_a_refused_cooler_off_still_ends_shutting_down():
    backend = _zwo(fail_on=asi.Control.CoolerOn)

    with pytest.raises(RuntimeError):
        backend.shutdown()

    assert not backend.is_active(ImagerActivities.ShuttingDown)


@pytest.mark.parametrize("attribute", ["temperature", "cooler_power"])
def test_a_failed_read_is_none_and_recorded_not_unbound(monkeypatch, attribute):
    """`val` was never assigned on a failed read, so these raised UnboundLocalError."""

    def refuse(*_a, **_k):
        raise RuntimeError("camera gone")

    monkeypatch.setattr(zwo.zwoasi, "getControlValue", refuse)
    backend = _zwo()

    assert getattr(backend, attribute) is None
    assert backend.errors
