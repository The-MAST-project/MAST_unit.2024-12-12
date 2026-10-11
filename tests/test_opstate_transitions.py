"""The unit's `opstate`: which lifecycle command it is living under (opmode-design 4, 4a).

INITIALIZING -> INITIALIZED (end of start_lifespan, production) -> RUNNING <-> SHUTDOWN, each
set on *receipt* of the command, not on its completion; health stays in `operational`. Only
the unit writes it, and only `start_lifespan` acts on the opmode.
"""

from __future__ import annotations

import pytest

from common.activities import UnitActivities
from common.opmode import OpMode, OpState


class _Flags:
    def __init__(self):
        self.active: set = set()

    def is_active(self, activity) -> bool:
        return activity in self.active

    def start_activity(self, activity, **_):
        self.active.add(activity)

    def end_activity(self, activity, **_):
        self.active.discard(activity)


class _NoThread:
    """Records the target instead of running it: these tests are about what happens on receipt."""

    started: list = []

    def __init__(self, name=None, target=None, **_):
        self.name, self.target = name, target

    def start(self):
        _NoThread.started.append(self.name)


@pytest.fixture
def unit(monkeypatch):
    import unit as unit_module
    from unit import Unit

    _NoThread.started = []
    monkeypatch.setattr(unit_module, "Thread", _NoThread)

    u = object.__new__(Unit)
    from common.opmode import OpmodeBase

    OpmodeBase.__init__(u)
    u._opmode = OpMode.PRODUCTION
    u.components = []
    u._was_shut_down = False
    flags = _Flags()
    for name in ("is_active", "start_activity", "end_activity"):
        setattr(u, name, getattr(flags, name))
    return u


def test_a_new_unit_is_initializing(unit):
    assert unit.opstate is OpState.INITIALIZING


def test_production_start_lifespan_reports_initialized_and_does_not_start(unit):
    unit.start_lifespan()

    assert unit.opstate is OpState.INITIALIZED
    assert _NoThread.started == []


def test_development_start_lifespan_starts_at_once(unit):
    unit._opmode = OpMode.DEVELOPMENT

    unit.start_lifespan()

    assert unit.opstate is OpState.RUNNING
    assert _NoThread.started == ["unit-startup-thread"]


def test_startup_is_running_on_receipt_before_the_work_runs(unit):
    unit.start_lifespan()

    unit.startup()

    assert unit.opstate is OpState.RUNNING
    assert unit.is_active(UnitActivities.StartingUp), "raised before the thread, not in it"


def test_a_second_startup_while_starting_starts_nothing_more(unit):
    unit.startup()
    unit.startup()

    assert _NoThread.started == ["unit-startup-thread"]


def test_shutdown_from_initialized_is_accepted(unit, monkeypatch):
    """How a supervisor makes a never-started machine safe."""
    unit.connect = lambda: None
    monkeypatch.setattr(type(unit), "connected", property(lambda self: True))
    unit.start_lifespan()

    unit.shutdown()

    assert unit.opstate is OpState.SHUTDOWN
    assert unit.is_active(UnitActivities.ShuttingDown), "raised before the thread, not in it"


def test_shutdown_is_idempotent_and_startup_returns_to_running(unit, monkeypatch):
    monkeypatch.setattr(type(unit), "connected", property(lambda self: True))
    unit.startup()
    unit.end_activity(UnitActivities.StartingUp)

    unit.shutdown()
    unit.shutdown()
    assert unit.opstate is OpState.SHUTDOWN

    unit.end_activity(UnitActivities.ShuttingDown)
    unit.startup()
    assert unit.opstate is OpState.RUNNING


def test_shutdown_raises_its_dispatch_flag_before_the_thread(unit):
    """Else an ontimer tick before `do_shutdown` runs sees no component flag and ends it."""
    unit.shutdown()

    assert unit._shutdown_dispatching is True
    assert _NoThread.started == ["shutdown-thread"]


def test_a_repeat_shutdown_does_not_reconnect_and_reports_ok(unit):
    """The connect is `do_shutdown`'s: a repeat must not reconnect what is being disconnected."""
    connects: list[int] = []
    unit.connect = lambda: connects.append(1)
    unit.start_activity(UnitActivities.ShuttingDown)

    response = unit.shutdown()

    assert response.succeeded
    assert connects == []
    assert _NoThread.started == []


@pytest.fixture
def ticking_unit(unit):
    """`unit`, plus what `ontimer` reads, with no components and no autofocus."""
    for name in ("mount", "imager", "stage", "focuser", "covers", "pw", "autofocuser", "power_switch"):
        setattr(unit, name, None)
    return unit


def test_ontimer_does_not_end_startingup_while_startup_is_dispatching(ticking_unit):
    """The 2026-10-11 gap: between two components, no component StartingUp is up."""
    ticking_unit.startup()
    assert ticking_unit._startup_dispatching

    ticking_unit.ontimer()

    assert ticking_unit.is_active(UnitActivities.StartingUp)


def test_ontimer_ends_startingup_once_dispatched_and_no_component_is_starting(ticking_unit):
    ticking_unit.startup()
    ticking_unit._startup_dispatching = False  # do_startup's finally

    ticking_unit.ontimer()

    assert not ticking_unit.is_active(UnitActivities.StartingUp)


def test_do_startup_clears_its_dispatch_flag_even_when_a_component_raises(unit):
    class Broken:
        def startup(self):
            raise RuntimeError("no camera")

    unit.components = [Broken()]
    unit._startup_dispatching = True

    with pytest.raises(RuntimeError):
        unit.do_startup()

    assert unit._startup_dispatching is False


def test_opstate_cannot_be_assigned_from_outside(unit):
    """Components used to write the unit's opstate; there is one writer now."""
    with pytest.raises(AttributeError):
        unit.opstate = OpState.RUNNING  # type: ignore[misc]
