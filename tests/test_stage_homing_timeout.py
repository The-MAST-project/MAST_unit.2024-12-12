"""Homing starts and returns; the status timer finishes it -- zeroes it, or stops it and calls it failed.

It used to be one blocking `command_homezero` under ``stage_lock``, made from `Stage.__init__`.
A stage that did not home held up the whole unit startup for the full timeout and blinded the
status timer for as long: mast07 logged nothing for 150 s on every start of 2026-11-23.

The timeout must still outlast a homing from the far end (MAST_unit#311): on mast03 on
2026-10-12 a homing from SPEC (322823) was still running at 60 s with a fifth of the travel left.
And an overdue homing is now *stopped*, never abandoned -- an abandoned one zeroed the stage in
the middle of the move that followed.
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

#: Homing from SPEC was measured still running at 60 s with roughly a fifth of the travel
#: left, so it needs well over a minute; this is the floor any timeout must clear.
MEASURED_FAR_END_HOMING_LOWER_BOUND_S = 60.0

OK, ERROR = 0, -1
MVCMD_HOME, MVCMD_ERROR, MVCMD_RUNNING = 0x06, 0x40, 0x80


class FakeXimc:
    def __init__(self):
        self.calls: list[str] = []
        self.results: dict[str, int] = {}

    def get_status(self, device, ref):
        ref.Flags = 0
        return OK

    def __getattr__(self, name):
        def call(device, *args):
            self.calls.append(name)
            return self.results.get(name, OK)

        return call


class _Stage(Stage):
    def __del__(self):
        """`Stage.__del__` closes the device through the real ximclib."""


@pytest.fixture
def clock(monkeypatch):
    now = SimpleNamespace(t=1000.0)
    monkeypatch.setattr(stage_module.time, "monotonic", lambda: now.t)
    return now


@pytest.fixture
def ximc(monkeypatch):
    fake = FakeXimc()
    monkeypatch.setattr(stage_module, "ximclib", fake, raising=False)
    monkeypatch.setattr(stage_module, "byref", lambda obj: obj)
    monkeypatch.setattr(stage_module, "status_t", lambda: SimpleNamespace(Flags=0))
    monkeypatch.setattr(stage_module.Result, "Ok", OK, raising=False)
    monkeypatch.setattr(stage_module, "MvcmdStatus", SimpleNamespace(MVCMD_RUNNING=MVCMD_RUNNING, MVCMD_ERROR=MVCMD_ERROR))
    return fake


@pytest.fixture
def stage(ximc, clock):
    s = object.__new__(_Stage)
    s.stage_lock = threading.Lock()
    s.device = 1
    s._controller_errors = 0x7  # no attribution reads: not this file's subject
    s.activities_ = set()
    s.start_activity = lambda a, **k: s.activities_.add(a)  # type: ignore[method-assign]
    s.end_activity = lambda a, **k: s.activities_.discard(a)  # type: ignore[method-assign]
    s.is_active = lambda a: a in s.activities_  # type: ignore[method-assign]
    s.moves = []
    s.move_to_preset = lambda p: s.moves.append(p)  # type: ignore[method-assign]
    return s


def _status(mvcmd, position=0):
    return SimpleNamespace(MvCmdSts=mvcmd, CurPosition=position, Flags=0)


Homing = stage_module.StageActivities.Homing
StartingUp = stage_module.StageActivities.StartingUp


def test_the_homing_timeout_outlasts_a_homing_from_the_far_end():
    assert stage_module.HOMING_TIMEOUT_SECONDS >= 2 * MEASURED_FAR_END_HOMING_LOWER_BOUND_S


def test_home_starts_homing_and_returns_without_waiting(stage, ximc):
    assert stage.home().succeeded
    assert ximc.calls == ["command_home"]
    assert stage.is_active(Homing) and stage.is_moving


def test_a_finished_homing_is_zeroed_and_ends(stage, ximc):
    stage.home()
    stage._advance_homing(_status(MVCMD_HOME | MVCMD_RUNNING), read_after_homing_began=True)
    assert stage.is_active(Homing)

    stage._advance_homing(_status(MVCMD_HOME), read_after_homing_began=True)

    assert ximc.calls == ["command_home", "command_zero"]
    assert not stage.is_active(Homing) and stage.homing_error is None


def test_a_status_read_before_the_command_does_not_end_the_homing(stage, ximc):
    """The timer may read "not running" just before `command_home` takes the lock."""
    stage.home()
    stage._advance_homing(_status(0), read_after_homing_began=False)
    assert stage.is_active(Homing) and "command_zero" not in ximc.calls


def test_an_overdue_homing_is_stopped_not_abandoned(stage, ximc, clock):
    stage.home()
    clock.t += stage_module.HOMING_TIMEOUT_SECONDS + 1

    stage._advance_homing(_status(MVCMD_HOME | MVCMD_RUNNING, position=2), read_after_homing_began=True)

    assert ximc.calls == ["command_home", "command_stop"]
    assert not stage.is_active(Homing)
    assert "not home within" in stage.homing_error and "stopped at 2" in stage.homing_error


def test_a_homing_that_ends_in_error_is_not_zeroed(stage, ximc):
    stage.home()
    stage._advance_homing(_status(MVCMD_HOME | MVCMD_ERROR), read_after_homing_began=True)
    assert "command_zero" not in ximc.calls
    assert "MVCMD_ERROR" in stage.homing_error


def test_a_refused_home_command_is_a_failure_with_no_activity(stage, ximc):
    ximc.results["command_home"] = ERROR
    assert stage.home().failed
    assert not stage.is_active(Homing) and stage.homing_error


def test_startup_waits_for_homing_then_moves(stage):
    stage.home()
    stage.start_activity(StartingUp)

    stage._advance_homing(_status(MVCMD_HOME), read_after_homing_began=True)

    assert stage.moves == [stage_module.STARTUP_PRESET]


def test_a_failed_homing_ends_startup_without_moving(stage, clock):
    stage.home()
    stage.start_activity(StartingUp)
    clock.t += stage_module.HOMING_TIMEOUT_SECONDS + 1

    stage._advance_homing(_status(MVCMD_HOME | MVCMD_RUNNING), read_after_homing_began=True)

    assert stage.moves == [] and not stage.is_active(StartingUp)
