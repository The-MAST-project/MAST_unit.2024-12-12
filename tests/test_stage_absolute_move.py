"""Every absolute move goes through `move_absolute`, so every one is range-checked.

`PUT /stage/position` is the only route an operator has for an absolute move, and it used
to assign the `position` property -- a second implementation that never gained
`move_absolute`'s guards. It could therefore drive the stage past `max_travel`, which
`move_absolute` refuses, and it raised on refusal while its caller returned `Ok` regardless.

Part of #85, whose "no absolute-position route" note is stale: the route exists, it was
simply wired to the wrong one of the two implementations.
"""

from __future__ import annotations

from collections import deque
from types import SimpleNamespace

import pytest

try:
    from stage import Stage
except Exception as ex:  # noqa: BLE001 -- the import chain is Windows-and-hardware-only
    pytest.skip(f"unit import chain unavailable here ({ex!r})", allow_module_level=True)

from common.canonical import CanonicalResponse

MIN_TRAVEL, MAX_TRAVEL = 0, 343544


def _stub(**kw):
    """Enough of a Stage to reach the guard clauses, without a device.

    `move_absolute` is only ever exercised here up to the point of refusal or of deciding
    it is already there; nothing below reaches `ximclib`.
    """
    base = {
        "detected": True,
        "connected": True,
        "min_travel": MIN_TRAVEL,
        "max_travel": MAX_TRAVEL,
        "position": 190000,
        "close_enough": lambda _p: False,
    }
    stub = SimpleNamespace(**{**base, **kw})
    if "move_absolute" not in kw:
        # The real implementation by default, so callers exercise the guards rather than a
        # mock of them. Tests that are about the *delegation* pass their own recorder.
        stub.move_absolute = lambda pos: Stage.move_absolute(stub, pos)
    return stub


@pytest.mark.parametrize("beyond", [MAX_TRAVEL, MAX_TRAVEL + 1, 999_999, -1])
def test_the_endpoint_refuses_positions_outside_travel(beyond):
    """The whole point: this is what assigning the property skipped."""
    result = Stage.set_position(_stub(), beyond)

    assert isinstance(result, CanonicalResponse)
    assert result.failed
    assert "out of range" in result.errors[0]


def test_the_endpoint_refuses_when_undetected():
    result = Stage.set_position(_stub(detected=False), 200000)

    assert isinstance(result, CanonicalResponse)
    assert result.failed


def test_already_there_is_ok_not_none():
    """A bare `return` here is HTTP 200 with a null body -- a refusal and a success look
    identical to the caller, which is the #85 failure mode."""
    result = Stage.set_position(_stub(close_enough=lambda _p: True), 190000)

    assert isinstance(result, CanonicalResponse)
    assert result.succeeded


def test_a_non_numeric_position_is_an_error_not_a_traceback():
    result = Stage.move_absolute(_stub(), "not-a-number")

    assert isinstance(result, CanonicalResponse)
    assert result.failed


def test_set_position_delegates_rather_than_reimplementing():
    """Pins the delegation itself: the endpoint must not grow its own move path again."""
    seen = []
    stub = _stub(move_absolute=lambda pos: seen.append(pos) or CanonicalResponse(value="ok"))

    Stage.set_position(stub, 250000)

    assert seen == [250000]


def test_the_property_setter_delegates_too():
    """Both absolute-move paths must land on the one implementation that range-checks."""
    seen = []
    stub = _stub(move_absolute=lambda pos: seen.append(pos) or CanonicalResponse(value="ok"))

    Stage.position.fset(stub, 250000)

    assert seen == [250000]


def test_the_property_setter_raises_on_refusal():
    """It is a property, so it cannot return an envelope -- but it must not swallow one."""
    stub = _stub(move_absolute=lambda _pos: CanonicalResponse(errors=["out of range"]))

    with pytest.raises(ValueError, match="out of range"):
        Stage.position.fset(stub, 999_999)


# ------------------------------------------- is_moving is set by the command, not the poll --


def _movable_stub(monkeypatch, **kw):
    """A stub that reaches PAST `command_move` into the post-command bookkeeping."""
    import threading

    import stage as stage_module

    monkeypatch.setattr(
        stage_module,
        "ximclib",
        SimpleNamespace(command_move=lambda *a, **k: stage_module.Result.Ok),
    )
    return _stub(
        device=1,
        stage_lock=threading.Lock(),
        latest_positions=deque(maxlen=3),
        ticks_at_start=None,
        target=None,
        motion_start_time=None,
        is_moving=False,
        start_activity=lambda *a, **k: None,
        **kw,
    )


def test_a_commanded_move_sets_is_moving_before_any_poll_runs(monkeypatch):
    """The 2026-09-02 race.

    `is_moving` is refreshed only by the stage's 2-second `ontimer`, so if the command does
    not set it, `while stage.is_moving` reads the pre-move False and returns immediately.
    `acquirer._await_stage` did exactly that, 6 ms into a 133,000-count traverse, and the
    acquisition exposed with the fold mirror in transit.

    No poll runs in this test -- that is the point. The flag must be true purely because a
    move was commanded.
    """
    stub = _movable_stub(monkeypatch)

    result = Stage.move_absolute(stub, 200000)

    assert result.succeeded
    assert stub.is_moving is True


def test_is_moving_is_not_set_when_the_move_is_refused(monkeypatch):
    """Already close enough: no command is issued, so nothing may claim motion. Without
    this the test above would pass on a stub that set the flag unconditionally."""
    stub = _movable_stub(monkeypatch, close_enough=lambda _p: True)

    result = Stage.move_absolute(stub, 200000)

    assert result.succeeded
    assert stub.is_moving is False


def test_is_moving_is_not_set_when_the_position_is_out_of_range(monkeypatch):
    stub = _movable_stub(monkeypatch)

    result = Stage.move_absolute(stub, MAX_TRAVEL + 1)

    assert result.failed
    assert stub.is_moving is False
