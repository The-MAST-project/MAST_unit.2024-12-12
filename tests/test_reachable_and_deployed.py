"""Each migrated component reports the two halves of `operational` (MAST_unit#144).

The property under test is that *connected but not commanded* is now sayable. Before the
split a unit that had reached every device and moved none reported `operational: false` with
reasons reading "covers: not open", "stage: not at a preset" -- character-for-character what a
unit with jammed covers and a stuck stage reports.

Components are subclassed rather than constructed: `__init__` acquires hardware today, so the
terms are overridden and the inherited halves computed from them. What is being tested is the
predicate arithmetic, which is where the conflation lived.
"""

from __future__ import annotations

import pytest

from common.models.statuses import CoversState, LifecycleState, component_lifecycle_state
from covers import Covers


class FakeCovers(Covers):
    """Covers whose four terms are dictated rather than read from PWI4."""

    def __init__(self, *, on: bool, viable: bool, connected: bool, state: CoversState):
        self._on = on
        self._viable = viable
        self._connected = connected
        self._state = state

    def is_on(self) -> bool:
        return self._on

    @property
    def pwi4_is_viable(self) -> bool:
        return self._viable

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def state(self) -> CoversState:
        return self._state

    @property
    def name(self) -> str:
        return "covers"


def _covers(**kwargs) -> FakeCovers:
    covers = object.__new__(FakeCovers)
    FakeCovers.__init__(covers, **kwargs)
    return covers


def test_connected_and_closed_is_standby_not_a_fault():
    covers = _covers(on=True, viable=True, connected=True, state=CoversState.Closed)

    assert covers.reachable is True
    assert covers.deployed is False
    assert covers.why_not_reachable == []
    assert covers.why_not_deployed == ["covers: not open (state='Closed')"]
    assert component_lifecycle_state(covers.reachable, covers.deployed) is LifecycleState.Standby


def test_unpowered_and_closed_reports_the_reachability_cause_only():
    """`why_not_operational` keeps its old output: the presence failure, not both halves."""
    covers = _covers(on=False, viable=True, connected=True, state=CoversState.Closed)

    assert covers.reachable is False
    assert covers.why_not_operational == ["covers: not powered"]
    assert component_lifecycle_state(covers.reachable, covers.deployed) is LifecycleState.Unreachable


def test_open_and_connected_is_operational():
    covers = _covers(on=True, viable=True, connected=True, state=CoversState.Open)

    assert covers.operational is True
    assert covers.why_not_operational == []
    assert component_lifecycle_state(covers.reachable, covers.deployed) is LifecycleState.Operational


def test_an_unreachable_pwi4_is_a_reachability_failure_not_a_cover_fault():
    covers = _covers(on=True, viable=False, connected=False, state=CoversState.Unknown)

    assert covers.reachable is False
    assert covers.why_not_reachable is not None
    assert "PWI4 not answering" in covers.why_not_reachable[0]


def test_the_phd2_imager_is_presence_only():
    """It starts cooling and never follows it to setpoint (#149), so it has no deployment term."""
    phd2 = pytest.importorskip("phd2.phd2")

    class FakeConnector(phd2.PHD2Connector):
        def __init__(self, connected: bool):
            self._connected = connected
            self.watched_process = None  # the real `__del__` reaps it

        @property
        def connected(self) -> bool:
            return self._connected

        @property
        def name(self) -> str:
            return "imager"

    connected = object.__new__(FakeConnector)
    FakeConnector.__init__(connected, True)
    assert connected.reachable is True
    assert connected.deployed is True
    assert connected.operational is True

    absent = object.__new__(FakeConnector)
    FakeConnector.__init__(absent, False)
    assert absent.reachable is False
    assert absent.operational is False
    assert absent.why_not_operational == ["imager: not connected"]


def test_the_stage_treats_a_driver_failure_as_unreachable():
    """`_currently_operational` is a driver-failure latch: an unusable stage is not merely idle."""
    try:
        import stage as stage_module
    except (ImportError, NameError) as ex:
        # `importorskip` is not enough: `stage.py:69` reads pyximc's `Result` members outside the
        # `platform.system() == "Windows"` guard four lines above them, so the import fails with
        # `NameError` rather than `ImportError` off Windows. That is MAST_unit#118's near-miss
        # case, and this skip is here to be deleted when it lands.
        pytest.skip(f"stage is not importable here: {type(ex).__name__}: {ex}")

    class FakeStage(stage_module.Stage):
        def __init__(self, *, currently_operational: bool):
            self._currently = currently_operational
            self._why = ["stage: controller reported a failure"]

        def is_on(self) -> bool:
            return True

        @property
        def detected(self) -> bool:
            return True

        @property
        def connected(self) -> bool:
            return True

        @property
        def _currently_operational(self) -> bool:
            return self._currently

        @property
        def _why_not_currently_operational(self) -> list[str]:
            return self._why

        def at_preset(self, preset) -> bool:
            return False

        @property
        def name(self) -> str:
            return "stage"

    failed = object.__new__(FakeStage)
    FakeStage.__init__(failed, currently_operational=False)

    assert failed.reachable is False
    assert failed.why_not_operational == ["stage: controller reported a failure"]
