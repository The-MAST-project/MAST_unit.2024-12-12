"""A unit shutdown waits for its components before cancelling their timers (#259, #253).

`do_shutdown()` used to raise component activities and then kill the timers that would clear
them -- the unit's own, and every component's via `unit_shutdown_event`. Measured on mast03
2026-09-22: the covers held `["Closing", "ShuttingDown"]` indefinitely while the mount, which
ends its flags inline, came out clean on the same event.

The invariant under test is one line: do not cancel a timer while an activity depends on it.
"""

from __future__ import annotations

import pytest

from common.activities import CoverActivities, UnitActivities


class FakeActivities:
    """The slice of the `Activities` mixin `do_shutdown` uses, over a real IntFlag."""

    def __init__(self, flag_type, active=()):
        self.activities = flag_type(0)
        self._flag_type = flag_type
        for a in active:
            self.activities |= a
        self.ended: list = []

    def is_active(self, activity):
        return bool(self.activities & activity)

    def start_activity(self, activity, **kwargs):
        self.activities |= activity

    def end_activity(self, activity, **kwargs):
        self.activities &= ~activity
        self.ended.append(activity)

    def await_activity_clear(self, activity, *, timeout, interval=0.2):
        """The real one polls; these fakes settle or not by construction, so answer at once."""
        return not self.is_active(activity)


class FakeComponent(FakeActivities):
    def __init__(self, name, flag_type, active=(), clears_on_shutdown=True):
        super().__init__(flag_type, active)
        self._name = name
        self._clears = clears_on_shutdown
        self.shutdown_called = False

    @property
    def name(self):
        return self._name

    def shutdown(self):
        self.shutdown_called = True
        self.start_activity(self._flag_type.ShuttingDown)
        if self._clears:
            self.end_activity(self._flag_type.ShuttingDown)


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


def _unit(components):
    """A Unit that runs the real `do_shutdown` over fake components."""
    from unit import Unit

    unit = object.__new__(Unit)
    unit.components = components
    unit.guider = None
    unit.timer = FakeTimer()
    unit.unit_shutdown_event = FakeEvent()
    unit._was_shut_down = False

    recorder = FakeActivities(UnitActivities)
    for attr in ("is_active", "start_activity", "end_activity"):
        setattr(unit, attr, getattr(recorder, attr))
    unit._recorder = recorder
    return unit


def test_a_component_that_settles_is_not_swept():
    covers = FakeComponent("covers", CoverActivities, clears_on_shutdown=True)
    unit = _unit([covers])

    unit.do_shutdown()

    assert covers.shutdown_called
    assert covers.activities == CoverActivities(0)


def test_a_component_that_never_settles_is_ended_rather_than_stranded():
    """The mast03 case. The covers leave `Closing` / `ShuttingDown` to their own `ontimer`,
    and that timer is about to be cancelled, so nothing else can ever take them down."""
    covers = FakeComponent(
        "covers",
        CoverActivities,
        active=[CoverActivities.Closing],
        clears_on_shutdown=False,
    )
    unit = _unit([covers])

    unit.do_shutdown()

    assert covers.activities == CoverActivities(0), f"stranded {covers.activities!r}"
    assert CoverActivities.Closing in covers.ended
    assert CoverActivities.ShuttingDown in covers.ended


def test_the_sweep_happens_before_the_timers_are_cancelled():
    """Order is the whole defect: sweeping after the event is set would be sweeping after the
    component timers are already dead, which is where the stranded flags come from."""
    covers = FakeComponent("covers", CoverActivities, clears_on_shutdown=False)
    order: list[str] = []

    unit = _unit([covers])
    unit.timer.cancel = lambda: order.append("timer_cancelled")
    unit.unit_shutdown_event.set = lambda: order.append("event_set")
    original_end = covers.end_activity

    def record_end(activity, **kwargs):
        order.append("component_flag_ended")
        original_end(activity, **kwargs)

    covers.end_activity = record_end

    unit.do_shutdown()

    assert "component_flag_ended" in order
    assert order.index("component_flag_ended") < order.index("timer_cancelled")
    assert order.index("component_flag_ended") < order.index("event_set")


def test_unit_shutting_down_is_ended_by_do_shutdown_itself():
    """`Unit.ontimer` is the only other place that ends it, and `do_shutdown` cancels that
    timer -- the same trap #193 fixed on the mount, one level up."""
    unit = _unit([FakeComponent("covers", CoverActivities)])

    unit.do_shutdown()

    assert not unit._recorder.is_active(UnitActivities.ShuttingDown)
    assert unit._was_shut_down is True


def test_the_teardown_still_happens():
    unit = _unit([FakeComponent("covers", CoverActivities)])

    unit.do_shutdown()

    assert unit.timer.cancelled is True
    assert unit.unit_shutdown_event.was_set is True


def test_a_component_with_no_activities_is_skipped_not_crashed_on():
    """`self.components` carries the power switch, which is a `SwitchedOutlet` rather than a
    component with an activities enum."""

    class Outlet:
        name = "power_switch"

        def shutdown(self):
            pass

    unit = _unit([Outlet(), FakeComponent("covers", CoverActivities)])

    unit.do_shutdown()

    assert unit.unit_shutdown_event.was_set is True


@pytest.mark.parametrize("stuck", [CoverActivities.Opening, CoverActivities.Closing])
def test_any_stuck_activity_is_swept_not_just_shuttingdown(stuck):
    covers = FakeComponent("covers", CoverActivities, active=[stuck], clears_on_shutdown=True)
    unit = _unit([covers])

    unit.do_shutdown()

    assert covers.activities == CoverActivities(0)


def test_ontimer_does_not_end_unit_shuttingdown():
    """The race measured on mast03, 2026-09-22.

    `do_shutdown` shuts components down one after another. Between one finishing and the next
    starting, no component reports `ShuttingDown` -- and the old `ontimer` branch read that
    window as "the unit is down". The flag cleared 0.75 s into a shutdown whose covers took
    27.5 s more to close, so the declared completion of `PUT /unit/shutdown` went clear while
    the mirror was still moving.

    `do_shutdown` ends it once the components have settled, so `ontimer` must not race it.
    """
    import inspect

    from unit import Unit

    source = inspect.getsource(Unit.ontimer)
    assert "end_activity(UnitActivities.ShuttingDown)" not in source


def test_do_shutdown_ends_unit_shuttingdown_after_the_components_settle():
    """The positive half: removing the ontimer branch must not leave the flag stranded -- the
    trap #193 and #259 are both about."""
    covers = FakeComponent("covers", CoverActivities, clears_on_shutdown=True)
    order: list[str] = []

    unit = _unit([covers])
    original_end = unit.end_activity

    def record_end(activity, **kwargs):
        if activity == UnitActivities.ShuttingDown:
            order.append("unit_flag_ended")
        original_end(activity, **kwargs)

    unit.end_activity = record_end
    original_await = unit._await_components_at_rest

    def record_await():
        order.append("components_settled")
        original_await()

    unit._await_components_at_rest = record_await

    unit.do_shutdown()

    assert order == ["components_settled", "unit_flag_ended"]
    assert not unit._recorder.is_active(UnitActivities.ShuttingDown)
