"""The unit's dance: alt/az figures, the mount's walk through one, and the unit endpoint that starts it.

No mount, no unit: the figures are data, the mount's walk runs on a fake mount, and the endpoint on a fake unit.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time

import pytest

import mount as mount_module
import unit as unit_module
from common.activities import CoverActivities, MountActivities, UnitActivities
from common.canonical import CanonicalResponse, CanonicalResponse_Ok
from common.endpoints import Tier, declaration_of
from common.models.statuses import CoversState
from dance import CENTRE_ALT, CENTRE_AZ, CIRCLE_RADIUS, DEFAULT_STYLE, STYLES, dance_path
from mount import Mount
from unit import Unit

# ------------------------------------------------------------------------------------------------ the figures


@pytest.mark.parametrize("style", sorted(STYLES))
def test_every_step_is_inside_the_mounts_limits(style):
    path = dance_path(style)
    assert path
    for alt, az in path:
        assert Mount.MIN_ALTITUDE_DEGREES + 20.0 <= alt <= Mount.MAX_ALTITUDE_DEGREES
        assert 0.0 <= az < 360.0


def _separation(a, b):
    """Angular distance in degrees between two (alt, az) points."""
    import math

    alt1, az1, alt2, az2 = (math.radians(v) for v in (*a, *b))
    c = math.sin(alt1) * math.sin(alt2) + math.cos(alt1) * math.cos(alt2) * math.cos(az1 - az2)
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


def test_the_foxtrot_is_a_circle_on_the_sky():
    path = dance_path("foxtrot")
    centre = (CENTRE_ALT, CENTRE_AZ)
    assert len(path) == 13
    assert path[0] == path[-1]  # it ends where it began
    for step in path:
        assert _separation(step, centre) == pytest.approx(CIRCLE_RADIUS, abs=0.01)
    # all the way round: it goes above and below the centre, east and west of it
    alts = [a for a, _ in path]
    azs = [z for _, z in path]
    assert max(alts) > CENTRE_ALT + 15 and min(alts) < CENTRE_ALT - 15
    assert min(azs) < CENTRE_AZ - 20 and max(azs) > CENTRE_AZ + 20
    # but never round the full azimuth circle: no cable wrap
    assert max(azs) - min(azs) < 120.0


def test_the_waltz_is_a_triangle_inscribed_in_the_same_circle():
    path = dance_path("waltz")
    assert len(path) == 6 and path[:3] == path[3:]
    for step in path:
        assert _separation(step, (CENTRE_ALT, CENTRE_AZ)) == pytest.approx(CIRCLE_RADIUS, abs=0.01)


def test_alto_is_a_circle_round_the_zenith_taken_once_round():
    path = dance_path("alto")
    assert len(path) == 13 and path[0] == path[-1]
    for alt, _ in path:
        assert _separation((alt, 0.0), (90.0, 0.0)) == pytest.approx(CIRCLE_RADIUS, abs=0.01)
    azimuths = [az for _, az in path]
    steps = [(b - a) % 360.0 for a, b in zip(azimuths, azimuths[1:], strict=False)]
    assert steps == [30.0] * 12


def test_a_style_is_not_case_sensitive_and_unknown_ones_are_listed():
    assert dance_path(" Foxtrot ") == dance_path(DEFAULT_STYLE)
    with pytest.raises(ValueError, match="alto, foxtrot, waltz"):
        dance_path("macarena")


# ------------------------------------------------------------------------------------------------ the mount's walk


class FakeMount:
    """Slews for `slew_polls` reads of `is_slewing` after each goto, and homes for `home_polls`
    reads of `is_active(FindingHome)` after each `find_home` (forever, with `never_homes`)."""

    def __init__(self, slew_polls=2, refuse_at=None, never_stops=False, home_polls=0, never_homes=False, connected=True):
        self.events: list = []
        self.slew_polls = slew_polls
        self.refuse_at = refuse_at
        self.never_stops = never_stops
        self.home_polls = home_polls
        self.never_homes = never_homes
        self.connected = connected
        self._left = 0
        self._homing_left: int | None = None
        self.gotos = []

    def find_home(self):
        self.events.append(("home",))
        self._homing_left = self.home_polls

    def is_active(self, activity):
        assert activity == MountActivities.FindingHome, "the dance only asks about homing"
        if self._homing_left is None:
            return False
        if self.never_homes:
            return True
        if self._homing_left == 0:
            self._homing_left = None
            self.events.append(("homed",))
            return False
        self._homing_left -= 1
        return True

    def end_activity(self, activity):
        self.events.append(("end", activity))
        self._homing_left = None

    def _home_for_dance(self, *args):
        # The real one, so the dance's homing is what these tests exercise.
        return Mount._home_for_dance(self, *args)  # type: ignore[arg-type]

    def goto_alt_az(self, alt, az):
        self.gotos.append((alt, az))
        self.events.append(("goto", alt, az))
        if self.refuse_at == len(self.gotos):
            return CanonicalResponse(errors=["PWI4 said no"])
        self._left = self.slew_polls
        return CanonicalResponse_Ok

    @property
    def is_slewing(self):
        if self.never_stops:
            return True
        slewing = self._left > 0
        self._left = max(0, self._left - 1)
        self.events.append(("slewing", slewing))
        return slewing

    def axis_positions(self):
        return (float(len(self.gotos)), 0.0)

    def axes_report(self):
        return 'axis1 0.1894 deg (target 0.0500, off 501.7")'

    def stop_slew(self):
        self.events.append(("stop",))


def _walk(mount, path, keep_going=lambda: True, **kw):
    kw.setdefault("rest_seconds", 0.0)
    kw.setdefault("poll_seconds", 0.0)
    kw.setdefault("start_grace_seconds", 0.2)
    return Mount.dance_alt_az(mount, path, keep_going, **kw)


def test_the_mount_visits_every_step_in_order():
    mount = FakeMount()
    path = dance_path("waltz")
    assert _walk(mount, path) is None
    assert mount.gotos == path


def test_each_slew_is_waited_out_before_the_next_step():
    mount = FakeMount(slew_polls=2)
    _walk(mount, [(60.0, 100.0), (60.0, 140.0)])
    first_goto, second_goto = (i for i, e in enumerate(mount.events) if e[0] == "goto")
    assert ("slewing", False) in mount.events[first_goto:second_goto]


def test_a_stop_is_heard_between_steps():
    mount = FakeMount()
    calls = {"n": 0}

    def keep_going():
        calls["n"] += 1
        return len(mount.gotos) < 2  # after the second goto, stop

    assert _walk(mount, dance_path("foxtrot"), keep_going) is None
    assert len(mount.gotos) == 2


def test_a_stop_is_heard_during_a_slew_that_will_not_end():
    mount = FakeMount(never_stops=True)
    seen = {"n": 0}

    def keep_going():
        seen["n"] += 1
        return seen["n"] < 6

    assert _walk(mount, dance_path("foxtrot"), keep_going, slew_timeout_seconds=60.0) is None
    assert len(mount.gotos) == 1


def test_a_refused_goto_ends_the_dance_with_the_reason():
    mount = FakeMount(refuse_at=2)
    error = _walk(mount, dance_path("foxtrot"))
    assert error is not None and "step 2" in error and "PWI4 said no" in error
    assert len(mount.gotos) == 2


def test_a_slew_that_never_ends_times_out():
    mount = FakeMount(never_stops=True)
    error = _walk(mount, dance_path("foxtrot"), slew_timeout_seconds=0.05)
    assert error is not None and "still slewing" in error


def test_a_timed_out_slew_is_stopped_and_says_where_the_axes_are():
    """mast07, 2026-11-23: a stalled step left PWI4 holding the goto, `Slewing` raised."""
    mount = FakeMount(never_stops=True)
    error = _walk(mount, dance_path("foxtrot"), slew_timeout_seconds=0.05)
    assert ("stop",) in mount.events
    assert "mount stopped" in error and 'off 501.7"' in error


def test_a_slew_that_is_stopped_by_keep_going_is_not_stopped_by_the_dance():
    """`stop_dancing` and `abort` stop the mount themselves; the walk only returns."""
    mount = FakeMount(never_stops=True)
    seen = {"n": 0}

    def keep_going():
        seen["n"] += 1
        return seen["n"] < 6

    _walk(mount, dance_path("foxtrot"), keep_going, slew_timeout_seconds=60.0)
    assert ("stop",) not in mount.events


def test_each_step_logs_where_the_axes_went(caplog):
    caplog.set_level(logging.INFO)
    _walk(FakeMount(), [(60.0, 100.0)])
    assert any("step 1 done: axis0 0.0000 -> 1.0000" in r.getMessage() for r in caplog.records)


# ------------------------------------------------------------------------------------------------ homing and pace


def _kinds(mount):
    return [e[0] for e in mount.events if e[0] in ("home", "homed", "goto")]


def test_a_dance_is_homed_first_and_last():
    mount = FakeMount(home_polls=2)
    assert _walk(mount, dance_path("waltz")) is None
    assert _kinds(mount) == ["home", "homed", *["goto"] * len(dance_path("waltz")), "home", "homed"]


def test_the_first_step_waits_for_the_homing_to_end():
    """`FindingHome`, not `is_slewing`: is_slewing stays False throughout a homing."""
    mount = FakeMount(home_polls=5)
    _walk(mount, [(60.0, 100.0)])
    assert _kinds(mount)[:3] == ["home", "homed", "goto"]


def test_a_stopped_dance_is_not_homed_at_the_end():
    """A stop means stop where you are: `stop_dancing` and `abort` stop the mount themselves."""
    mount = FakeMount()
    _walk(mount, dance_path("foxtrot"), lambda: len(mount.gotos) < 2)
    assert _kinds(mount).count("home") == 1


def test_a_failed_dance_is_not_homed_at_the_end():
    mount = FakeMount(refuse_at=2)
    assert _walk(mount, dance_path("foxtrot")) is not None
    assert _kinds(mount).count("home") == 1


def test_a_stop_during_the_first_homing_dances_nothing():
    mount = FakeMount(never_homes=True)
    seen = {"n": 0}

    def keep_going():
        seen["n"] += 1
        return seen["n"] < 4

    assert _walk(mount, dance_path("foxtrot"), keep_going) is None
    assert mount.gotos == [] and ("stop",) not in mount.events


def test_a_homing_that_never_ends_is_stopped_and_dances_nothing():
    mount = FakeMount(never_homes=True)
    error = _walk(mount, dance_path("foxtrot"), home_timeout_seconds=0.05)
    assert error is not None and "homing before the dance not done" in error and 'off 501.7"' in error
    assert ("stop",) in mount.events and ("end", MountActivities.FindingHome) in mount.events
    assert mount.gotos == []


def test_a_disconnected_mount_is_not_danced():
    mount = FakeMount(connected=False)
    error = _walk(mount, dance_path("foxtrot"))
    assert error is not None and "not connected" in error
    assert mount.events == []


def test_the_pace_is_half_a_second():
    """mast02, 2026-11-25: pointing 0.5 s after a slew was as good as after 3 s (2-3")."""
    import inspect

    defaults = {k: p.default for k, p in inspect.signature(Mount.dance_alt_az).parameters.items()}
    assert mount_module.DANCE_REST_SECONDS == 0.5 and defaults["rest_seconds"] == 0.5
    assert defaults["poll_seconds"] == 0.5


# ------------------------------------------------------------------------------------------------ the unit endpoint


class FakeUnit:
    """The slice of `Unit` the dance endpoints use."""

    def __init__(self, mount=None, busy=(), covers=None):
        self.flags = set(busy)
        self.errors: list[str] = []
        self.mount = mount if mount is not None else FakeDancingMount()
        self.covers = covers if covers is not None else FakeCovers()
        self.details = None

    def is_active(self, flag):
        return flag in self.flags

    def start_activity(self, flag, **kw):
        self.flags.add(flag)
        self.details = kw.get("details")

    def end_activity(self, flag, **kw):
        self.flags.discard(flag)

    @property
    def required_mount(self):
        return self.mount

    @property
    def required_covers(self):
        return self.covers

    do_dance = Unit.do_dance


class FakeCovers:
    """Covers that reach the end state `travel_reads` reads of `state` after a command -- or, in
    the `stop_short` direction ("open"/"close"), stop PartlyOpen with the motion activity ended."""

    def __init__(self, connected=True, state=CoversState.Closed, travel_reads=3, stop_short=None):
        self.connected = connected
        self._state = state
        self.travel_reads = travel_reads
        self.stop_short = stop_short
        self.events: list[str] = []
        self.activities: set = set()
        self._moving = None  # (direction, target, motion, reads left)

    def _command(self, direction, target, motion):
        self.events.append(direction)
        self.activities.add(motion)
        self._moving = (direction, target, motion, self.travel_reads)
        return CanonicalResponse_Ok

    def open(self):
        return self._command("open", CoversState.Open, CoverActivities.Opening)

    def close(self):
        return self._command("close", CoversState.Closed, CoverActivities.Closing)

    @property
    def state(self):
        if self._moving is None:
            return self._state
        direction, target, motion, left = self._moving
        if self.stop_short == direction:
            self._moving, self._state = None, CoversState.PartlyOpen
            self.activities.discard(motion)
        elif left == 0:
            self._moving, self._state = None, target
            self.activities.discard(motion)
        else:
            self._moving = (direction, target, motion, left - 1)
            return CoversState.Moving
        return self._state

    def is_active(self, activity):
        return activity in self.activities

    def end_activity(self, activity):
        self.activities.discard(activity)


class FakeDancingMount:
    """Dances until released (or stopped), running the `AlongsideHoming`s as the real one does."""

    def __init__(self, error=None, raises=None, steps_before_stop=None):
        self.error = error
        self.raises = raises
        self.aborted = False
        self.paths: list = []
        self.alongside: list = []
        self.release = threading.Event()

    @staticmethod
    def _run(alongside, keep_going):
        if alongside is None:
            return None
        error = alongside.start()
        if error:
            return error
        while keep_going():
            done, error = alongside.progress()
            if error or done:
                return error
            time.sleep(0.005)
        return None

    def dance_alt_az(self, path, keep_going, alongside_first_homing=None, alongside_last_homing=None):
        self.paths.append(path)
        self.alongside.append((alongside_first_homing, alongside_last_homing))
        error = self._run(alongside_first_homing, keep_going)
        if error:
            return error
        while not self.release.is_set() and keep_going():
            time.sleep(0.005)
        if self.raises:
            raise self.raises
        if self.error or not keep_going():
            return self.error
        return self._run(alongside_last_homing, keep_going)

    def abort(self):
        self.aborted = True


def _start(unit, style="foxtrot", covers=False):
    return Unit.endpoint_start_dancing(unit, style=style, covers=covers)


def _wait_until(predicate, seconds=3.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def test_starting_returns_at_once_with_dancing_raised_then_lowers_it():
    unit = FakeUnit()
    assert _start(unit) is CanonicalResponse_Ok
    assert UnitActivities.Dancing in unit.flags
    assert unit.details == ["foxtrot"]
    assert _wait_until(lambda: unit.mount.paths)
    assert unit.mount.paths[0] == dance_path("foxtrot")
    unit.mount.release.set()
    assert _wait_until(lambda: UnitActivities.Dancing not in unit.flags)
    assert unit.errors == []


def test_the_style_chooses_the_figure():
    unit = FakeUnit()
    _start(unit, "waltz")
    assert _wait_until(lambda: unit.mount.paths)
    assert unit.mount.paths[0] == dance_path("waltz")
    unit.mount.release.set()


def test_an_unknown_style_is_refused_and_nothing_starts():
    unit = FakeUnit()
    response = _start(unit, "macarena")
    assert response.failed and "alto, foxtrot, waltz" in response.errors[0]
    assert UnitActivities.Dancing not in unit.flags
    assert unit.mount.paths == []


def test_it_is_refused_while_dancing_or_busy():
    dancing = FakeUnit(busy=(UnitActivities.Dancing,))
    assert "already dancing" in _start(dancing).errors[0]
    busy = FakeUnit(busy=(UnitActivities.Exposing, UnitActivities.Guiding))
    response = _start(busy)
    assert response.failed and "Exposing" in response.errors[0] and "Guiding" in response.errors[0]
    assert busy.mount.paths == []
    aborting = FakeUnit(busy=(UnitActivities.Aborting,))
    assert "Aborting" in _start(aborting).errors[0]


def test_a_missing_mount_is_refused_with_the_reason():
    class NoMount(FakeUnit):
        @property
        def required_mount(self):
            raise RuntimeError("the mount did not initialize")

    response = _start(NoMount())
    assert response.failed and "mount did not initialize" in response.errors[0]


def test_a_failed_dance_is_recorded_and_the_flag_is_lowered():
    unit = FakeUnit(mount=FakeDancingMount(error="step 3 refused"))
    unit.mount.release.set()
    _start(unit)
    assert _wait_until(lambda: UnitActivities.Dancing not in unit.flags)
    assert unit.errors == ["step 3 refused"]


def test_a_dance_that_raises_still_lowers_the_flag():
    unit = FakeUnit(mount=FakeDancingMount(raises=RuntimeError("pwi4 went away")))
    unit.mount.release.set()
    _start(unit)
    assert _wait_until(lambda: UnitActivities.Dancing not in unit.flags)
    assert "pwi4 went away" in unit.errors[0]


def test_stopping_clears_the_flag_and_stops_the_mount():
    unit = FakeUnit()
    _start(unit)
    assert _wait_until(lambda: unit.mount.paths)
    assert asyncio.run(Unit.endpoint_stop_dancing(unit)) is CanonicalResponse_Ok
    assert UnitActivities.Dancing not in unit.flags
    assert unit.mount.aborted
    unit.mount.release.set()


def test_stopping_when_not_dancing_does_not_touch_the_mount():
    unit = FakeUnit()
    asyncio.run(Unit.endpoint_stop_dancing(unit))
    assert not unit.mount.aborted


def test_the_route_does_not_autostart_and_completes_on_the_flag():
    declaration = declaration_of(Unit.endpoint_start_dancing)
    assert declaration is not None
    assert declaration.autostartup is False
    assert declaration.completion is UnitActivities.Dancing
    assert declaration.tier is Tier.OPERATION
    assert unit_module._ABORTABLE_WORK.count(UnitActivities.Dancing) == 1


# ------------------------------------------------------------------------------------------------ the covers


class NoCovers(FakeUnit):
    @property
    def required_covers(self):
        raise RuntimeError("the covers did not initialize")


def test_without_covers_the_covers_are_not_touched():
    unit = FakeUnit()
    unit.mount.release.set()
    _start(unit)
    assert _wait_until(lambda: UnitActivities.Dancing not in unit.flags)
    assert unit.covers.events == []
    assert unit.mount.alongside == [(None, None)]


def test_with_covers_they_open_with_the_first_homing_and_close_with_the_last():
    unit = FakeUnit()
    assert _start(unit, covers=True) is CanonicalResponse_Ok
    assert unit.details == ["foxtrot", "covers"]
    assert _wait_until(lambda: unit.covers._state == CoversState.Open), "opened before the steps"
    assert unit.covers.events == ["open"]
    unit.mount.release.set()
    assert _wait_until(lambda: UnitActivities.Dancing not in unit.flags)
    assert unit.covers.events == ["open", "close"]
    assert unit.covers._state == CoversState.Closed, "Dancing ends once the covers are closed"
    assert unit.errors == []


def test_a_stopped_dance_closes_the_covers():
    unit = FakeUnit()
    _start(unit, covers=True)
    assert _wait_until(lambda: unit.covers._state == CoversState.Open)
    asyncio.run(Unit.endpoint_stop_dancing(unit))
    assert _wait_until(lambda: unit.covers.events == ["open", "close"])


def test_a_failed_dance_closes_the_covers():
    unit = FakeUnit(mount=FakeDancingMount(error="step 3 refused"))
    unit.mount.release.set()
    _start(unit, covers=True)
    assert _wait_until(lambda: UnitActivities.Dancing not in unit.flags)
    assert unit.errors == ["step 3 refused"]
    assert unit.covers.events == ["open", "close"]


def test_covers_that_stop_short_fail_the_dance_and_are_closed():
    unit = FakeUnit(covers=FakeCovers(stop_short="open"))
    unit.mount.release.set()
    _start(unit, covers=True)
    assert _wait_until(lambda: UnitActivities.Dancing not in unit.flags)
    assert unit.errors and "stopped PartlyOpen, not Open" in unit.errors[0]
    assert unit.covers.events == ["open", "close"]


def test_disconnected_or_missing_covers_are_refused_and_nothing_starts():
    unit = FakeUnit(covers=FakeCovers(connected=False))
    response = _start(unit, covers=True)
    assert response.failed and "covers not connected" in response.errors[0]
    assert UnitActivities.Dancing not in unit.flags and unit.mount.paths == []

    response = _start(NoCovers(), covers=True)
    assert response.failed and "covers did not initialize" in response.errors[0]


def test_missing_covers_do_not_matter_without_covers():
    unit = NoCovers()
    assert _start(unit) is CanonicalResponse_Ok
    unit.mount.release.set()
    assert _wait_until(lambda: UnitActivities.Dancing not in unit.flags)


# ------------------------------------------------------------------------------------------------ work alongside a homing


def _alongside(done_after=0, start_error=None, fails=None, what="opening the covers"):
    calls = {"start": 0, "polls": 0}

    def start():
        calls["start"] += 1
        return start_error

    def progress():
        calls["polls"] += 1
        if fails:
            return False, fails
        return calls["polls"] > done_after, None

    return mount_module.AlongsideHoming(what, start=start, progress=progress), calls


def test_the_first_step_waits_for_the_work_alongside_the_homing():
    mount = FakeMount()
    alongside, calls = _alongside(done_after=4)
    _walk(mount, [(60.0, 100.0)], alongside_first_homing=alongside)
    assert calls["start"] == 1 and calls["polls"] == 5, "polled until done, then the step"
    assert _kinds(mount)[:3] == ["home", "homed", "goto"]


def test_work_alongside_that_cannot_start_ends_the_dance():
    mount = FakeMount()
    alongside, _ = _alongside(start_error="covers: not connected")
    error = _walk(mount, dance_path("foxtrot"), alongside_first_homing=alongside)
    assert error is not None and "opening the covers before the dance: covers: not connected" in error
    assert mount.gotos == []


def test_work_alongside_that_fails_ends_the_dance():
    mount = FakeMount()
    alongside, _ = _alongside(fails="the covers stopped PartlyOpen, not Open")
    error = _walk(mount, dance_path("foxtrot"), alongside_first_homing=alongside)
    assert error is not None and "stopped PartlyOpen" in error and mount.gotos == []


def test_work_alongside_that_never_finishes_times_out_without_stopping_a_homed_mount():
    mount = FakeMount()
    alongside, _ = _alongside(done_after=10**9)
    error = _walk(mount, dance_path("foxtrot"), alongside_first_homing=alongside, home_timeout_seconds=0.05)
    assert error is not None and "opening the covers before the dance not done" in error
    assert ("stop",) not in mount.events, "the mount had homed: nothing of it to stop"


def test_the_last_homing_runs_its_work_alongside():
    mount = FakeMount()
    first, first_calls = _alongside()
    last, last_calls = _alongside(what="closing the covers")
    assert _walk(mount, dance_path("waltz"), alongside_first_homing=first, alongside_last_homing=last) is None
    assert first_calls["start"] == 1 and last_calls["start"] == 1
