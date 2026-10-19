"""The old-vs-new handover switches: `phd2.handover.style` and `phd2.handover.pause`.

Temporary, for the handover A/B; removed with the switches after the tests.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from common.config.phd2 import HandoverConfig, HandoverStyle

try:
    from acquirer import Acquirer
    from phd2.phd2 import PHD2Connector
    from stage import StagePresetPosition
except Exception as ex:  # noqa: BLE001 -- the import chain is Windows-and-hardware-only
    pytest.skip(f"unit import chain unavailable here ({ex!r})", allow_module_level=True)

from common.activities import UnitActivities
from common.config.rois import FcuVersion


def unit_with(handover: HandoverConfig) -> SimpleNamespace:
    return SimpleNamespace(
        fcu_version=FcuVersion.v2,
        raised=[],
        unit_conf=SimpleNamespace(phd2=SimpleNamespace(handover=handover)),
        required_stage=MagicMock(),
    )


def connector(handover: HandoverConfig) -> tuple[PHD2Connector, SimpleNamespace]:
    unit = unit_with(handover)
    unit.start_activity = unit.raised.append
    unit.end_activity = MagicMock()
    c = object.__new__(PHD2Connector)
    c.parent = SimpleNamespace(unit=unit)
    return c, unit


def test_the_defaults_are_the_new_handover():
    assert HandoverConfig().style == HandoverStyle.BRACKETED
    assert HandoverConfig().pause is True


class TestLegacy:
    def test_the_guider_does_not_insert(self):
        c, unit = connector(HandoverConfig(style=HandoverStyle.LEGACY))
        with patch.object(threading, "Thread") as thread:
            assert c.start_handover_if_configured() is False
        thread.assert_not_called()
        assert UnitActivities.PreGuiding not in unit.raised

    def test_the_acquirer_inserts_before_guiding(self):
        a = object.__new__(Acquirer)
        a.unit = unit_with(HandoverConfig(style=HandoverStyle.LEGACY))
        a._position_mirror_for_guiding()
        a.unit.required_stage.move_to_preset.assert_called_once_with(StagePresetPosition.Spec)


def test_bracketed_leaves_the_mirror_to_the_guider():
    a = object.__new__(Acquirer)
    a.unit = unit_with(HandoverConfig())
    a._position_mirror_for_guiding()
    a.unit.required_stage.move_to_preset.assert_not_called()


@pytest.mark.parametrize("pause", [True, False])
def test_the_insertion_pauses_only_when_told_to(pause):
    c, unit = connector(HandoverConfig(pause=pause))
    unit.unit_conf.phd2.settle = SimpleNamespace(pixels=5, time=5, timeout=1)
    c.wait_for_settle = MagicMock(return_value=SimpleNamespace(settled=True))
    c._nudge_lock_before_insertion = MagicMock()
    c._wait_for_stage_at_spec = MagicMock()
    c.pause = MagicMock()
    c.unpause = MagicMock()

    c.do_fcu_v2_spec_handover()

    unit.required_stage.move_to_preset.assert_called_once_with(StagePresetPosition.Spec)
    assert c.pause.called is pause
    assert c.unpause.called is pause
    assert UnitActivities.Guiding in unit.raised
