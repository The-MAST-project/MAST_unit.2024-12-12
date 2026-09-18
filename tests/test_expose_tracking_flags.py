"""`expose` can leave the mount alone, and its thread arguments stay positionally aligned.

`do_expose` has always started tracking before the series and stopped it in its `finally`,
unconditionally in both directions. That is right for a standalone exposure and wrong for
two cases the handover campaign needs: two frames that must share a field (the `finally`
is what defeated the 2026-09-02 band measurement), and a frame taken while guiding is
paused, where stopping the mount lets the field walk away before guiding resumes.

Both defaults stay `True`, so no existing caller changes behavior. These tests pin that,
and pin the coupling underneath it: `expose` passes its arguments into the thread
POSITIONALLY, so a parameter inserted anywhere but the end silently shifts every argument
after it -- the same failure mode `test_expose_params` was written for, where a caller got
"ok" and no image.

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


class FakeMount:
    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0

    def start_tracking(self) -> None:
        self.started += 1

    def stop_tracking(self) -> None:
        self.stopped += 1


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


def make_unit() -> Unit:
    """A Unit with only what `expose` and `do_expose` touch on this path."""
    u = object.__new__(Unit)
    u.mount = FakeMount()
    u.imager = FakeImager()
    u.activities = set()
    # `expose` clears the cancellation event before starting the run (#212).
    u._expose_cancelled = threading.Event()
    for name in ("start_activity", "end_activity", "is_active"):
        setattr(u, name, getattr(FakeActivities, name).__get__(u))
    return u


class TestDefaultsAreTheHistoricalBehavior:
    @pytest.mark.parametrize("name", ["expose", "do_expose"])
    def test_both_flags_default_to_true(self, name):
        parameters = inspect.signature(getattr(Unit, name)).parameters
        assert parameters["start_tracking"].default is True
        assert parameters["stop_tracking"].default is True

    def test_by_default_the_mount_is_started_and_stopped(self, monkeypatch):
        u = make_unit()
        monkeypatch.setattr(Unit, "_expose_repeatedly", lambda *a, **k: None)
        u.do_expose()
        assert (u.mount.started, u.mount.stopped) == (1, 1)


class TestTheFlagsAreHonored:
    def test_start_tracking_false_leaves_the_mount_alone_on_the_way_in(self, monkeypatch):
        u = make_unit()
        monkeypatch.setattr(Unit, "_expose_repeatedly", lambda *a, **k: None)
        u.do_expose(start_tracking=False)
        assert u.mount.started == 0

    def test_stop_tracking_false_leaves_the_pointing_standing(self, monkeypatch):
        u = make_unit()
        monkeypatch.setattr(Unit, "_expose_repeatedly", lambda *a, **k: None)
        u.do_expose(stop_tracking=False)
        assert u.mount.stopped == 0

    def test_stop_tracking_false_holds_even_when_the_exposure_raises(self, monkeypatch):
        """The `finally` must respect the flag, not only the happy path."""

        def boom(*a, **k):
            raise RuntimeError("camera fell over")

        u = make_unit()
        monkeypatch.setattr(Unit, "_expose_repeatedly", boom)
        response = u.do_expose(stop_tracking=False)
        assert response.failed
        assert u.mount.stopped == 0

    def test_declining_the_mount_does_not_strand_the_completion_flag(self, monkeypatch):
        """The interaction between this flag and #219's completion signal.

        `stop_tracking` is honored in the inner `finally`; `UnitActivities.Exposing`
        is cleared in the outer one. A resolution that folded the two together would
        leave a caller polling for a run that is visibly never over.
        """
        u = make_unit()
        u.activities.add(UnitActivities.Exposing)
        monkeypatch.setattr(Unit, "_expose_repeatedly", lambda *a, **k: None)
        u.do_expose(start_tracking=False, stop_tracking=False)
        assert u.mount.started == 0
        assert u.mount.stopped == 0
        assert not u.is_active(UnitActivities.Exposing)


class TestThreadArgumentsStayAligned:
    """The positional coupling between `expose` and `do_expose`.

    Adding a parameter anywhere but the end of both shifts every argument after it, and
    the endpoint has already answered `ok` by the time the thread fails.
    """

    def test_the_captured_arguments_bind_to_the_names_they_are_meant_for(self, monkeypatch):
        captured: dict[str, object] = {}

        class CapturingThread:
            def __init__(self, *, name: str, target, args) -> None:
                captured["target"] = target
                captured["args"] = args

            def start(self) -> None:
                pass

        monkeypatch.setattr(unit_module, "Thread", CapturingThread)
        u = make_unit()
        u.expose(subfolder="probe", exposure_seconds=2.0, start_tracking=False, stop_tracking=False)

        bound = inspect.signature(Unit.do_expose).bind(u, *captured["args"])
        assert bound.arguments["subfolder"] == "probe"
        assert bound.arguments["exposure_seconds"] == 2.0
        assert bound.arguments["start_tracking"] is False
        assert bound.arguments["stop_tracking"] is False

    def test_the_defaults_arrive_as_true(self, monkeypatch):
        captured: dict[str, object] = {}

        class CapturingThread:
            def __init__(self, *, name: str, target, args) -> None:
                captured["args"] = args

            def start(self) -> None:
                pass

        monkeypatch.setattr(unit_module, "Thread", CapturingThread)
        u = make_unit()
        u.expose()

        bound = inspect.signature(Unit.do_expose).bind(u, *captured["args"])
        assert bound.arguments["start_tracking"] is True
        assert bound.arguments["stop_tracking"] is True
