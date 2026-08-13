"""Every absolute move goes through `move_absolute`, so every one is range-checked.

`PUT /stage/position` is the only route an operator has for an absolute move, and it used
to assign the `position` property -- a second implementation that never gained
`move_absolute`'s guards. It could therefore drive the stage past `max_travel`, which
`move_absolute` refuses, and it raised on refusal while its caller returned `Ok` regardless.

Part of #85, whose "no absolute-position route" note is stale: the route exists, it was
simply wired to the wrong one of the two implementations.
"""

from __future__ import annotations

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
