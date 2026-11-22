"""`Stage.stage_model` is always defined.

`set_profile` read `self.stage_model`, which nothing assigned since the model moved to the
configuration: enabling the call (it is commented out in `__init__`) would raise AttributeError.
Pyright flagged all seven reads.
"""

from __future__ import annotations

import pytest

try:
    from stage import Stage
except Exception as ex:  # noqa: BLE001 -- the import chain is Windows-and-hardware-only
    pytest.skip(f"unit import chain unavailable here ({ex!r})", allow_module_level=True)


def test_a_stage_without_a_model_declines_to_set_a_profile_instead_of_raising():
    stage = object.__new__(Stage)  # no __init__: no hardware, no configuration
    assert stage.stage_model is None
    assert Stage.set_profile(stage) is None
