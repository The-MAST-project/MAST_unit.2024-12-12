"""What `expose` and `do_expose` promise, beyond the ROI rules in test_expose_roi.py.

These need the real Unit class (the endpoint is a method), so the module is imported --
which is safe: conftest installs the process guard at import time, before collection.
The Unit itself is never constructed; its methods are called on a stand-in, because
constructing one reaches for a mount, a camera, a power switch and MongoDB.
"""

from __future__ import annotations

import pytest

# No platform guard: `conftest` stubs the absent hardware modules, so this runs on a dev
# machine as well as on a unit (#52). Windows keeps the real modules -- only absent ones
# are stubbed -- so nothing here can mask genuine Windows behaviour.
import unit as unit_module
from common.activities import UnitActivities
from common.canonical import CanonicalResponse_Ok
from common.endpoints import declaration_of
from unit import Unit


class FakeActivities:
    """The flag half of a Unit, over a set instead of an IntFlag bitmask."""

    def __init__(self):
        self.activities: set[UnitActivities] = set()
        self.details: dict[UnitActivities, list[str] | None] = {}

    def start_activity(self, activity, details=None, **kwargs):
        self.activities.add(activity)
        self.details[activity] = details

    def end_activity(self, activity, **kwargs):
        self.activities.discard(activity)

    def is_active(self, activity):
        return activity in self.activities


class FakeImager:
    def __init__(self):
        self.series = []
        self.ended = []
        self.camera_x_size = 8288
        self.camera_y_size = 5644

    def start_exposure_series(self, purpose=None):
        s = object()
        self.series.append(s)
        return s

    def end_exposure_series(self, series):
        self.ended.append(series)


class FakeMount:
    def __init__(self):
        self.tracking = False
        self.stop_calls = 0
        self.slews = []
        self.start_tracking_raises = False

    def goto_ra_dec_j2000(self, ra, dec):
        self.slews.append((ra, dec))

    def wait_until_settled(self, mode):
        pass

    def start_tracking(self):
        if self.start_tracking_raises:
            raise RuntimeError("the mount would not start tracking")
        self.tracking = True

    def stop_tracking(self):
        self.tracking = False
        self.stop_calls += 1


class Stub(FakeActivities):
    """Enough of a Unit to run expose's and do_expose's own bodies."""

    def __init__(self):
        super().__init__()
        self.imager = FakeImager()
        self.mount = FakeMount()
        self.raised = False

    def _expose_repeatedly(self, *args, **kwargs):
        if self.raised:
            raise RuntimeError("camera fell over mid-run")

    def do_expose(self, *args, **kwargs):
        """`expose` hands this to a Thread; the tests fake the Thread, so it never runs."""


class FakeThread:
    """Records the thread `expose` dispatches, and never runs its target."""

    started: list[FakeThread] = []

    def __init__(self, name=None, target=None, args=None):
        self.name = name
        self.target = target
        self.args = args

    def start(self):
        FakeThread.started.append(self)


@pytest.fixture
def dispatched(monkeypatch):
    """The threads `expose` started during one test."""
    monkeypatch.setattr(FakeThread, "started", [])
    monkeypatch.setattr(unit_module, "Thread", FakeThread)
    return FakeThread.started


class TestCleanupOnFailure:
    """do_expose runs inside `expose-thread`. Without try/finally an exception left the
    mount tracking indefinitely and the exposure series open, with nothing able to close
    them -- and the caller had already been told "ok"."""

    def test_a_successful_run_closes_the_series_and_stops_tracking(self):
        stub = Stub()
        response = Unit.do_expose(stub)  # type: ignore[arg-type]

        assert response == CanonicalResponse_Ok
        assert stub.imager.ended == stub.imager.series, "the series must be closed"
        assert not stub.mount.tracking
        assert stub.mount.stop_calls == 1

    def test_a_failing_run_still_closes_the_series_and_stops_tracking(self):
        stub = Stub()
        stub.raised = True

        response = Unit.do_expose(stub)  # type: ignore[arg-type]

        assert response.failed, "the failure must be reported, not swallowed"
        assert stub.imager.ended == stub.imager.series, "the series must be closed even on failure"
        assert not stub.mount.tracking, "the mount must not be left tracking forever"
        assert stub.mount.stop_calls == 1


class TestCoordinatePairing:
    """Half a coordinate pair used to be accepted and then quietly dropped: the slew
    requires BOTH to be floats, so only-RA meant no slew, no error, and a caller
    believing it had pointed somewhere it had not."""

    @pytest.mark.parametrize(
        ("ra", "dec", "expected"),
        [("12:30:45", None, "dec_j2000_degs"), (None, "-45:30:00", "ra_j2000_hours")],
        ids=["ra without dec", "dec without ra"],
    )
    def test_one_coordinate_without_the_other_is_refused(self, ra, dec, expected):
        class ImagerPresent(FakeActivities):
            imager = object()

        response = Unit.expose(ImagerPresent(), ra_j2000_hours=ra, dec_j2000_degs=dec)  # type: ignore[arg-type]

        assert response.failed
        assert expected in response.errors[0], "the error must name the one that is missing"
        assert "supply both" in response.errors[0]


class TestBinning:
    """Only the camera's binnings are accepted, and the ones that are must actually work.

    This class previously asserted the annotation *was*
    `ASI_294MM_SUPPORTED_BINNINGS_LITERAL` -- identity of an implementation detail rather
    than behaviour. It passed while `?binning=1` returned 422 for every request, because a
    Literal is exactly what breaks a query parameter: values arrive as strings and pydantic
    will not coerce `"1"` into `Literal[1, 2]`. The parameter was omittable but never
    settable, so bin 2 was unreachable over HTTP, and the test enforced the cause.

    Asserted through the parameter's own validation now, so it says what a caller can do.
    """

    @staticmethod
    def _validate(value):
        """Run `value` through the binning parameter exactly as a request would."""
        import typing

        from pydantic import TypeAdapter

        return TypeAdapter(typing.get_type_hints(Unit.expose)["binning"]).validate_python(value)

    @pytest.mark.parametrize("supplied", ["1", "2", 1, 2])
    def test_the_camera_s_binnings_are_accepted_as_strings_and_ints(self, supplied):
        """A query parameter arrives as a string; both forms must work."""
        assert self._validate(supplied) == int(supplied)

    @pytest.mark.parametrize("supplied", ["0", "3", "4", 3, "two", ""])
    def test_anything_else_is_refused(self, supplied):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            self._validate(supplied)

    def test_the_accepted_value_is_an_int_downstream(self):
        """`ImagerSettings.binning` is still the Literal, so whatever this yields has to
        satisfy it -- an IntEnum member does, a string would not."""
        value = self._validate("2")

        assert isinstance(value, int)
        assert value == 2


class TestTheExposingFlag:
    """`expose` answers Ok while the run is still going, so the run has to be watchable.

    The signal is `UnitActivities.Exposing`, declared as the endpoint's `completion`. It is
    raised by the endpoint and cleared by the thread, and the whole value of it is that the
    clearing happens on *every* path -- a flag that survives its run is a published promise
    that never resolves.
    """

    def test_expose_declares_the_flag_as_its_completion_signal(self):
        declaration = declaration_of(Unit.expose)

        assert declaration is not None
        assert declaration.completion is UnitActivities.Exposing

    def test_a_successful_run_ends_the_flag(self):
        stub = Stub()
        stub.start_activity(UnitActivities.Exposing)

        Unit.do_expose(stub)  # type: ignore[arg-type]

        assert not stub.is_active(UnitActivities.Exposing)

    def test_a_failing_run_ends_the_flag(self):
        stub = Stub()
        stub.raised = True
        stub.start_activity(UnitActivities.Exposing)

        Unit.do_expose(stub)  # type: ignore[arg-type]

        assert not stub.is_active(UnitActivities.Exposing)

    def test_a_failure_before_the_series_opens_ends_the_flag(self):
        """`start_tracking` and `start_exposure_series` run outside the inner try, so the
        flag has to be cleared further out than they can raise."""
        stub = Stub()
        stub.mount.start_tracking_raises = True
        stub.start_activity(UnitActivities.Exposing)

        with pytest.raises(RuntimeError):
            Unit.do_expose(stub)  # type: ignore[arg-type]

        assert not stub.is_active(UnitActivities.Exposing)

    def test_the_flag_is_raised_before_the_endpoint_answers(self, dispatched):
        """Thread.start() returns before the target body runs, so raising the flag in
        `do_expose` would leave a window where the caller holds Ok and the unit reads idle."""
        stub = Stub()

        response = Unit.expose(stub)  # type: ignore[arg-type]

        assert response == CanonicalResponse_Ok
        assert len(dispatched) == 1
        assert stub.is_active(UnitActivities.Exposing), "the flag must be up by the time Ok is returned"


class TestOneRunAtATime:
    """One flag bit cannot represent two runs: the first to finish would clear it while the
    second was still exposing, publishing a completion that had not happened."""

    def test_a_second_run_is_refused(self, dispatched):
        stub = Stub()
        stub.start_activity(UnitActivities.Exposing)

        response = Unit.expose(stub)  # type: ignore[arg-type]

        assert response.failed
        assert "already in flight" in response.errors[0]

    def test_the_refusal_moves_no_hardware(self, dispatched):
        """Refused ahead of the slew -- a second call must not point the telescope
        somewhere else while a run is in progress."""
        stub = Stub()
        stub.start_activity(UnitActivities.Exposing)

        Unit.expose(stub, ra_j2000_hours=12.5, dec_j2000_degs=-45.5)  # type: ignore[arg-type]

        assert stub.mount.slews == [], "the mount must not be commanded"
        assert dispatched == [], "no thread may be started"


class TestValidationPrecedesTheSlew:
    """The ROI and offset parameters are pure, and rejecting one after the mount has moved
    leaves the telescope somewhere the caller never asked for and got an error about."""

    def test_a_bad_offset_list_is_refused_without_slewing(self, dispatched):
        stub = Stub()

        response = Unit.expose(stub, ra_j2000_hours=12.5, dec_j2000_degs=-45.5, ra_offsets="not-a-number")  # type: ignore[arg-type]

        assert response.failed
        assert stub.mount.slews == [], "the mount must not be commanded for a request that is refused"
        assert not stub.is_active(UnitActivities.Exposing), "a refused request raises no flag"
        assert dispatched == []

    def test_an_incomplete_roi_is_refused_without_slewing(self, dispatched):
        stub = Stub()

        response = Unit.expose(stub, ra_j2000_hours=12.5, dec_j2000_degs=-45.5, width=1500)  # type: ignore[arg-type]

        assert response.failed
        assert "incomplete ROI" in response.errors[0]
        assert stub.mount.slews == []
        assert not stub.is_active(UnitActivities.Exposing)
        assert dispatched == []

    def test_a_valid_request_slews_and_dispatches(self, dispatched):
        stub = Stub()

        response = Unit.expose(stub, ra_j2000_hours=12.5, dec_j2000_degs=-45.5)  # type: ignore[arg-type]

        assert response == CanonicalResponse_Ok
        assert stub.mount.slews == [(12.5, -45.5)]
        assert len(dispatched) == 1
