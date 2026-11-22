"""Clearing a move command's MVCMD_ERROR: stop once it is clear, not once it is still set.

The check was inverted.  On mast03 (2026-10-12/13) and mast02 (2026-11-16) a move ended with
MVCMD_ERROR, `command_stop()` left the status at 0x00000005 -- MVCMD_STOP, error bit clear --
and the loop took that for a failure: all three attempts ran, none was logged as cleared.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

try:
    import stage as stage_module
    from stage import Stage
except Exception as ex:  # noqa: BLE001 -- the import chain is Windows-and-hardware-only
    pytest.skip(f"unit import chain unavailable here ({ex!r})", allow_module_level=True)

# Ximc's own values (pyximc.py).  The stage module's `MvcmdStatus` is a stub where the
# controller library is not installed, so the tests carry the numbers they depend on.
MVCMD_MOVE = 0x01
MVCMD_STOP = 0x05
MVCMD_ERROR = 0x40


class FakeXimc:
    """A controller whose status after each `command_stop()` is the next one scripted."""

    def __init__(self, statuses, get_status_result=None):
        self.statuses = list(statuses)
        self.stops = 0
        self.get_status_result = stage_module.Result.Ok if get_status_result is None else get_status_result

    def command_stop(self, device):
        self.stops += 1
        return stage_module.Result.Ok

    def get_status(self, device, ref):
        if self.get_status_result != stage_module.Result.Ok:
            return self.get_status_result
        ref.MvCmdSts = self.statuses.pop(0)
        return stage_module.Result.Ok


@pytest.fixture
def stage(monkeypatch):
    monkeypatch.setattr(stage_module.time, "sleep", lambda s: None)
    monkeypatch.setattr(stage_module, "MvcmdStatus", SimpleNamespace(MVCMD_ERROR=MVCMD_ERROR))
    monkeypatch.setattr(stage_module, "byref", lambda obj: obj)
    s = object.__new__(Stage)
    s.stage_lock = threading.Lock()
    s.device = 1
    return s


def _clear(stage, monkeypatch, fake):
    monkeypatch.setattr(stage_module, "ximclib", fake, raising=False)
    status = SimpleNamespace(MvCmdSts=MVCMD_MOVE | MVCMD_ERROR)
    return stage._clear_mvcmd_error(status), status


def test_an_error_that_one_stop_clears_is_cleared_on_the_first_attempt(stage, monkeypatch):
    fake = FakeXimc([MVCMD_STOP, MVCMD_STOP, MVCMD_STOP])  # what mast02 and mast03 reported
    cleared, status = _clear(stage, monkeypatch, fake)
    assert cleared
    assert fake.stops == 1, "it kept stopping after the error was already clear"
    assert status.MvCmdSts & MVCMD_ERROR == 0


def test_an_error_that_persists_is_tried_three_times_and_reported(stage, monkeypatch):
    still = MVCMD_STOP | MVCMD_ERROR
    fake = FakeXimc([still, still, still])
    cleared, _ = _clear(stage, monkeypatch, fake)
    assert not cleared
    assert fake.stops == 3


def test_an_error_cleared_on_the_second_attempt_stops_there(stage, monkeypatch):
    fake = FakeXimc([MVCMD_STOP | MVCMD_ERROR, MVCMD_STOP, MVCMD_STOP])
    cleared, _ = _clear(stage, monkeypatch, fake)
    assert cleared and fake.stops == 2


def test_a_controller_that_stops_answering_ends_the_attempts(stage, monkeypatch):
    fake = FakeXimc([], get_status_result=stage_module.Result.NoDevice)
    cleared, _ = _clear(stage, monkeypatch, fake)
    assert not cleared and fake.stops == 1
