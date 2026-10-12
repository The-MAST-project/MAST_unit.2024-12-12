"""PHD2's startup and shutdown connect and disconnect (and switch the cooler); they never launch
or kill phd2.exe.

`shutdown()` used to terminate phd2.exe and `startup()` did nothing (the constructor had
launched it), so under the opmode lifecycle -- startup -> shutdown -> startup in one process --
the second startup found no PHD2 and the unit stayed non-operational (mast00, 2026-10-12). The
design: the supervisor starts and watches phd2.exe; the unit only connects and disconnects.

The connector is never constructed (`__init__` launches phd2.exe); its methods run on a bare
instance with the PHD2 calls recorded.
"""

from __future__ import annotations

import pytest

import phd2.phd2 as phd2_module
from common.activities import ImagerActivities
from phd2.phd2 import COOLING_TOLERANCE_DEGREES, PHD2Connector


class _Imager:
    """The parent imager: owns `ImagerActivities`, which the cool-down raises and ends."""

    def __init__(self):
        self.activities = ImagerActivities(0)
        self.unit = None

    def is_active(self, activity):
        return bool(self.activities & activity)

    def start_activity(self, activity, **_):
        self.activities |= activity

    def end_activity(self, activity, **_):
        self.activities &= ~activity


class _NoThread:
    """Records the watcher instead of running it; a test calls `started[0]()` itself."""

    started: list = []

    def __init__(self, name=None, target=None, daemon=None):
        self.target = target

    def start(self):
        _NoThread.started.append(self.target)


class _Process:
    def __init__(self):
        self.terminated = False

    def terminate(self):
        self.terminated = True


def _connector(connected: bool, failing: tuple[str, ...] = ()) -> PHD2Connector:
    c = object.__new__(PHD2Connector)
    c._connected = connected
    c.watched_process = _Process()
    c.parent = _Imager()
    c.calls = []
    c.flags = []

    def record(name):
        def step(*_, **__):
            c.calls.append(name)
            if name in failing:
                raise RuntimeError(f"{name} refused")
            if name in ("_await_phd2",):
                c._connected = True
            if name == "disconnect":
                c._connected = False

        step.__name__ = name
        return step

    for name in ("stop_guiding", "_cooler_off", "disconnect_equipment", "disconnect", "_await_phd2", "connect_equipment"):
        setattr(c, name, record(name))
    c.start_activity = lambda activity, **_: c.flags.append(("start", activity.name))
    c.end_activity = lambda activity, **_: c.flags.append(("end", activity.name))
    return c


@pytest.fixture(autouse=True)
def no_cooler(monkeypatch):
    """`cooler_on` is a property whose setter calls PHD2; record it instead."""
    cooler = property(lambda self: None, lambda self, value: self.calls.append(f"cooler={value}"))
    monkeypatch.setattr(PHD2Connector, "cooler_on", cooler)


@pytest.fixture(autouse=True)
def no_thread(monkeypatch):
    _NoThread.started = []
    monkeypatch.setattr(phd2_module.threading, "Thread", _NoThread)


class TestShutdown:
    def test_it_disconnects_and_leaves_phd2_running(self):
        c = _connector(connected=True)

        response = c.shutdown()

        assert response.succeeded
        assert c.calls == ["stop_guiding", "_cooler_off", "disconnect_equipment", "disconnect"]
        assert c.watched_process.terminated is False, "phd2.exe is the supervisor's, not ours to kill"
        assert c.flags == [("start", "ShuttingDown"), ("end", "ShuttingDown")]

    def test_a_failing_step_still_disconnects_and_reports(self):
        c = _connector(connected=True, failing=("stop_guiding",))

        response = c.shutdown()

        assert response.failed
        assert "stop_guiding failed" in response.errors[0]
        assert c.calls == ["stop_guiding", "_cooler_off", "disconnect_equipment", "disconnect"]
        assert ("end", "ShuttingDown") in c.flags

    def test_the_cooler_goes_off_while_phd2_still_has_the_camera(self):
        """Before `disconnect_equipment`: afterwards PHD2 can no longer reach the camera, and
        with the outlets powered after a shutdown it could otherwise cool all day."""
        c = _connector(connected=True)

        c.shutdown()

        assert c.calls.index("_cooler_off") < c.calls.index("disconnect_equipment")

    def test_a_cooler_that_will_not_switch_off_is_reported_and_the_equipment_still_released(self):
        c = _connector(connected=True, failing=("_cooler_off",))

        response = c.shutdown()

        assert response.failed
        assert "_cooler_off failed" in response.errors[0]
        assert c.calls[-2:] == ["disconnect_equipment", "disconnect"]

    def test_not_connected_it_only_closes_the_socket(self):
        c = _connector(connected=False)

        assert c.shutdown().succeeded
        assert c.calls == ["disconnect"]


class TestStartup:
    def test_already_connected_it_does_nothing(self):
        c = _connector(connected=True)

        assert c.startup().succeeded
        assert c.calls == []

    def test_after_a_shutdown_it_reconnects_without_launching(self):
        c = _connector(connected=True)
        c.shutdown()
        c.calls.clear()

        response = c.startup()

        assert response.succeeded
        assert c.calls[:3] == ["_await_phd2", "connect_equipment", "cooler=True"]
        assert c.watched_process.terminated is False

    def test_a_failed_connect_is_reported_not_raised(self):
        c = _connector(connected=False, failing=("_await_phd2",))

        response = c.startup()

        assert response.failed
        assert "could not connect" in response.errors[0]


class TestCoolingDown:
    """The cooler switched on raises `ImagerActivities.CoolingDown` on the imager, and the imager
    is not operational until the sensor is within COOLING_TOLERANCE_DEGREES of PHD2's set point."""

    def _cooling(self, monkeypatch, readings):
        """A connector that has switched its cooler on, its watcher not yet run, and the
        cooler reading `readings` in turn (temperature, set point) -- None for a failed read."""
        c = _connector(connected=True)
        sequence = iter(readings)

        def status(self):
            reading = next(sequence)
            return None if reading is None else {"temperature": reading[0], "setpoint": reading[1], "coolerOn": True}

        monkeypatch.setattr(PHD2Connector, "cooler_status", property(status))
        monkeypatch.setattr(phd2_module.time, "sleep", lambda _s: None)
        c._start_cooling_down()
        return c

    def test_switching_the_cooler_on_raises_cooling_down_and_makes_it_not_operational(self, monkeypatch):
        c = self._cooling(monkeypatch, [])

        assert c.calls == ["cooler=True"]
        assert c.parent.is_active(ImagerActivities.CoolingDown)
        assert c.operational is False

    def test_it_ends_once_within_tolerance_of_the_set_point(self, monkeypatch):
        c = self._cooling(monkeypatch, [(12.0, -10.0), None, (-8.5, -10.0), (-10.0 + COOLING_TOLERANCE_DEGREES, -10.0)])

        _NoThread.started[0]()

        assert not c.parent.is_active(ImagerActivities.CoolingDown)
        assert c.operational is True
        assert c._cooling_reading == (-10.0 + COOLING_TOLERANCE_DEGREES, -10.0)

    def test_while_cooling_why_not_operational_gives_the_reading(self, monkeypatch):
        c = self._cooling(monkeypatch, [])
        c._cooling_reading = (3.2, -10.0)
        monkeypatch.setattr(PHD2Connector, "name", property(lambda self: "phd2"))

        assert c.why_not_operational == ["phd2: cooling down, 3.2 C, set point -10.0 C"]

    def test_a_disconnect_ends_it(self, monkeypatch):
        c = self._cooling(monkeypatch, [])
        c._connected = False

        _NoThread.started[0]()

        assert not c.parent.is_active(ImagerActivities.CoolingDown)

    def test_switching_the_cooler_off_ends_it(self, monkeypatch):
        c = self._cooling(monkeypatch, [])
        monkeypatch.setattr(PHD2Connector, "call", lambda self, *a, **k: None)

        PHD2Connector._cooler_off(c)

        assert not c.parent.is_active(ImagerActivities.CoolingDown)

    def test_a_second_cooler_on_does_not_start_a_second_watcher(self, monkeypatch):
        c = self._cooling(monkeypatch, [])

        c._start_cooling_down()

        assert len(_NoThread.started) == 1

    def test_an_unreachable_set_point_warns_once_and_keeps_waiting(self, monkeypatch, caplog):
        monkeypatch.setattr(phd2_module, "COOLING_SLOW_WARNING_SECONDS", -1.0)
        c = self._cooling(monkeypatch, [(5.0, -20.0)] * 3 + [None])
        polls = iter(range(3))

        def sleep(_s):
            if next(polls, None) is None:
                c._connected = False  # stop the loop after three polls

        monkeypatch.setattr(phd2_module.time, "sleep", sleep)

        with caplog.at_level("WARNING"):
            _NoThread.started[0]()

        assert caplog.text.count("still cooling") == 1
