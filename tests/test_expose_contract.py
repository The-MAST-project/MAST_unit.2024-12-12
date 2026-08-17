"""What `expose` and `do_expose` promise, beyond the ROI rules in test_expose_roi.py.

These need the real Unit class (the endpoint is a method), so the module is imported --
which is safe: conftest installs the process guard at import time, before collection.
The Unit itself is never constructed; its methods are called on a stand-in, because
constructing one reaches for a mount, a camera, a power switch and MongoDB.
"""

from __future__ import annotations

import pytest

from common.canonical import CanonicalResponse_Ok
from unit import Unit


class FakeImager:
    def __init__(self):
        self.series = []
        self.ended = []

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

    def start_tracking(self):
        self.tracking = True

    def stop_tracking(self):
        self.tracking = False
        self.stop_calls += 1


class Stub:
    """Enough of a Unit to run do_expose's own body."""

    def __init__(self):
        self.imager = FakeImager()
        self.mount = FakeMount()
        self.raised = False

    def _expose_repeatedly(self, *args, **kwargs):
        if self.raised:
            raise RuntimeError("camera fell over mid-run")


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
        class ImagerPresent:
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
