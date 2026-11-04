"""Autostartup: a route declared `autostartup=True` starts the unit up first.

`Unit._autostartup` starts the unit -- or joins a startup already running -- waits up to
`seconds_to_autostartup` for it to complete, and answers with `why_not_operational` when it does
not, or when the unit is up but unfit; it refuses while the unit is shutting down. `startup`
itself is a no-op on a unit already up and fit, so a caller need not ask first.

The unit is never constructed (that builds every component); a bare instance carries a fake
`startup` that `ontimer` completes on the fake clock, and a fake fitness.
"""

from __future__ import annotations

import inspect
import threading
from types import SimpleNamespace

import pytest

import unit as unit_module
from common.activities import UnitActivities
from common.canonical import CanonicalResponse
from common.models.statuses import COOLING_DOWN_REASON
from common.opmode import OpmodeBase, OpState
from unit import Unit


class _Clock:
    """`time.monotonic` and `time.sleep` together: sleeping advances the clock, and runs the
    fake `ontimer` once per tick, which is what completes a startup."""

    def __init__(self):
        self.now = 1000.0
        self.on_tick = lambda: None

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        self.on_tick()


class _Unit(Unit):
    conf = None
    fit = True
    reasons: list[str] = []

    @property
    def unit_conf(self):
        return self.conf

    @property
    def operational(self):
        return self.fit

    @property
    def why_not_operational(self):
        return list(self.reasons)


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(unit_module.time, "monotonic", c.monotonic)
    monkeypatch.setattr(unit_module.time, "sleep", c.sleep)
    return c


@pytest.fixture
def unit(clock):
    u = object.__new__(_Unit)
    OpmodeBase.__init__(u)
    u.activities = UnitActivities(0)
    u.lock = threading.RLock()
    u.conf = SimpleNamespace(seconds_to_autostartup=120)
    u._last_lifecycle_command = None
    u.startups = 0
    u.startup_seconds = 34.0  # how long the fake sequence takes; None never completes

    def startup():
        u.startups += 1
        u._last_lifecycle_command = "startup"
        u.activities |= UnitActivities.StartingUp
        u.started_at = clock.now

    def ontimer():
        if not u.activities & UnitActivities.StartingUp or u.startup_seconds is None:
            return
        if clock.now - u.started_at >= u.startup_seconds:
            u.activities &= ~UnitActivities.StartingUp
            if u._last_lifecycle_command == "startup":
                u._set_opstate(OpState.STARTED_UP)

    u.startup = startup
    clock.on_tick = ontimer
    u._set_opstate(OpState.INITIALIZED)
    return u


class TestTheGate:
    def test_a_started_up_fit_unit_runs_at_once(self, unit, clock):
        unit._set_opstate(OpState.STARTED_UP)

        assert unit._autostartup("expose") is None
        assert unit.startups == 0
        assert clock.now == 1000.0

    @pytest.mark.parametrize("opstate", [OpState.INITIALIZED, OpState.SHUT_DOWN])
    def test_a_unit_not_up_is_started_and_waited_for(self, unit, clock, opstate):
        unit._set_opstate(opstate)

        assert unit._autostartup("expose") is None
        assert unit.startups == 1
        assert unit.opstate == OpState.STARTED_UP
        assert 1034.0 <= clock.now < 1035.0

    def test_a_running_startup_is_joined_not_restarted(self, unit, clock):
        unit.startup()

        assert unit._autostartup("expose") is None
        assert unit.startups == 1

    def test_a_startup_past_the_wait_answers_why_and_is_left_running(self, unit, clock):
        unit.startup_seconds = None
        unit.reasons = ["covers: still opening"]

        errors = unit._autostartup("focus_on_star")

        assert errors[0] == "autostartup(focus_on_star): not started up after 120 s (startup left running)"
        assert errors[1:] == ["covers: still opening"]
        assert unit.is_active(UnitActivities.StartingUp)

    def test_the_wait_is_read_live(self, unit, clock):
        unit.startup_seconds = None
        unit.conf = SimpleNamespace(seconds_to_autostartup=10)

        errors = unit._autostartup("expose")

        assert "after 10 s" in errors[0]
        assert clock.now - 1000.0 == pytest.approx(10.0)

    @pytest.mark.parametrize("flag", [UnitActivities.ShuttingDown, UnitActivities.PoweringDown])
    def test_a_unit_on_its_way_down_is_refused(self, unit, flag):
        unit._set_opstate(OpState.STARTED_UP)
        unit.activities |= flag

        assert unit._autostartup("expose") == ["autostartup(expose): the unit is shutting down; not starting it up"]
        assert unit.startups == 0

    def test_a_shutdown_arriving_during_the_wait_ends_it(self, unit, clock):
        def shutdown_midway():
            if clock.now >= 1010.0:
                unit._last_lifecycle_command = "shutdown"

        unit.startup_seconds = None
        clock.on_tick = shutdown_midway

        assert unit._autostartup("expose") == ["autostartup(expose): a shutdown arrived while starting up"]

    def test_up_but_unfit_answers_why(self, unit):
        unit._set_opstate(OpState.STARTED_UP)
        unit.fit = False
        unit.reasons = ["mount: not connected"]

        assert unit._autostartup("expose") == [
            "autostartup(expose): started up but not operational",
            "mount: not connected",
        ]

    def test_up_but_unfit_with_no_reason_says_so(self, unit):
        unit._set_opstate(OpState.STARTED_UP)
        unit.fit = False

        assert unit._autostartup("expose")[1:] == ["no reason given"]

    def test_only_cooling_is_fit(self, unit):
        """The rule MAST_control applies: a unit kept from operational only by cooling may work."""
        unit._set_opstate(OpState.STARTED_UP)
        unit.fit = False
        unit.reasons = [f"{COOLING_DOWN_REASON}, -3.0 C, set point -10.0 C"]

        assert unit._autostartup("expose") is None


class TestTheWrapper:
    def test_the_handler_runs_once_the_unit_is_up(self, unit):
        def expose(seconds: float = 1.0):
            return {"exposed": seconds}

        assert unit._autostarted(expose)(seconds=3.0) == {"exposed": 3.0}
        assert unit.startups == 1

    def test_a_refusal_answers_the_errors_and_skips_the_handler(self, unit):
        ran = []
        unit.activities |= UnitActivities.ShuttingDown

        response = unit._autostarted(lambda: ran.append(1))()

        assert isinstance(response, CanonicalResponse)
        assert response.errors == ["autostartup(<lambda>): the unit is shutting down; not starting it up"]
        assert ran == []

    def test_the_signature_carries_through(self, unit):
        def expose(seconds: float = 1.0, count: int = 1):
            return None

        assert list(inspect.signature(unit._autostarted(expose)).parameters) == ["seconds", "count"]

    def test_an_async_handler_is_refused(self, unit):
        async def dance():
            return None

        with pytest.raises(TypeError, match="sync handler"):
            unit._autostarted(dance)


class TestStartupIsANoOpWhenUp:
    """`startup` on a unit already up and fit does nothing; up but unfit, it runs again."""

    @pytest.fixture
    def real(self, unit, monkeypatch):
        unit.start_activity = lambda flag: setattr(unit, "activities", unit.activities | flag)
        unit._idle_since = unit._active_since = None
        unit._was_shut_down = False
        unit.threads = []

        class _Thread:
            def __init__(self, **kwargs):
                unit.threads.append(kwargs["name"])

            def start(self):
                pass

        monkeypatch.setattr(unit_module, "Thread", _Thread)
        return unit

    def test_up_and_fit_does_nothing(self, real):
        real._set_opstate(OpState.STARTED_UP)

        Unit.startup(real)

        assert real.threads == []
        assert not real.is_active(UnitActivities.StartingUp)

    def test_up_but_unfit_runs_again(self, real):
        real._set_opstate(OpState.STARTED_UP)
        real.fit = False
        real.reasons = ["focuser: not connected"]

        Unit.startup(real)

        assert real.threads == ["unit-startup-thread"]

    def test_up_but_shutting_down_runs(self, real):
        """A startup during a shutdown is what reverses it; it must not be swallowed."""
        real._set_opstate(OpState.STARTED_UP)
        real.activities |= UnitActivities.ShuttingDown

        Unit.startup(real)

        assert real.threads == ["unit-startup-thread"]

    def test_not_up_runs(self, real):
        Unit.startup(real)

        assert real.threads == ["unit-startup-thread"]
