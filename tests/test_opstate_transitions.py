"""The unit's `opstate`: the lifecycle state it has reached (opmode-design 4, 4a).

INITIALIZING -> INITIALIZED (end of start_lifespan, both opmodes) -> STARTED_UP <-> SHUT_DOWN.
STARTED_UP and SHUT_DOWN are set when the startup / shutdown sequence COMPLETES -- where the
unit's StartingUp / ShuttingDown activity ends -- not on receipt of the command, and only by
the most recent lifecycle command, so a shutdown received mid-startup ends in SHUT_DOWN.
(Until 2026-10-13: RUNNING / SHUTDOWN, set on receipt.) Health stays in `operational`.
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
    """Records the target instead of running it: these tests drive the sequence by hand."""

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
    u.power_switch = None
    u._was_shut_down = False
    u._startup_dispatching = False
    u._shutdown_dispatching = False
    u._shutdown_dispatched_at = None
    u._last_lifecycle_command = None
    flags = _Flags()
    for name in ("is_active", "start_activity", "end_activity"):
        setattr(u, name, getattr(flags, name))
    return u


@pytest.fixture
def ticking_unit(unit, monkeypatch):
    """`unit`, plus what `ontimer` reads, with no components and no autofocus."""
    for name in ("mount", "imager", "stage", "focuser", "covers", "pw", "autofocuser", "guider"):
        setattr(unit, name, None)
    # What the idle clock (`_track_idleness`) reads; `unit_conf` would otherwise be a live Config read.
    unit.activities = UnitActivities(0)
    unit._idle_since = unit._active_since = None
    unit._guiding_live = False
    monkeypatch.setattr(type(unit), "unit_conf", property(lambda self: None))
    return unit


def _finish_startup(u):
    """What `do_startup`'s finally and the next `ontimer` tick do once the components are up."""
    u._startup_dispatching = False
    u.ontimer()


def _finish_shutdown(u):
    """What `do_shutdown`'s finally and the next `ontimer` tick do once the components are down."""
    u._shutdown_dispatching = False
    u.ontimer()


# --------------------------------------------------------------------------- start_lifespan


def test_a_new_unit_is_initializing(unit):
    assert unit.opstate is OpState.INITIALIZING


def test_production_start_lifespan_reports_initialized_and_does_not_start(unit):
    unit.start_lifespan()

    assert unit.opstate is OpState.INITIALIZED
    assert _NoThread.started == []


def test_development_start_lifespan_is_initialized_while_its_startup_runs(unit):
    """INITIALIZED in both modes; development then starts, and is STARTED_UP only on completion
    -- not INITIALIZING, visibly, for the length of its first startup."""
    unit._opmode = OpMode.DEVELOPMENT

    unit.start_lifespan()

    assert unit.opstate is OpState.INITIALIZED
    assert unit.is_active(UnitActivities.StartingUp)
    assert _NoThread.started == ["unit-startup-thread"]


# -------------------------------------------------------------------------------- startup


def test_startup_changes_no_opstate_on_receipt(unit):
    unit.start_lifespan()

    unit.startup()

    assert unit.opstate is OpState.INITIALIZED, "the transition is the StartingUp activity"
    assert unit.is_active(UnitActivities.StartingUp), "raised before the thread, not in it"


def test_startup_completes_into_started_up(ticking_unit):
    ticking_unit.start_lifespan()
    ticking_unit.startup()

    _finish_startup(ticking_unit)

    assert not ticking_unit.is_active(UnitActivities.StartingUp)
    assert ticking_unit.opstate is OpState.STARTED_UP


def test_not_started_up_while_startup_is_still_dispatching(ticking_unit):
    """The 2026-10-11 gap: between two components, no component StartingUp is up."""
    ticking_unit.start_lifespan()
    ticking_unit.startup()
    assert ticking_unit._startup_dispatching

    ticking_unit.ontimer()

    assert ticking_unit.is_active(UnitActivities.StartingUp)
    assert ticking_unit.opstate is OpState.INITIALIZED


def test_a_second_startup_while_starting_starts_nothing_more(unit):
    unit.startup()
    unit.startup()

    assert _NoThread.started == ["unit-startup-thread"]


# ------------------------------------------------------------------------------- shutdown


def test_shutdown_from_initialized_completes_into_shut_down(ticking_unit):
    """How a supervisor makes a never-started machine safe."""
    ticking_unit.start_lifespan()

    ticking_unit.shutdown()
    assert ticking_unit.opstate is OpState.INITIALIZED, "not on receipt"
    assert ticking_unit.is_active(UnitActivities.ShuttingDown), "raised before the thread, not in it"

    _finish_shutdown(ticking_unit)

    assert ticking_unit.opstate is OpState.SHUT_DOWN


def test_a_started_up_unit_stays_started_up_while_shutting_down(ticking_unit):
    ticking_unit.startup()
    _finish_startup(ticking_unit)

    ticking_unit.shutdown()
    assert ticking_unit.opstate is OpState.STARTED_UP

    _finish_shutdown(ticking_unit)
    assert ticking_unit.opstate is OpState.SHUT_DOWN


def test_a_shutdown_received_mid_startup_wins(ticking_unit):
    """The startup finishing after the shutdown was received must not overwrite it."""
    ticking_unit.start_lifespan()
    ticking_unit.startup()

    ticking_unit.shutdown()
    _finish_startup(ticking_unit)  # StartingUp ends, after the shutdown was received
    assert ticking_unit.opstate is OpState.INITIALIZED

    _finish_shutdown(ticking_unit)
    assert ticking_unit.opstate is OpState.SHUT_DOWN


def test_a_startup_received_mid_shutdown_wins(ticking_unit):
    ticking_unit.startup()
    _finish_startup(ticking_unit)
    ticking_unit.shutdown()

    ticking_unit.end_activity(UnitActivities.StartingUp)  # (none up; startup may begin)
    ticking_unit.startup()
    _finish_shutdown(ticking_unit)  # ShuttingDown ends, after the startup was received
    assert ticking_unit.opstate is OpState.STARTED_UP, "not SHUT_DOWN: the startup came later"

    _finish_startup(ticking_unit)
    assert ticking_unit.opstate is OpState.STARTED_UP


def test_shutdown_is_idempotent_and_startup_returns_to_started_up(ticking_unit, monkeypatch):
    monkeypatch.setattr(type(ticking_unit), "connected", property(lambda self: True))
    ticking_unit.startup()
    _finish_startup(ticking_unit)

    ticking_unit.shutdown()
    ticking_unit.shutdown()
    _finish_shutdown(ticking_unit)
    assert ticking_unit.opstate is OpState.SHUT_DOWN

    ticking_unit.startup()
    assert ticking_unit.opstate is OpState.SHUT_DOWN, "until the startup completes"
    _finish_startup(ticking_unit)
    assert ticking_unit.opstate is OpState.STARTED_UP


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


# ----------------------------------------------------------------------------- dispatching


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
        unit.opstate = OpState.STARTED_UP  # type: ignore[misc]
