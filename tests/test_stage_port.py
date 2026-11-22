"""Freeing the stage's COM port from XILab before the scan (mast08, 2026-11-22).

XILab, connected, holds the XIMC controller's port: libximc then enumerates 0 devices and cannot
open it.  `release_xilab` ends XILab when the port is held and XILab is what is running, waits for
it and for the port, and leaves any other holder alone.
"""

from __future__ import annotations

from types import SimpleNamespace

import stage_port as sp


class Held:
    """A port that is held until ``frees_after`` checks have been made (None: forever)."""

    def __init__(self, held_now=True, frees_after=None):
        self.checks = 0
        self.held_now = held_now
        self.frees_after = frees_after

    def __call__(self, port):
        self.checks += 1
        if not self.held_now:
            return False
        return self.frees_after is None or self.checks <= self.frees_after


def _xilab(pid=4956):
    return SimpleNamespace(pid=pid, create_time=lambda: 0.0, username=lambda: "MAST08\\mast")


def _run(held, procs, alive=()):
    killed: list[list] = []
    slept: list[float] = []

    def kill(found):
        killed.append(list(found))
        return list(alive)

    ok = sp.release_xilab(["COM7"], held=held, find=lambda: procs, kill=kill, sleep=slept.append)
    return ok, killed, slept


def test_a_free_port_is_left_alone():
    ok, killed, _ = _run(Held(held_now=False), [_xilab()])
    assert ok and killed == []


def test_a_held_port_with_xilab_running_ends_xilab_and_waits_for_the_port():
    xilab = _xilab()
    ok, killed, slept = _run(Held(frees_after=3), [xilab])
    assert ok
    assert killed == [[xilab]]
    assert slept, "it did not wait for the port to come free"


def test_a_port_held_by_something_else_is_not_touched():
    ok, killed, _ = _run(Held(), [])  # held, no XILab running
    assert not ok and killed == []


def test_an_xilab_that_will_not_die_is_reported_not_waited_for():
    xilab = _xilab()
    ok, killed, slept = _run(Held(), [xilab], alive=[xilab])
    assert not ok and killed == [[xilab]]
    assert slept == []


def test_a_port_still_held_after_xilab_is_gone_is_reported():
    ok, killed, slept = _run(Held(), [_xilab()])  # never frees
    assert not ok and len(killed) == 1
    assert len(slept) == int(sp.RELEASE_TIMEOUT_SECONDS / sp.RELEASE_POLL_SECONDS)


def test_every_xilab_is_killed():
    one, two = _xilab(1), _xilab(2)
    _, killed, _ = _run(Held(frees_after=1), [one, two])
    assert killed == [[one, two]]


def test_a_failure_while_checking_never_stops_the_scan():
    def boom(port):
        raise OSError("no")

    assert sp.release_xilab(["COM7"], held=boom) is False


def test_no_ports_means_nothing_to_do():
    assert sp.release_xilab([], held=Held()) is True


class TestPortIsHeld:
    def _serial(self, monkeypatch, behaviour):
        import serial

        closed: list[bool] = []

        def factory(port):
            if isinstance(behaviour, Exception):
                raise behaviour
            return SimpleNamespace(close=lambda: closed.append(True))

        monkeypatch.setattr(serial, "Serial", factory)
        return serial, closed

    def test_access_denied_means_another_process_has_it(self, monkeypatch):
        import serial

        self._serial(
            monkeypatch,
            serial.SerialException("could not open port 'COM7': PermissionError(13, 'Access is denied.', None, 5)"),
        )
        assert sp.port_is_held("COM7") is True

    def test_a_port_that_is_not_there_is_not_held(self, monkeypatch):
        import serial

        self._serial(
            monkeypatch,
            serial.SerialException(
                "could not open port 'COM7': FileNotFoundError(2, 'The system cannot find the file specified.', None, 2)"
            ),
        )
        assert sp.port_is_held("COM7") is False

    def test_a_port_that_opens_is_closed_again_and_not_held(self, monkeypatch):
        _, closed = self._serial(monkeypatch, None)
        assert sp.port_is_held("COM7") is False
        assert closed == [True]


def test_find_xilab_matches_the_executable_name_in_any_case(monkeypatch):
    import psutil

    procs = [
        SimpleNamespace(info={"name": "XILab.exe"}),
        SimpleNamespace(info={"name": "xilab.EXE"}),
        SimpleNamespace(info={"name": "python.exe"}),
        SimpleNamespace(info={"name": None}),
        SimpleNamespace(info={"name": "XILabHelper.exe"}),
    ]
    monkeypatch.setattr(psutil, "process_iter", lambda attrs=None: iter(procs))
    assert sp.find_xilab() == procs[:2]


def test_stage_init_calls_release_xilab_before_the_scan():
    """The order is the point: the port is freed, then the controller is looked for."""
    import inspect

    import stage as stage_module

    src = inspect.getsource(stage_module.Stage.__init__)
    assert src.index("release_xilab(ports)") < src.index("enumerate_devices")
