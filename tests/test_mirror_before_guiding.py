"""On the legacy handover the acquirer inserts the fold mirror before guiding starts (#30).

PHD2 then picks its star on the field it guides on for the whole exposure. On
2026-10-20 the bracketed handover, which picks at SKY and inserts afterwards, lost
its star at insertion on 3 of 13 arms: it had been picked on area the mirror then
vignetted.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from common.config.phd2 import HandoverConfig, HandoverStyle
from common.config.rois import FcuVersion

try:
    from acquirer import Acquirer
    from stage import StagePresetPosition
except Exception as ex:  # noqa: BLE001 -- the import chain is Windows-and-hardware-only
    pytest.skip(f"unit import chain unavailable here ({ex!r})", allow_module_level=True)


def acquirer(*, style=HandoverStyle.LEGACY, auto_insert=True, fcu=FcuVersion.v2) -> tuple[Acquirer, list]:
    moves: list = []
    unit = SimpleNamespace(
        fcu_version=fcu,
        unit_conf=SimpleNamespace(phd2=SimpleNamespace(handover=HandoverConfig(style=style, auto_insert=auto_insert))),
        required_stage=SimpleNamespace(move_to_preset=moves.append),
    )
    a = object.__new__(Acquirer)
    a.unit = unit
    return a, moves


def test_legacy_inserts_the_mirror():
    a, moves = acquirer()
    a._position_mirror_for_guiding()
    assert moves == [StagePresetPosition.Spec]


def test_bracketed_leaves_it_to_the_guider():
    a, moves = acquirer(style=HandoverStyle.BRACKETED)
    a._position_mirror_for_guiding()
    assert moves == []


def test_with_auto_insert_off_the_caller_inserts():
    """The measurement mode: guiding starts with the mirror wherever it is."""
    a, moves = acquirer(auto_insert=False)
    a._position_mirror_for_guiding()
    assert moves == []


def test_fcu_v1_has_no_mirror_to_insert():
    a, moves = acquirer(fcu=FcuVersion.v1)
    a._position_mirror_for_guiding()
    assert moves == []
