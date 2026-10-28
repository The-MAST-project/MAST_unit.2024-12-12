"""The imager's cooler: the configured set point, and `CoolingDown` until the camera reaches it.

One implementation in the `Imager` wrapper, for every backend:

- `startup` writes `imager.cooler_set_point` where the backend can set a target (ZWO; ASCOM
  when the driver allows), and compares it with the camera's where it cannot (PHD2, whose set
  point comes from its profile), warning on a mismatch;
- switching the cooler on raises `ImagerActivities.CoolingDown`, and the imager is not
  operational until the sensor is within COOLING_TOLERANCE_DEGREES of the set point.

The wrapper is never constructed (that powers outlets and launches a backend); it runs on a
bare instance over a fake backend, with the activity methods recorded rather than notified.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import imagers as imagers_module
from common.activities import ImagerActivities
from common.canonical import CanonicalResponse_Ok
from imagers import COOLING_TOLERANCE_DEGREES, Imager


class FakeBackend:
    def __init__(self, can_set: bool = True, set_point: float = 5.0, readings=(), set_raises: bool = False):
        self.can_set_cooler_set_point = can_set
        self.connected = True
        self.cooler_on = True
        self.cooler_power = 100.0
        self.operational = True
        self.why_not_operational: list[str] = []
        self.calls: list = []
        self._set_point = set_point
        self._readings = iter(readings)
        self._set_raises = set_raises

    @property
    def set_point(self):
        return self._set_point

    @property
    def temperature(self):
        reading = next(self._readings)
        if isinstance(reading, Exception):
            raise reading
        return reading

    def set_cooler_set_point(self, degrees):
        self.calls.append(("set_cooler_set_point", degrees))
        if self._set_raises:
            raise RuntimeError("SDK refused")
        self._set_point = degrees

    def startup(self):
        self.calls.append(("startup",))
        return CanonicalResponse_Ok


class _NoThread:
    started: list = []

    def __init__(self, name=None, target=None, daemon=None):
        self.target = target

    def start(self):
        _NoThread.started.append(self.target)


@pytest.fixture(autouse=True)
def no_thread_no_sleep(monkeypatch):
    _NoThread.started = []
    monkeypatch.setattr(imagers_module.threading, "Thread", _NoThread)
    monkeypatch.setattr(imagers_module.time, "sleep", lambda _s: None)
    monkeypatch.setattr(imagers_module, "log_usb_link", lambda: None)


def _imager(backend: FakeBackend, cooler_set_point: float | None = 5.0) -> Imager:
    imager = object.__new__(Imager)
    imager._backend = backend
    imager.backend_type = "phd2"
    imager.unit = SimpleNamespace(
        unit_conf=SimpleNamespace(imager=SimpleNamespace(cooler_set_point=cooler_set_point, temp_check_interval=10))
    )
    flags: set = set()
    imager.is_active = lambda activity: activity in flags
    imager.start_activity = lambda activity, **_: flags.add(activity)
    imager.end_activity = lambda activity, **_: flags.discard(activity)
    return imager


class TestTheSetPoint:
    def test_a_backend_that_can_set_it_gets_the_configured_value_before_it_starts(self):
        backend = FakeBackend(can_set=True)
        imager = _imager(backend, cooler_set_point=5.0)

        imager.startup()

        assert backend.calls[:2] == [("set_cooler_set_point", 5.0), ("startup",)]

    def test_it_is_read_live_so_a_config_edit_applies_at_the_next_startup(self):
        backend = FakeBackend(can_set=True)
        imager = _imager(backend, cooler_set_point=5.0)
        imager.unit.unit_conf.imager.cooler_set_point = -3.0

        imager.startup()

        assert ("set_cooler_set_point", -3.0) in backend.calls

    def test_a_backend_that_cannot_set_it_is_not_asked_and_a_mismatch_is_warned(self, caplog):
        backend = FakeBackend(can_set=False, set_point=-10.0)
        imager = _imager(backend, cooler_set_point=5.0)

        with caplog.at_level("WARNING"):
            imager.startup()

        assert not any(c[0] == "set_cooler_set_point" for c in backend.calls)
        assert "cannot set it -- update the phd2 profile" in caplog.text

    def test_no_warning_when_the_profile_agrees_within_tolerance(self, caplog):
        backend = FakeBackend(can_set=False, set_point=5.0 + COOLING_TOLERANCE_DEGREES / 2)
        imager = _imager(backend, cooler_set_point=5.0)

        with caplog.at_level("WARNING"):
            imager.startup()

        assert "update the" not in caplog.text

    def test_a_refused_set_point_is_logged_and_startup_carries_on(self, caplog):
        backend = FakeBackend(can_set=True, set_raises=True)
        imager = _imager(backend)

        with caplog.at_level("ERROR"):
            response = imager.startup()

        assert response.succeeded
        assert ("startup",) in backend.calls
        assert "could not apply cooler set point" in caplog.text


class TestCoolingDown:
    def test_startup_with_the_cooler_on_raises_it_and_the_imager_is_not_operational(self):
        imager = _imager(FakeBackend())

        imager.startup()

        assert imager.is_active(ImagerActivities.CoolingDown)
        assert imager.operational is False
        assert imager.why_not_operational == ["imager: cooling down to the set point"]

    def test_it_ends_once_within_tolerance_and_the_imager_is_operational(self):
        readings = [22.0, RuntimeError("read failed"), 8.0, 5.0 + COOLING_TOLERANCE_DEGREES]
        backend = FakeBackend(set_point=5.0, readings=readings)
        imager = _imager(backend)
        imager.start_cooling_down()

        _NoThread.started[0]()

        assert not imager.is_active(ImagerActivities.CoolingDown)
        assert imager.operational is True
        assert imager._cooling_reading == (5.0 + COOLING_TOLERANCE_DEGREES, 5.0)

    def test_the_reading_is_reported_while_cooling(self):
        imager = _imager(FakeBackend())
        imager.start_cooling_down()
        imager._cooling_reading = (12.3, 5.0)

        assert imager.why_not_operational == ["imager: cooling down, 12.3 C, set point 5.0 C"]

    def test_a_disconnect_ends_it(self):
        backend = FakeBackend()
        imager = _imager(backend)
        imager.start_cooling_down()
        backend.connected = False

        _NoThread.started[0]()

        assert not imager.is_active(ImagerActivities.CoolingDown)

    def test_before_the_backend_exists_it_does_nothing(self):
        """A backend switching its cooler on during its own construction runs inside
        `Imager.__init__`, before `_backend` is assigned (mast00, 2026-10-13)."""
        imager = _imager(FakeBackend())
        del imager._backend

        imager.start_cooling_down()

        assert not imager.is_active(ImagerActivities.CoolingDown)
        assert _NoThread.started == []

    def test_starting_it_twice_runs_one_watcher(self):
        imager = _imager(FakeBackend())

        imager.start_cooling_down()
        imager.start_cooling_down()

        assert len(_NoThread.started) == 1

    def test_an_unreachable_set_point_is_logged_degraded_once_and_keeps_waiting(self, monkeypatch, caplog):
        monkeypatch.setattr(imagers_module, "COOLING_SLOW_WARNING_SECONDS", -1.0)
        backend = FakeBackend(set_point=-30.0, readings=[10.0] * 3)
        imager = _imager(backend)
        imager.start_cooling_down()
        polls = iter(range(3))

        def sleep(_s):
            if next(polls, None) is None:
                backend.connected = False  # stop after three polls

        monkeypatch.setattr(imagers_module.time, "sleep", sleep)

        with caplog.at_level("WARNING"):
            _NoThread.started[0]()

        assert caplog.text.count("DEGRADED cooler=unreached temp=10.0 set-point=-30.0") == 1

    def test_the_cooler_endpoints_start_and_end_it(self):
        backend = FakeBackend()
        imager = _imager(backend)

        imager.turn_cooler_on()
        assert imager.is_active(ImagerActivities.CoolingDown)

        imager.turn_cooler_off()
        assert not imager.is_active(ImagerActivities.CoolingDown)
        assert backend.cooler_on is False
