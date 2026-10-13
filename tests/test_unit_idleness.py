"""Unit idleness: `why_busy`, the idle clock, and the auto-shutdown.

Design: mast-claude-config plans/unit_idleness_design.md (rev. 2026-10-13). Busy is the unit's
activities, every component's (the imager's wrapper and backend together) and guiding -- not
`ImagerActivities.CoolingDown`, not tracking. The clock runs only while RUNNING, restarts at
`startup`, and after `minutes_to_autoshutdown_on_idle` idle (0 disables) the unit shuts down.

The unit is never constructed (that builds every component); a bare instance carries fake
components, a stubbed `shutdown`, and a fake monotonic clock.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

import unit as unit_module
from common.activities import UnitActivities
from common.opmode import OpmodeBase, OpState
from unit import Unit


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _component(*flags):
    return SimpleNamespace(activities_verbal=list(flags) or None)


class _Unit(Unit):
    """`unit_conf` is a live Config read; here it is whatever the test sets."""

    conf = None

    @property
    def unit_conf(self):
        return self.conf


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(unit_module.time, "monotonic", c)
    return c


@pytest.fixture
def unit():
    u = object.__new__(_Unit)
    OpmodeBase.__init__(u)
    u.activities = UnitActivities(0)
    u.guider = None
    u._idle_since = u._active_since = None
    u._guiding_live = False
    for owner in Unit._BUSY_OWNERS:
        setattr(u, owner, _component())
    u.imager = SimpleNamespace(activities_verbal=None, _backend=_component())
    u.conf = SimpleNamespace(minutes_to_autoshutdown_on_idle=0)
    u.shutdowns = []

    def shutdown():
        u.shutdowns.append(1)
        u._set_opstate(OpState.SHUTDOWN)

    u.shutdown = shutdown
    u._set_opstate(OpState.RUNNING)
    return u


class TestWhyBusy:
    def test_an_idle_unit_has_nothing(self, unit):
        assert unit.why_busy == {}
        assert unit.is_idle() is True

    def test_a_component_activity_alone_is_busy(self, unit):
        """A direct mount slew raises no unit flag; it must still count."""
        unit.mount = _component("Slewing")

        assert unit.why_busy == {"mount": ["Slewing"]}
        assert unit.is_idle() is False

    def test_the_unit_s_own_activities_count(self, unit):
        unit.activities = UnitActivities.Acquiring

        assert unit.why_busy == {"unit": ["Acquiring"]}

    def test_cooling_down_alone_is_not_busy(self, unit):
        unit.imager.activities_verbal = ["CoolingDown"]

        assert unit.why_busy == {}

    def test_the_imager_backend_s_activities_join_the_wrapper_s(self, unit):
        unit.imager.activities_verbal = ["Exposing", "CoolingDown"]
        unit.imager._backend = _component("Guiding", "Exposing")

        assert unit.why_busy == {"imager": ["Exposing", "Guiding"]}

    def test_phd2_guiding_counts_even_with_the_unit_flag_down(self, unit):
        unit._guiding_live = True

        assert unit.why_busy == {"unit": ["Guiding"]}


class TestTheGuidingCache:
    def test_it_reads_the_guider(self, unit):
        unit.guider = SimpleNamespace(is_guiding=True)

        unit._refresh_guiding_live()

        assert unit._guiding_live is True

    def test_a_failed_read_keeps_the_last_answer(self, unit):
        class Broken:
            @property
            def is_guiding(self):
                raise RuntimeError("PHD2 did not answer")

        unit._guiding_live = True
        unit.guider = Broken()

        unit._refresh_guiding_live()

        assert unit._guiding_live is True, "guessing 'not guiding' could shut down a unit in use"


class TestTheClock:
    def test_idle_then_busy_then_idle_stamps_the_marks(self, unit, clock):
        unit._track_idleness()
        assert unit._idle_since == 1000.0

        clock.now = 1010.0
        unit.mount = _component("Slewing")
        unit._track_idleness()
        assert (unit._idle_since, unit._active_since) == (None, 1010.0)

        clock.now = 1030.0
        unit.mount = _component()
        unit._track_idleness()
        assert (unit._idle_since, unit._active_since) == (1030.0, None)

    def test_overlapping_activities_stay_active_until_the_last_ends(self, unit, clock):
        unit.activities = UnitActivities.Acquiring | UnitActivities.Solving
        unit._track_idleness()
        clock.now = 1005.0
        unit.activities = UnitActivities.Solving
        unit._track_idleness()

        assert unit._active_since == 1000.0, "still active, the mark unchanged"

    @pytest.mark.parametrize("opstate", [OpState.INITIALIZED, OpState.SHUTDOWN])
    def test_outside_running_it_does_not_run(self, unit, clock, opstate):
        unit._idle_since = 500.0
        unit._set_opstate(opstate)

        unit._track_idleness()

        assert (unit._idle_since, unit._active_since) == (None, None)

    def test_startup_restarts_it(self, unit, monkeypatch):
        monkeypatch.setattr(unit_module, "Thread", lambda **_: SimpleNamespace(start=lambda: None))
        unit.start_activity = lambda *_a, **_k: None
        unit.is_active = lambda _a: False
        unit._idle_since = 500.0

        unit.startup()

        assert unit._idle_since is None


class TestAutoShutdown:
    def test_it_fires_at_the_configured_minutes_and_not_before(self, unit, clock, caplog):
        unit.conf.minutes_to_autoshutdown_on_idle = 5
        unit._track_idleness()  # idle from 1000

        clock.now = 1000.0 + 5 * 60 - 1
        unit._track_idleness()
        assert unit.shutdowns == []

        clock.now = 1000.0 + 5 * 60
        with caplog.at_level(logging.INFO):
            unit._track_idleness()
        assert unit.shutdowns == [1]
        assert "auto-shutdown: idle for 5 min (minutes_to_autoshutdown_on_idle=5)" in caplog.text

    def test_it_does_not_fire_twice(self, unit, clock):
        unit.conf.minutes_to_autoshutdown_on_idle = 5
        unit._track_idleness()
        clock.now += 5 * 60
        unit._track_idleness()
        clock.now += 5 * 60
        unit._track_idleness()

        assert unit.shutdowns == [1]

    def test_zero_disables_it(self, unit, clock):
        unit._track_idleness()
        clock.now += 24 * 3600
        unit._track_idleness()

        assert unit.shutdowns == []

    def test_busy_time_does_not_count(self, unit, clock):
        unit.conf.minutes_to_autoshutdown_on_idle = 5
        unit._track_idleness()
        clock.now += 4 * 60
        unit.guider = SimpleNamespace(is_guiding=True)  # guiding: busy (re-read every tick)
        unit._track_idleness()
        clock.now += 10 * 60
        unit.guider = SimpleNamespace(is_guiding=False)
        unit._track_idleness()  # idle again, from now
        clock.now += 4 * 60
        unit._track_idleness()

        assert unit.shutdowns == []

    def test_a_config_change_applies_on_the_next_tick(self, unit, clock):
        unit._track_idleness()
        clock.now += 10 * 60
        unit._track_idleness()
        assert unit.shutdowns == []

        unit.conf.minutes_to_autoshutdown_on_idle = 5
        unit._track_idleness()

        assert unit.shutdowns == [1]


class TestTheStatusFields:
    def test_while_idle(self, unit, clock):
        unit._track_idleness()
        clock.now += 42.0

        assert unit._idleness_status() == {"idle_seconds": 42.0, "active_seconds": None, "why_busy": {}}

    def test_while_busy(self, unit, clock):
        unit.covers = _component("Closing")
        unit._track_idleness()
        clock.now += 7.0

        assert unit._idleness_status() == {"idle_seconds": None, "active_seconds": 7.0, "why_busy": {"covers": ["Closing"]}}

    def test_outside_running_all_are_unknown(self, unit):
        unit._set_opstate(OpState.INITIALIZED)

        assert unit._idleness_status() == {"idle_seconds": None, "active_seconds": None, "why_busy": None}

    def test_a_component_that_cannot_be_read_makes_them_unknown_not_a_failed_status(self, unit, caplog):
        """Status survives a sick component (#222); idleness then reads as unknown."""

        class Sick:
            @property
            def activities_verbal(self):
                raise RuntimeError("component gone")

        unit.mount = Sick()

        with caplog.at_level(logging.ERROR):
            fields = unit._idleness_status()

        assert fields == {"idle_seconds": None, "active_seconds": None, "why_busy": None}
        assert "could not compute why_busy" in caplog.text
