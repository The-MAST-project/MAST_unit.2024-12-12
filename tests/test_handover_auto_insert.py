"""Starting to guide inserts the fold mirror, unless told not to (#19).

On an FCU v2 unit `StartGuiding` fires `do_fcu_v2_spec_handover` on its own. That
is right for observing and wrong for measuring: an instrument timing the insertion,
or running a control that does not insert, cannot start guiding without the unit
inserting first. The harness then has to bring the stage back to SKY under a pause
before it can begin, so a null control spends two stage traverses demonstrating
none -- and each traverse carries its own settling and its own chance of losing the
star, which is the noise the control exists to exclude.

`auto_insert` defaults to true, so a unit with no DB entry behaves exactly as it
always has.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from common.config.phd2 import HandoverConfig

try:
    from phd2.phd2 import PHD2Connector
except Exception as ex:  # noqa: BLE001 -- the import chain is Windows-and-hardware-only
    pytest.skip(f"unit import chain unavailable here ({ex!r})", allow_module_level=True)

from common.activities import UnitActivities
from common.config.rois import FcuVersion


def connector(*, auto_insert: bool, fcu: FcuVersion = FcuVersion.v2) -> tuple[PHD2Connector, list]:
    """A connector carrying only what the StartGuiding branch reads."""
    raised: list = []
    c = object.__new__(PHD2Connector)
    # `conf` is a live property reading through parent.unit.unit_conf.phd2, so the
    # configuration is stubbed where the property looks rather than assigned over it.
    c.parent = SimpleNamespace(
        unit=SimpleNamespace(
            fcu_version=fcu,
            start_activity=raised.append,
            unit_conf=SimpleNamespace(phd2=SimpleNamespace(handover=HandoverConfig(auto_insert=auto_insert))),
        )
    )
    return c, raised


def fire_start_guiding(c: PHD2Connector) -> list:
    """Run the decision `StartGuiding` makes, capturing any thread it starts."""
    started: list = []

    class Recorder(threading.Thread):
        def __init__(self, *a, **kw):
            started.append(kw.get("target"))
            super().__init__(*a, **kw)

        def start(self):  # never actually run the handover
            pass

    with patch.object(threading, "Thread", Recorder):
        c.start_handover_if_configured()
    return started


class TestTheDefault:
    def test_a_unit_with_no_entry_still_inserts(self):
        """The only behaviour there has ever been, and what an observing night wants."""
        assert HandoverConfig().auto_insert is True

    def test_starting_to_guide_launches_the_handover(self):
        c, _ = connector(auto_insert=True)
        assert any(getattr(t, "__name__", "") == "do_fcu_v2_spec_handover" for t in fire_start_guiding(c))

    def test_it_raises_preguiding_while_the_mirror_is_out(self):
        """Not ready for exposure until the handover ends it."""
        c, raised = connector(auto_insert=True)
        fire_start_guiding(c)
        assert UnitActivities.PreGuiding in raised


class TestTurnedOff:
    def test_nothing_inserts_the_mirror(self):
        c, _ = connector(auto_insert=False)
        assert fire_start_guiding(c) == []

    def test_preguiding_is_not_raised(self):
        """An activity nobody will clear is how a unit reports work that is not happening.

        The handover is what ends PreGuiding. With no handover running, raising it
        would leave the unit permanently not-ready-for-exposure.
        """
        c, raised = connector(auto_insert=False)
        fire_start_guiding(c)
        assert UnitActivities.PreGuiding not in raised


class TestUnaffectedUnits:
    def test_an_fcu_v1_unit_never_inserted_anyway(self):
        """v1 has no fold mirror and is already at SPEC from the solve phase."""
        c, _ = connector(auto_insert=True, fcu=FcuVersion.v1)
        assert fire_start_guiding(c) == []
