"""`Stage.stage_model` is read live from the configuration.

`set_profile` read `self.stage_model`, which nothing assigned since the model moved to the
configuration: enabling the call (it is commented out in `__init__`) would raise AttributeError.
Pyright flagged all seven reads.  It is a property, not an attribute set in `__init__`: the
configuration is read live (`test_config_is_live`).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

try:
    from stage import Stage
except Exception as ex:  # noqa: BLE001 -- the import chain is Windows-and-hardware-only
    pytest.skip(f"unit import chain unavailable here ({ex!r})", allow_module_level=True)


def _stage(model):
    """A Stage without __init__: no hardware, only the configuration it reads."""
    stage = object.__new__(Stage)
    stage.unit = SimpleNamespace(unit_conf=SimpleNamespace(stage=SimpleNamespace(model=model)))  # type: ignore[assignment]
    return stage


def test_the_model_is_the_configurations():
    assert _stage("8MT173-20DCE2").stage_model == "8MT173-20DCE2"


def test_the_model_follows_the_configuration_as_it_changes():
    stage = _stage("8MT167-25LS-MEn1")
    stage.unit.unit_conf.stage.model = "8MT173-20DCE2"  # type: ignore[union-attr]
    assert stage.stage_model == "8MT173-20DCE2"


def test_a_stage_without_a_model_declines_to_set_a_profile_instead_of_raising():
    assert Stage.set_profile(_stage(None)) is None


def test_a_model_without_a_profile_file_declines_instead_of_raising():
    assert Stage.set_profile(_stage("NO-SUCH-MODEL")) is None
