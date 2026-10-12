"""A unit shutdown: `do_shutdown` dispatches, `ontimer` ends ShuttingDown (#259, #253).

`do_shutdown` connects, waits for an in-flight startup to finish dispatching, aborts, and calls
every component's `shutdown()` except the power switch's. It does not wait for them. The unit's
`ontimer` ends `UnitActivities.ShuttingDown` -- after shutting the power switch down -- once
`do_shutdown` has returned and no component still reports its own `ShuttingDown`.

The "has returned" condition is the mast03 race of 2026-09-22: components are shut down one
after another, and a tick landing between two of them, with no component flag up, cleared the
unit's flag 0.75 s into a shutdown whose covers took 27.5 s more to close.

Bounded: past SHUTDOWN_SETTLE_TIMEOUT_SECONDS, whatever a component still reports is ended, so
`end_lifespan` and `do_powerdown` -- which wait on the flag unbounded -- are not hung on work
that will never finish. Teardown is `end_lifespan`'s, at process exit (opmode-design 5.2).
"""

from __future__ import annotations

import pytest

from common.activities import CoverActivities, FocuserActivities, StageActivities, Timing, UnitActivities


class FakeActivities:
    """The slice of the `Activities` mixin the shutdown uses, over a real IntFlag."""

    def __init__(self, flag_type, active=()):
        self.activities = flag_type(0)
        self._flag_type = flag_type
        self.timings: dict = {}
        for a in active:
            self.start_activity(a)
        self.ended: list = []

    def is_active(self, activity):
        return bool(self.activities & activity)

    def start_activity(self, activity, **kwargs):
        self.activities |= activity
        self.timings[activity] = Timing()

    def end_activity(self, activity, **kwargs):
        self.activities &= ~activity
        self.ended.append(activity)


class FakeComponent(FakeActivities):
    connected = True

    def __init__(self, name, flag_type, active=(), clears_on_shutdown=True, on_shutdown=None):
        super().__init__(flag_type, active)
        self._name = name
        self._clears = clears_on_shutdown
        self._on_shutdown = on_shutdown
        self.shutdown_called = False

    @property
    def name(self):
        return self._name

    def shutdown(self):
        self.shutdown_called = True
        self.start_activity(self._flag_type.ShuttingDown)
        if self._clears:
            self.end_activity(self._flag_type.ShuttingDown)
        if self._on_shutdown:
            self._on_shutdown()


class Outlet:
    """The power switch: a `SwitchedOutlet`, with no activities enum."""

    name = "power_switch"
    connected = True

    def __init__(self, record=None):
        self.shutdown_called = False
        self._record = record

    def shutdown(self):
        self.shutdown_called = True
        if self._record is not None:
            self._record.append("power_switch.shutdown")


class FakeTimer:
    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class FakeEvent:
    def __init__(self):
        self.was_set = False

    def set(self):
        self.was_set = True

    def is_set(self):
        return self.was_set


def _unit(components, power_switch=None):
    """A Unit that runs the real `do_shutdown` and `_end_shutting_down_when_done` over fakes."""
    from unit import Unit

    unit = object.__new__(Unit)
    unit.power_switch = power_switch
    unit.components = ([power_switch] if power_switch else []) + components
    unit.guider = None
    unit.timer = FakeTimer()
    unit.unit_shutdown_event = FakeEvent()
    unit._was_shut_down = False
    unit._startup_dispatching = False
    unit._shutdown_dispatching = False
    unit._shutdown_dispatched_at = None
    unit.abort = lambda: None  # do_shutdown aborts first; abort itself is tested elsewhere

    recorder = FakeActivities(UnitActivities)
    for attr in ("is_active", "start_activity", "end_activity", "timings"):
        setattr(unit, attr, getattr(recorder, attr))
    unit._recorder = recorder
    return unit


def _shut_down(unit):
    """`shutdown()` without its thread, then one `ontimer` tick."""
    unit.start_activity(UnitActivities.ShuttingDown)
    unit._shutdown_dispatching = True
    unit.do_shutdown()
    unit._end_shutting_down_when_done()


# ------------------------------------------------------------------ do_shutdown dispatches


def test_do_shutdown_dispatches_and_leaves_shuttingdown_to_ontimer():
    covers = FakeComponent("covers", CoverActivities, clears_on_shutdown=False)
    unit = _unit([covers])
    unit.start_activity(UnitActivities.ShuttingDown)

    unit.do_shutdown()

    assert covers.shutdown_called
    assert unit._recorder.is_active(UnitActivities.ShuttingDown), "not ended by do_shutdown"
    assert unit._shutdown_dispatching is False


def test_a_component_that_raises_does_not_stop_the_next():
    """The covers come after the imager: an imager that raises must not leave them open."""

    class Broken(FakeComponent):
        def shutdown(self):
            raise RuntimeError("imager refused")

    covers = FakeComponent("covers", CoverActivities)
    unit = _unit([Broken("imager", FocuserActivities), covers])

    _shut_down(unit)

    assert covers.shutdown_called
    assert not unit._recorder.is_active(UnitActivities.ShuttingDown)


def test_do_shutdown_clears_its_dispatch_flag_even_when_it_raises():
    """Otherwise ontimer could never end ShuttingDown, and end_lifespan would hang."""
    unit = _unit([FakeComponent("covers", CoverActivities)])

    def broken_abort():
        raise RuntimeError("abort blew up")

    unit.abort = broken_abort
    unit._shutdown_dispatching = True

    with pytest.raises(RuntimeError):
        unit.do_shutdown()

    assert unit._shutdown_dispatching is False


class Connectable(FakeComponent):
    """Records every `connected = ...` assignment, as the real setters act on each one."""

    def __init__(self, name, flag_type, connected, raises=False, record=None):
        super().__init__(name, flag_type)
        self._connected = connected
        self._raises = raises
        self.sets: list[bool] = []
        self._record = record

    @property
    def connected(self):
        return self._connected

    @connected.setter
    def connected(self, value):
        if self._raises:
            raise RuntimeError("no route")
        self.sets.append(value)
        self._connected = value
        if self._record is not None:
            self._record.append(f"{self.name}.connect")

    def shutdown(self):
        if self._record is not None:
            self._record.append(f"{self.name}.shutdown")
        super().shutdown()


def test_do_shutdown_connects_only_what_is_disconnected():
    """The covers do nothing when disconnected, so they are connected first. The stage, already
    connected, is left alone: reconnecting an open stage lost it (mast00, 2026-10-12)."""
    order: list[str] = []
    covers = Connectable("covers", CoverActivities, connected=False, record=order)
    stage = Connectable("stage", StageActivities, connected=True, record=order)
    unit = _unit([covers, stage], power_switch=Outlet())

    unit.do_shutdown()

    assert covers.sets == [True]
    assert stage.sets == [], "an already-connected component is not reconnected"
    assert order.index("covers.connect") < order.index("covers.shutdown")


def test_an_unreachable_power_switch_does_not_reconnect_the_components():
    """The switch reads disconnected when unreachable; that must not trigger a unit-wide
    reconnect, which is what its old constant `False` did on every shutdown."""
    stage = Connectable("stage", StageActivities, connected=True)
    switch = Outlet()
    switch.connected = False
    unit = _unit([stage], power_switch=switch)

    unit.do_shutdown()

    assert stage.sets == []


def test_a_connect_that_raises_does_not_cost_the_shutdown(caplog):
    covers = Connectable("covers", CoverActivities, connected=False, raises=True)
    stage = Connectable("stage", StageActivities, connected=False)
    unit = _unit([covers, stage])

    with caplog.at_level("ERROR"):
        unit.do_shutdown()

    assert covers.shutdown_called
    assert stage.sets == [True], "one failed connect does not stop the next"
    assert any("could not connect covers" in r.getMessage() for r in caplog.records)


def test_do_shutdown_leaves_the_timers_running():
    """start -> shut -> start: the unit timer is the only thing that ends StartingUp, and now
    ShuttingDown too (opmode-design 5.2)."""
    unit = _unit([FakeComponent("covers", CoverActivities)])

    unit.do_shutdown()

    assert unit.timer.cancelled is False
    assert unit.unit_shutdown_event.was_set is False


# ------------------------------------------------------------ ontimer ends ShuttingDown


def test_ontimer_ends_shuttingdown_once_every_component_has_ended_its_own():
    covers = FakeComponent("covers", CoverActivities, clears_on_shutdown=False)
    unit = _unit([covers])

    _shut_down(unit)
    assert unit._recorder.is_active(UnitActivities.ShuttingDown), "the covers are still closing"

    covers.end_activity(CoverActivities.ShuttingDown)  # as the covers' own ontimer does
    unit._end_shutting_down_when_done()

    assert not unit._recorder.is_active(UnitActivities.ShuttingDown)
    assert unit._was_shut_down is True


def test_a_tick_between_two_components_does_not_end_shuttingdown():
    """The mast03 race: the focuser has finished, the covers have not started."""
    unit = _unit([])
    focuser = FakeComponent("focuser", FocuserActivities, on_shutdown=lambda: unit._end_shutting_down_when_done())
    covers = FakeComponent("covers", CoverActivities, clears_on_shutdown=False)
    unit.components = [focuser, covers]

    unit.start_activity(UnitActivities.ShuttingDown)
    unit._shutdown_dispatching = True
    unit.do_shutdown()

    assert unit._recorder.is_active(UnitActivities.ShuttingDown)


def test_shuttingdown_is_not_ended_while_do_shutdown_has_not_started():
    """`shutdown()` raises the dispatch flag before its thread, so a tick in between waits."""
    unit = _unit([FakeComponent("covers", CoverActivities)])
    unit.start_activity(UnitActivities.ShuttingDown)
    unit._shutdown_dispatching = True

    unit._end_shutting_down_when_done()

    assert unit._recorder.is_active(UnitActivities.ShuttingDown)


def test_the_power_switch_is_shut_down_last_by_ontimer():
    order: list[str] = []
    covers = FakeComponent("covers", CoverActivities, clears_on_shutdown=False)
    original = covers.shutdown

    def record():
        order.append("covers.shutdown")
        original()

    covers.shutdown = record
    switch = Outlet(order)
    unit = _unit([covers], power_switch=switch)

    _shut_down(unit)
    assert not switch.shutdown_called, "not while the covers are still closing"

    covers.end_activity(CoverActivities.ShuttingDown)
    unit._end_shutting_down_when_done()

    assert order == ["covers.shutdown", "power_switch.shutdown"]


def test_the_settle_bound_counts_from_the_end_of_dispatch_not_the_start_of_shutdown():
    """mast00, 2026-10-12: a 120 s abort wait spent a bound counted from the start, and the
    covers were swept 6 s into their close -- shutdown reported done with the mirror moving."""
    import datetime

    covers = FakeComponent("covers", CoverActivities, active=[CoverActivities.Closing], clears_on_shutdown=False)
    unit = _unit([covers])
    unit.start_activity(UnitActivities.ShuttingDown)
    # ShuttingDown raised long ago -- the abort wait took most of it ...
    unit.timings[UnitActivities.ShuttingDown].start_time -= datetime.timedelta(seconds=200)
    unit._shutdown_dispatching = True
    unit.do_shutdown()  # ... and dispatch has only just finished.

    unit._end_shutting_down_when_done()

    assert covers.is_active(CoverActivities.Closing), "the covers' close is not cut short"
    assert unit._recorder.is_active(UnitActivities.ShuttingDown)


def test_a_component_that_never_settles_is_swept_after_the_timeout(monkeypatch, caplog):
    import unit as unit_module

    monkeypatch.setattr(unit_module, "SHUTDOWN_SETTLE_TIMEOUT_SECONDS", 0.0)
    covers = FakeComponent("covers", CoverActivities, active=[CoverActivities.Closing], clears_on_shutdown=False)
    unit = _unit([covers])

    with caplog.at_level("ERROR"):
        _shut_down(unit)

    assert covers.activities == CoverActivities(0), f"stranded {covers.activities!r}"
    assert CoverActivities.ShuttingDown in covers.ended
    assert not unit._recorder.is_active(UnitActivities.ShuttingDown)
    assert any("covers: still reporting" in r.getMessage() for r in caplog.records)


def test_before_the_timeout_nothing_is_swept():
    covers = FakeComponent("covers", CoverActivities, active=[CoverActivities.Closing], clears_on_shutdown=False)
    unit = _unit([covers])

    _shut_down(unit)

    assert covers.is_active(CoverActivities.Closing)
    assert unit._recorder.is_active(UnitActivities.ShuttingDown)


# -------------------------------------------------------------------------- end_lifespan


def test_end_lifespan_tears_down_only_after_shuttingdown_ends(monkeypatch):
    """Order is the whole defect: tearing down first kills the timers that end the flags."""
    import unit as unit_module

    covers = FakeComponent("covers", CoverActivities, clears_on_shutdown=False)
    order: list[str] = []
    unit = _unit([covers])
    unit.timer.cancel = lambda: order.append("timer_cancelled")
    unit.unit_shutdown_event.set = lambda: order.append("event_set")
    unit.shutdown = lambda: _shut_down(unit)

    def tick(_seconds):
        # The covers close, then the unit timer runs.
        covers.end_activity(CoverActivities.ShuttingDown)
        unit._end_shutting_down_when_done()
        order.append("tick")

    monkeypatch.setattr(unit_module.time, "sleep", tick)

    unit.end_lifespan()

    assert order == ["tick", "timer_cancelled", "event_set"]
    assert unit._was_shut_down is True


# ------------------------------------------------------------- abort before shutdown (5.3)


def test_shutdown_aborts_before_it_shuts_any_component_down():
    """Nothing may still be exposing or guiding while the covers close."""
    order: list[str] = []
    covers = FakeComponent("covers", CoverActivities)
    original_shutdown = covers.shutdown

    def record_shutdown():
        order.append("covers.shutdown")
        original_shutdown()

    covers.shutdown = record_shutdown
    unit = _unit([covers])
    unit.abort = lambda: order.append("abort")

    unit.do_shutdown()

    assert order == ["abort", "covers.shutdown"]


def test_shutdown_waits_for_aborting_to_end(monkeypatch):
    import unit as unit_module

    covers = FakeComponent("covers", CoverActivities)
    unit = _unit([covers])
    unit.abort = lambda: unit.start_activity(UnitActivities.Aborting)

    waits: list[bool] = []

    def fake_sleep(_seconds):
        # The flag clears while do_shutdown waits, as ontimer would end it.
        waits.append(covers.shutdown_called)
        unit.end_activity(UnitActivities.Aborting)

    monkeypatch.setattr(unit_module.time, "sleep", fake_sleep)

    unit.do_shutdown()

    assert waits == [False], "waited once, before any component was shut down"
    assert covers.shutdown_called


def test_an_abort_that_never_settles_does_not_stop_the_shutdown(monkeypatch, caplog):
    """Bounded on purpose: shutdown is the make-safe path, and a run that ignores its abort
    must not keep the covers open."""
    import unit as unit_module

    monkeypatch.setattr(unit_module, "ABORT_SETTLE_TIMEOUT_SECONDS", 0.0)
    covers = FakeComponent("covers", CoverActivities)
    unit = _unit([covers])
    unit.abort = lambda: unit.start_activity(UnitActivities.Aborting)

    with caplog.at_level("ERROR"):
        unit.do_shutdown()

    assert covers.shutdown_called
    assert any("abort not settled" in r.getMessage() for r in caplog.records)


def test_unsettled_names_unit_work_and_component_aborts():
    covers = FakeComponent("covers", CoverActivities, active=[CoverActivities.Aborting])
    unit = _unit([covers])
    unit.start_activity(UnitActivities.Exposing)
    unit.start_activity(UnitActivities.ShuttingDown)  # lifecycle, not work: not listed

    assert unit._unsettled_by_abort() == ["Exposing", "covers.Aborting"]

    unit.end_activity(UnitActivities.Exposing)
    covers.end_activity(CoverActivities.Aborting)
    assert unit._unsettled_by_abort() == []


# ---------------------------------------------------------------- shutdown during startup


def test_shutdown_waits_for_an_in_flight_startup_to_finish_dispatching(monkeypatch):
    """Else covers.startup(), still to come, opens the covers this shutdown closed."""
    import unit as unit_module

    covers = FakeComponent("covers", CoverActivities)
    unit = _unit([covers])
    unit._startup_dispatching = True
    waits: list[bool] = []

    def fake_sleep(_seconds):
        waits.append(covers.shutdown_called)
        unit._startup_dispatching = False  # do_startup's finally

    monkeypatch.setattr(unit_module.time, "sleep", fake_sleep)

    unit.do_shutdown()

    assert waits == [False], "waited before shutting anything down"
    assert covers.shutdown_called


def test_a_startup_that_never_finishes_dispatching_does_not_stop_the_shutdown(monkeypatch, caplog):
    import unit as unit_module

    monkeypatch.setattr(unit_module, "STARTUP_DISPATCH_TIMEOUT_SECONDS", 0.0)
    covers = FakeComponent("covers", CoverActivities)
    unit = _unit([covers])
    unit._startup_dispatching = True

    with caplog.at_level("ERROR"):
        unit.do_shutdown()

    assert covers.shutdown_called
    assert any("startup still dispatching" in r.getMessage() for r in caplog.records)
