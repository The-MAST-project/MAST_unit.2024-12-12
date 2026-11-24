"""Naming the call that sets a controller error (ERRC / ERRD / ERRV), and logging each change once.

The flags latch and the controller records nothing about their cause, so `Stage._call` reads
them after every call. On mast07 (2026-11-23) the old check logged `STATE_ERRC, STATE_ERRV`
every 2 s, 965 times in one night, with no word on which command had set them.
"""

from __future__ import annotations

import logging
import threading
from types import SimpleNamespace

import pytest

try:
    import stage as stage_module
    from stage import Stage
except Exception as ex:  # noqa: BLE001 -- the import chain is Windows-and-hardware-only
    pytest.skip(f"unit import chain unavailable here ({ex!r})", allow_module_level=True)

OK = 0
VALUE_ERROR = -3
ERRC, ERRD, ERRV = 0x1, 0x2, 0x4


class FakeXimc:
    """A controller whose flags each scripted call leaves behind."""

    def __init__(self, flags=0):
        self.flags = flags
        self.effects: dict[str, tuple[int, int]] = {}  # name -> (result, flags it sets)
        self.status_reads = 0

    def get_status(self, device, ref):
        self.status_reads += 1
        ref.Flags = self.flags
        return OK

    def __getattr__(self, name):
        def call(device, *args):
            result, sets = self.effects.get(name, (OK, 0))
            self.flags |= sets
            return result

        return call


class _Stage(Stage):
    def __del__(self):
        """`Stage.__del__` closes the device through the real ximclib, the fake long gone by then."""


@pytest.fixture
def stage(monkeypatch):
    monkeypatch.setattr(stage_module, "byref", lambda obj: obj)
    monkeypatch.setattr(stage_module, "status_t", lambda: SimpleNamespace(Flags=0))
    monkeypatch.setattr(stage_module.Result, "Ok", OK, raising=False)
    s = object.__new__(_Stage)
    s.stage_lock = threading.Lock()
    s.device = 1
    return s


def _with(monkeypatch, fake):
    monkeypatch.setattr(stage_module, "ximclib", fake, raising=False)
    return fake


def _errors(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]


def test_the_call_that_sets_an_error_is_named(stage, monkeypatch, caplog):
    fake = _with(monkeypatch, FakeXimc())
    fake.effects["command_move"] = (OK, ERRV)

    stage._call("command_stop")
    stage._call("command_move", 999999, 0)

    expected = f"controller error after command_move(999999, 0): {stage_module.describe_controller_errors(ERRV)}"
    assert _errors(caplog) == [expected]


def test_a_latched_error_is_logged_once_not_on_every_read(stage, monkeypatch, caplog):
    fake = _with(monkeypatch, FakeXimc())
    fake.effects["command_move"] = (OK, ERRC)

    stage._call("command_move", 1, 0)
    for _ in range(5):
        stage._note_controller_errors(fake.flags, after="periodic")
        stage._call("get_secure_settings", object())

    assert len(_errors(caplog)) == 1


def test_a_second_bit_is_reported_against_its_own_call(stage, monkeypatch, caplog):
    fake = _with(monkeypatch, FakeXimc())
    fake.effects["command_move"] = (OK, ERRV)
    fake.effects["command_movr"] = (OK, ERRC)

    stage._call("command_move", 1, 0)
    stage._call("command_movr", 2, 0)

    errors = _errors(caplog)
    assert len(errors) == 2
    assert "command_movr(2, 0)" in errors[1] and "STATE_ERRC" in errors[1] and "STATE_ERRV" not in errors[1]


def test_a_failing_result_is_logged_with_the_call(stage, monkeypatch, caplog):
    fake = _with(monkeypatch, FakeXimc())
    fake.effects["get_device_information"] = (VALUE_ERROR, 0)

    assert stage._call("get_device_information", object()) == VALUE_ERROR
    assert any(m.startswith("get_device_information(): returned") for m in _errors(caplog))


def test_with_every_bit_already_set_no_status_read_is_spent(stage, monkeypatch):
    fake = _with(monkeypatch, FakeXimc(flags=ERRC | ERRD | ERRV))
    stage._controller_errors = ERRC | ERRD | ERRV

    stage._call("command_stop")

    assert fake.status_reads == 0


def test_bits_already_set_on_opening_are_a_warning_not_blamed_on_a_call(stage, monkeypatch, caplog):
    _with(monkeypatch, FakeXimc(flags=ERRC | ERRV))

    stage._baseline_controller_errors()
    stage._call("get_serial_number", object())

    assert _errors(caplog) == []
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "already set on opening" in warnings[0]


def test_a_cleared_bit_is_reported(stage, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    stage._controller_errors = ERRC

    stage._note_controller_errors(0, after="periodic")

    assert stage._controller_errors == 0
    assert any("cleared" in r.getMessage() and "STATE_ERRC" in r.getMessage() for r in caplog.records)


def test_the_libximc_callback_forwards_and_never_raises(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger=stage_module._libximc_logger.name)
    # Called through ctypes, the way libximc calls it.
    stage_module._forward_libximc_log(1, "errc from controller", None)
    assert any(r.levelno == logging.ERROR and "errc from controller" in r.getMessage() for r in caplog.records)

    monkeypatch.setattr(stage_module._libximc_logger, "log", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    stage_module._forward_libximc_log(2, "anything", None)  # must not raise
