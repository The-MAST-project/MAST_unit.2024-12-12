"""Every advertised stage preset must be reachable, and a rejected one must say so.

Regression cover for #85, where three of the five presets -- including both operationally
meaningful ones -- could not be commanded at all. The member names were capitalised while
request validation advertised the lowercase values, so `spec` passed validation and then
failed a by-name lookup, `Spec` was rejected by validation before the handler ran, and `mid`
failed because the member is spelled `Middle`. Only `Min` and `Max` worked, and then only
because those two happen to be spelled identically either way.

The failure mode is what makes this worth pinning: the handler's `return` on an unresolved
preset produced HTTP 200 with a null body and a motionless stage, which no caller could tell
from success. It cost a mirror-in exposure on mast01 on 2026-08-04 and a minute on mast00 on
2026-08-13.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

try:
    from stage import STARTUP_PRESET, Stage, StagePresetPosition, stage_position_names
except Exception as ex:  # noqa: BLE001 -- the import chain is Windows-and-hardware-only
    pytest.skip(f"unit import chain unavailable here ({ex!r})", allow_module_level=True)

from common.canonical import CanonicalResponse


def test_every_advertised_name_resolves():
    """The #85 regression: each name the API publishes must reach a member."""
    for name in stage_position_names:
        assert isinstance(StagePresetPosition(name), StagePresetPosition)


def test_the_operationally_meaningful_presets_are_reachable():
    """`sky` and `spec` are the two that matter, and were both unreachable."""
    assert StagePresetPosition("sky") is StagePresetPosition.Sky
    assert StagePresetPosition("spec") is StagePresetPosition.Spec


def test_mid_resolves_despite_the_member_being_middle():
    assert StagePresetPosition("mid") is StagePresetPosition.Middle


def test_capitalised_names_are_rejected():
    """One spelling on the wire. The capitals are Python identifiers, not API values."""
    for name in ("Sky", "Spec", "Mid", "Middle", "Min", "Max"):
        with pytest.raises(ValueError):
            StagePresetPosition(name)


def test_status_reports_a_value_the_setter_accepts():
    """`status()` names the preset it is parked at; that name has to round-trip.

    It did not: status used `name.lower()`, reporting "middle" for a preset the API calls
    "mid" and would reject.
    """
    for preset in StagePresetPosition:
        assert StagePresetPosition(preset.value) is preset


def test_startup_preset_is_not_an_api_value():
    """It was an enum alias of Sky, which published a duplicate "sky" in the OpenAPI enum."""
    assert STARTUP_PRESET is StagePresetPosition.Sky
    assert stage_position_names.count("sky") == 1


def test_published_schema_is_a_string_enum():
    """The 1-tuple members made this an *array* enum of 1-element lists."""
    from fastapi import FastAPI

    app = FastAPI()

    def handler(preset: StagePresetPosition): ...

    app.add_api_route("/move_to_preset", endpoint=handler)
    schema = app.openapi()["components"]["schemas"]["StagePresetPosition"]

    assert schema["type"] == "string"
    assert schema["enum"] == ["sky", "spec", "min", "mid", "max"]


def _stub(**kw):
    """Enough of a Stage for the guard clauses, without a device."""
    base = {
        "detected": True,
        "connected": True,
        "presets": {StagePresetPosition.Sky: 190000, StagePresetPosition.Spec: 321297},
        "position": 190000,
        "close_enough": lambda _p: False,
    }
    return SimpleNamespace(**{**base, **kw})


@pytest.mark.parametrize(
    ("stub", "because"),
    [
        (_stub(detected=False), "not detected"),
        (_stub(connected=False), "not connected"),
        (_stub(presets={}), "preset with no position yet"),
    ],
)
def test_refusals_return_an_error_envelope(stub, because):
    """Each of these was a bare `return` -- HTTP 200, null body, motionless stage."""
    result = Stage.move_to_preset(stub, StagePresetPosition.Spec)

    assert isinstance(result, CanonicalResponse), because
    assert result.failed
    assert result.errors


def test_unknown_string_names_what_was_expected():
    result = Stage.move_to_preset(_stub(), "nonesuch")

    assert isinstance(result, CanonicalResponse)
    assert result.failed
    assert "nonesuch" in result.errors[0]
    assert "spec" in result.errors[0]


def test_already_there_is_a_success_not_a_silent_none():
    result = Stage.move_to_preset(_stub(close_enough=lambda _p: True), StagePresetPosition.Spec)

    assert isinstance(result, CanonicalResponse)
    assert result.succeeded
