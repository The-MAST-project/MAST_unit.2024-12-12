"""PHD2's startup and shutdown connect and disconnect; they never launch or kill phd2.exe.

`shutdown()` used to terminate phd2.exe and `startup()` did nothing (the constructor had
launched it), so under the opmode lifecycle -- startup -> shutdown -> startup in one process --
the second startup found no PHD2 and the unit stayed non-operational (mast00, 2026-10-12). The
design: the supervisor starts and watches phd2.exe; the unit only connects and disconnects.

The connector is never constructed (`__init__` launches phd2.exe); its methods run on a bare
instance with the PHD2 calls recorded.
"""

from __future__ import annotations

import pytest

from phd2.phd2 import PHD2Connector


class _Process:
    def __init__(self):
        self.terminated = False

    def terminate(self):
        self.terminated = True


def _connector(connected: bool, failing: tuple[str, ...] = ()) -> PHD2Connector:
    c = object.__new__(PHD2Connector)
    c._connected = connected
    c.watched_process = _Process()
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

    for name in ("stop_guiding", "disconnect_equipment", "disconnect", "_await_phd2", "connect_equipment"):
        setattr(c, name, record(name))
    c.start_activity = lambda activity, **_: c.flags.append(("start", activity.name))
    c.end_activity = lambda activity, **_: c.flags.append(("end", activity.name))
    return c


@pytest.fixture(autouse=True)
def no_cooler(monkeypatch):
    """`cooler_on` is a property whose setter calls PHD2; record it instead."""
    cooler = property(lambda self: None, lambda self, value: self.calls.append(f"cooler={value}"))
    monkeypatch.setattr(PHD2Connector, "cooler_on", cooler)


class TestShutdown:
    def test_it_disconnects_and_leaves_phd2_running(self):
        c = _connector(connected=True)

        response = c.shutdown()

        assert response.succeeded
        assert c.calls == ["stop_guiding", "disconnect_equipment", "disconnect"]
        assert c.watched_process.terminated is False, "phd2.exe is the supervisor's, not ours to kill"
        assert c.flags == [("start", "ShuttingDown"), ("end", "ShuttingDown")]

    def test_a_failing_step_still_disconnects_and_reports(self):
        c = _connector(connected=True, failing=("stop_guiding",))

        response = c.shutdown()

        assert response.failed
        assert "stop_guiding failed" in response.errors[0]
        assert c.calls == ["stop_guiding", "disconnect_equipment", "disconnect"]
        assert ("end", "ShuttingDown") in c.flags

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
        assert c.calls == ["_await_phd2", "connect_equipment", "cooler=True"]
        assert c.watched_process.terminated is False

    def test_a_failed_connect_is_reported_not_raised(self):
        c = _connector(connected=False, failing=("_await_phd2",))

        response = c.startup()

        assert response.failed
        assert "could not connect" in response.errors[0]
