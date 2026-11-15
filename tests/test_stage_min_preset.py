"""The `Min` preset is kept clear of the lower border (FCU v2 parks the mirror there).

On limit-switch borders `min_travel` is 0 -- the switch itself -- so `Min` is
`max(min_travel, MIN_PRESET_CLEARANCE)`. `Middle` and `Max` still come from the real travel.
"""

from __future__ import annotations

import pytest
from test_config_is_live import component, stage_config, with_unit_conf


def _presets(min_travel, max_travel):
    stage = object.__new__(component("stage", "Stage"))
    stage.min_travel, stage.max_travel = min_travel, max_travel
    with_unit_conf(stage, "unit", "stage", stage_config())
    return stage.presets


def test_limit_switch_borders_leave_min_clear_of_the_switch():
    from stage import MIN_PRESET_CLEARANCE, StagePresetPosition

    assert _presets(0, 343544)[StagePresetPosition.Min] == MIN_PRESET_CLEARANCE == 100


def test_a_border_already_clear_of_the_switch_is_kept():
    from stage import StagePresetPosition

    assert _presets(500, 343544)[StagePresetPosition.Min] == 500


def test_the_clearance_does_not_move_the_other_presets():
    from stage import StagePresetPosition

    presets = _presets(0, 343544)

    assert presets[StagePresetPosition.Max] == 343544
    assert presets[StagePresetPosition.Middle] == 171772


def test_without_travel_limits_there_is_no_min():
    from stage import StagePresetPosition

    assert StagePresetPosition.Min not in _presets(None, None)


@pytest.mark.parametrize("min_travel", [0, 50, 99])
def test_min_is_never_inside_the_clearance(min_travel):
    from stage import MIN_PRESET_CLEARANCE, StagePresetPosition

    assert _presets(min_travel, 343544)[StagePresetPosition.Min] >= MIN_PRESET_CLEARANCE
