"""Homing must be given long enough to finish from the far end of travel (MAST_unit#311).

`Timeout` abandons the wait but not the call: an expired `command_homezero` keeps running,
startup's move to the SKY preset takes over the motor, and when the call returns it zeroes
the stage mid-move. On mast03 on 2026-10-12, homing from SPEC (322823) was still running at
60 s, at 68058 and 59475, and both times the next move to SPEC stopped at the end of travel.
"""

from __future__ import annotations

import threading

import pytest

try:
    import stage as stage_module
    from stage import Stage
except Exception as ex:  # noqa: BLE001 -- the import chain is Windows-and-hardware-only
    pytest.skip(f"unit import chain unavailable here ({ex!r})", allow_module_level=True)

#: Homing from SPEC was measured still running at 60 s with roughly a fifth of the travel
#: left, so it needs well over a minute; this is the floor any timeout must clear.
MEASURED_FAR_END_HOMING_LOWER_BOUND_S = 60.0


def test_the_homing_timeout_outlasts_a_homing_from_the_far_end():
    assert stage_module.HOMING_TIMEOUT_SECONDS >= 2 * MEASURED_FAR_END_HOMING_LOWER_BOUND_S


def test_home_waits_for_the_homing_timeout(monkeypatch):
    waited: list[float] = []

    class RecordingTimeout:
        def __init__(self, sec):
            waited.append(sec)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def run(self, func, *args, **kwargs):
            return func(*args, **kwargs)

    monkeypatch.setattr(stage_module, "Timeout", RecordingTimeout)
    monkeypatch.setattr(stage_module.ximclib, "command_homezero", lambda device: stage_module.Result.Ok, raising=False)

    s = object.__new__(Stage)
    s.stage_lock = threading.Lock()
    s.device = 1
    s.start_activity = lambda *a, **k: None  # type: ignore[method-assign]

    Stage.home(s)

    assert waited == [stage_module.HOMING_TIMEOUT_SECONDS]
