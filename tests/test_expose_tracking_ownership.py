"""`expose` leaves the mount's tracking as it found it, and its thread arguments stay aligned.

Tracking the caller established is the caller's: `expose` starts tracking before the run
and stops it after only when the mount was not tracking. The state is read in the
endpoint BEFORE the slew, because PWI4 engages tracking at the end of an equatorial goto;
read after it, every slewing expose would find the mount tracking and never stop it.

The open-loop tracking grid's `goto_alt_az` -> `expose` sequence depends on the start:
`goto_alt_az` stops tracking, so `expose` finds it off and starts it from exactly the
commanded alt/az.

`expose` passes its arguments into the thread POSITIONALLY, so a parameter inserted
anywhere but the end silently shifts every argument after it -- the same failure mode
`test_expose_params` was written for, where a caller got "ok" and no image.

Runs in the unit venv (Windows): the import chain is Windows-only today (`stage.py` uses
pyximc names at module level). Skips cleanly elsewhere.
"""

from __future__ import annotations

import inspect
import threading

import pytest

try:
    import unit as unit_module
    from unit import Unit
except (ImportError, NameError) as ex:  # NameError: stage.py off-Windows
    pytest.skip(f"unit import chain unavailable here ({ex!r})", allow_module_level=True)

from common.activities import UnitActivities

SENSOR_X, SENSOR_Y = 8288, 5644
TARGET_RA, TARGET_DEC = "10:00:00", "20:00:00"


class FakeMount:
    """Tracking as PWI4 keeps it: a goto engages it, start/stop set it."""

    def __init__(self, is_tracking: bool) -> None:
        self.is_tracking = is_tracking
        self.started = 0
        self.stopped = 0

    def start_tracking(self) -> None:
        self.started += 1
        self.is_tracking = True

    def stop_tracking(self) -> None:
        self.stopped += 1
        self.is_tracking = False

    def goto_ra_dec_j2000(self, ra: float, dec: float) -> None:
        self.is_tracking = True

    def wait_until_settled(self, mode) -> None:
        pass


class FakeImager:
    camera_x_size = SENSOR_X
    camera_y_size = SENSOR_Y

    def start_exposure_series(self, purpose: str) -> object:
        return object()

    def end_exposure_series(self, series: object) -> None:
        pass


class FakeActivities:
    """The flag half of a Unit, over a set instead of an IntFlag bitmask.

    Mirrors `test_expose_contract`'s fake rather than the real `Activities`, which
    wants a lock, a timings table and a notifier to answer three questions.
    """

    def start_activity(self, activity, **kwargs) -> None:
        self.activities.add(activity)

    def end_activity(self, activity, **kwargs) -> None:
        self.activities.discard(activity)

    def is_active(self, activity) -> bool:
        return activity in self.activities


def make_unit(is_tracking: bool) -> Unit:
    """A Unit with only what `expose` and `do_expose` touch on this path."""
    u = object.__new__(Unit)
    u.mount = FakeMount(is_tracking)
    u.imager = FakeImager()
    u.activities = set()
    # `expose` clears the cancellation event before starting the run (#212).
    u._expose_cancelled = threading.Event()
    for name in ("start_activity", "end_activity", "is_active"):
        setattr(u, name, getattr(FakeActivities, name).__get__(u))
    return u


def succeed(*a, **k) -> None:
    pass


def boom(*a, **k) -> None:
    raise RuntimeError("camera fell over")


def run_inline(monkeypatch) -> None:
    """Run the expose thread's target synchronously, so `expose` is tested end to end."""

    class InlineThread:
        def __init__(self, *, name: str, target, args) -> None:
            self.target, self.args = target, args

        def start(self) -> None:
            self.target(*self.args)

    monkeypatch.setattr(unit_module, "Thread", InlineThread)


class TestTheFlagsAreGone:
    @pytest.mark.parametrize("name", ["expose", "do_expose"])
    @pytest.mark.parametrize("flag", ["start_tracking", "stop_tracking"])
    def test_no_tracking_flag_in_the_signature(self, name, flag):
        assert flag not in inspect.signature(getattr(Unit, name)).parameters


class TestTrackingTheCallerEstablished:
    @pytest.mark.parametrize("repeatedly", [succeed, boom], ids=["succeeds", "raises"])
    def test_is_neither_recommanded_nor_stopped(self, monkeypatch, repeatedly):
        u = make_unit(is_tracking=True)
        monkeypatch.setattr(Unit, "_expose_repeatedly", repeatedly)
        run_inline(monkeypatch)
        u.expose()
        assert (u.mount.started, u.mount.stopped) == (0, 0)
        assert u.mount.is_tracking

    def test_survives_a_slew(self, monkeypatch):
        u = make_unit(is_tracking=True)
        monkeypatch.setattr(Unit, "_expose_repeatedly", succeed)
        run_inline(monkeypatch)
        u.expose(ra_j2000_hours=TARGET_RA, dec_j2000_degs=TARGET_DEC)
        assert u.mount.stopped == 0
        assert u.mount.is_tracking

    def test_leaving_the_mount_alone_does_not_strand_the_completion_flag(self, monkeypatch):
        """Tracking is handled in the inner `finally`, `UnitActivities.Exposing` cleared in
        the outer one. Folding the two together would leave a caller polling for a run that
        is visibly never over."""
        u = make_unit(is_tracking=True)
        monkeypatch.setattr(Unit, "_expose_repeatedly", succeed)
        run_inline(monkeypatch)
        u.expose()
        assert not u.is_active(UnitActivities.Exposing)


class TestTrackingExposeEstablished:
    @pytest.mark.parametrize("repeatedly", [succeed, boom], ids=["succeeds", "raises"])
    def test_is_started_and_stopped(self, monkeypatch, repeatedly):
        u = make_unit(is_tracking=False)
        monkeypatch.setattr(Unit, "_expose_repeatedly", repeatedly)
        run_inline(monkeypatch)
        u.expose()
        assert (u.mount.started, u.mount.stopped) == (1, 1)
        assert not u.mount.is_tracking

    def test_is_stopped_even_though_the_slew_engaged_it(self, monkeypatch):
        u = make_unit(is_tracking=False)
        monkeypatch.setattr(Unit, "_expose_repeatedly", succeed)
        run_inline(monkeypatch)
        u.expose(ra_j2000_hours=TARGET_RA, dec_j2000_degs=TARGET_DEC)
        assert u.mount.stopped == 1
        assert not u.mount.is_tracking


class TestThreadArgumentsStayAligned:
    """The positional coupling between `expose` and `do_expose`.

    Adding a parameter anywhere but the end of both shifts every argument after it, and
    the endpoint has already answered `ok` by the time the thread fails.
    """

    @pytest.mark.parametrize("is_tracking", [True, False])
    def test_the_captured_arguments_bind_to_the_names_they_are_meant_for(self, monkeypatch, is_tracking):
        captured: dict[str, object] = {}

        class CapturingThread:
            def __init__(self, *, name: str, target, args) -> None:
                captured["args"] = args

            def start(self) -> None:
                pass

        monkeypatch.setattr(unit_module, "Thread", CapturingThread)
        u = make_unit(is_tracking=is_tracking)
        u.expose(subfolder="probe", exposure_seconds=2.0)

        bound = inspect.signature(Unit.do_expose).bind(u, *captured["args"])
        assert bound.arguments["subfolder"] == "probe"
        assert bound.arguments["exposure_seconds"] == 2.0
        assert bound.arguments["mount_was_tracking"] is is_tracking
