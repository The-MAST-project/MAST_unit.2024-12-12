"""What `Mount.goto_alt_az` does around the PWI4 call.

The mount is never constructed -- that reaches for ASCOM, a power switch and PWI4 -- so
the method is called on a stand-in carrying only what it touches.
"""

from __future__ import annotations

import pytest

pytest.importorskip("win32com", reason="mount.py is Windows-only")

from common.activities import MountActivities
from mount import Mount, target_as_text


class FakePw:
    def __init__(self, raises=False):
        self.calls: list[tuple] = []
        self.raises = raises

    def mount_goto_alt_az(self, alt_degs, az_degs):
        if self.raises:
            raise RuntimeError("PWI4 refused the slew")
        self.calls.append((alt_degs, az_degs))

    def mount_goto_ra_dec_j2000(self, ra, dec):
        if self.raises:
            raise RuntimeError("PWI4 refused the slew")
        self.calls.append((ra, dec))

    def mount_goto_ra_dec_apparent(self, ra, dec):
        if self.raises:
            raise RuntimeError("PWI4 refused the slew")
        self.calls.append((ra, dec))


class Stub:
    """Enough of a Mount to run goto_alt_az's own body."""

    def __init__(self, connected=True, raises=False):
        self.connected = connected
        self.pw = FakePw(raises)
        self.target = None
        self.activities = MountActivities(0)
        self.activity_details: dict = {}
        self.stopped_tracking = 0

    def start_activity(self, activity, details=None):
        self.activities |= activity
        self.activity_details[activity] = details

    def end_activity(self, activity):
        self.activities &= ~activity

    def is_active(self, activity) -> bool:
        return bool(self.activities & activity)

    def stop_tracking(self):
        self.stopped_tracking += 1

    # The real implementations, so the endpoints exercise them rather than fakes of them.
    def goto_ra_dec_j2000(self, ra, dec):
        return Mount.goto_ra_dec_j2000(self, ra, dec)  # type: ignore[arg-type]

    def goto_ra_dec_apparent(self, ra, dec):
        return Mount.goto_ra_dec_apparent(self, ra, dec)  # type: ignore[arg-type]

    def _goto_equatorial(self, slew, ra_in, dec_in, op):
        return Mount._goto_equatorial(self, slew, ra_in, dec_in, op)  # type: ignore[arg-type]


def goto(stub, alt=45.0, az=180.0):
    return Mount.goto_alt_az(stub, alt, az)  # type: ignore[arg-type]


class TestTheHappyPath:
    def test_it_fires_the_slew_and_reports_ok(self):
        stub = Stub()
        response = goto(stub, 45.0, 180.0)

        assert response.succeeded
        assert stub.pw.calls == [(45.0, 180.0)], "PWI4 must be asked exactly once, with the validated values"

    def test_tracking_is_stopped_before_slewing(self):
        """An alt/az target is a fixed direction; sidereal tracking would drag the mount
        straight off it."""
        stub = Stub()
        goto(stub)
        assert stub.stopped_tracking == 1

    def test_the_slewing_activity_is_left_running(self):
        """Started here, ended by ontimer once the mount has moved and stopped -- the
        same lifecycle as goto_ra_dec_j2000."""
        stub = Stub()
        goto(stub)
        assert stub.is_active(MountActivities.Slewing)

    def test_the_slewing_activity_carries_the_target(self):
        """ "Slewing" alone does not say where to, which is the first thing anyone asks of
        a moving telescope. This is what a client watching /status sees."""
        stub = Stub()
        goto(stub, 45.0, 180.0)

        details = stub.activity_details[MountActivities.Slewing]
        assert details, "the activity must carry details"
        assert "45" in details[0] and "180" in details[0], f"the target must be in them: {details}"

    def test_the_target_is_a_string_not_a_tuple(self):
        """status() renders a tuple target as RA/Dec, so an alt/az pair put there would
        be displayed as a sky position it is not."""
        stub = Stub()
        goto(stub, 45.0, 180.0)
        assert isinstance(stub.target, str)
        assert "alt=45" in stub.target and "az=180" in stub.target


class TestRefusals:
    def test_a_disconnected_mount_is_refused_before_anything_else(self):
        stub = Stub(connected=False)
        response = goto(stub)

        assert response.failed and "not connected" in response.errors[0]
        assert stub.pw.calls == [] and stub.stopped_tracking == 0, "nothing may happen on a disconnected mount"
        assert not stub.is_active(MountActivities.Slewing)

    def test_the_ranges_are_declared_on_the_parameters(self):
        """Enforced by FastAPI/pydantic before the method runs, so a bad value is a 422
        and never reaches the mount. Declared rather than hand-checked, which also puts
        the limits in the OpenAPI schema where an operator can see them."""
        import typing

        def bounds(annotation):
            # FastAPI keeps the constraints as annotated_types objects (Ge, Le, Lt) on
            # Query.metadata, not as plain attributes.
            query = typing.get_args(annotation)[1]
            return {type(m).__name__.lower(): getattr(m, type(m).__name__.lower()) for m in query.metadata}

        hints = typing.get_type_hints(Mount.goto_alt_az, include_extras=True)

        assert bounds(hints["alt_degs"]) == {"ge": Mount.MIN_ALTITUDE_DEGREES, "le": Mount.MAX_ALTITUDE_DEGREES}
        assert bounds(hints["az_degs"]) == {"ge": 0.0, "lt": 360.0}, (
            "azimuth must be [0, 360) -- 360 excluded, not folded to 0"
        )


class TestWhenPwi4Fails:
    def test_the_slewing_activity_does_not_stay_stuck(self):
        """ontimer only ends Slewing after seeing the mount move and stop. A slew PWI4
        never accepted never moves, so without ending it here the unit would report a
        slew that is not happening, indefinitely."""
        stub = Stub(raises=True)
        response = goto(stub)

        assert response.failed
        assert not stub.is_active(MountActivities.Slewing), "the activity must not be left set"
        assert stub.target is None, "and the stale target must be cleared with it"


class TestGotoRaDecJ2000:
    """The equatorial sibling. `/mount/goto` and the divergent `goto()` it pointed at
    were retired in #37; these two verbs are what is left.
    """

    def test_the_retired_goto_is_gone(self):
        assert not hasattr(Mount, "goto"), "#37 retired the divergent goto()"

    def test_the_internal_method_raises_rather_than_returning_an_error(self):
        """Four callers (acquirer, autofocusing, stage_geometry, dance) invoke this
        directly and ignore the return value. Reporting a failure by returning would make
        it a silent no-op -- an acquisition carrying on believing it had slewed."""
        stub = Stub(raises=True)

        with pytest.raises(RuntimeError):
            Mount.goto_ra_dec_j2000(stub, 12.5, -45.0)  # type: ignore[arg-type]

        assert not stub.is_active(MountActivities.Slewing), "and the activity must not stick"
        assert stub.target is None

    def test_the_target_is_a_tuple_here(self):
        """The opposite of goto_alt_az: status() renders a tuple as RA/Dec, which is
        exactly right for an equatorial target."""
        stub = Stub()
        Mount.goto_ra_dec_j2000(stub, 12.5, -45.0)  # type: ignore[arg-type]

        assert stub.target == (12.5, -45.0)
        assert stub.is_active(MountActivities.Slewing)
        details = stub.activity_details[MountActivities.Slewing]
        assert "12.5" in details[0], f"the activity must carry the target: {details}"

    def test_the_endpoint_turns_a_failure_into_a_canonical_response(self):
        stub = Stub(raises=True)
        response = Mount.endpoint_goto_ra_dec_j2000(stub, 12.5, -45.0)  # type: ignore[arg-type]

        assert response.failed, "the endpoint reports, where the internal method raises"
        assert not stub.is_active(MountActivities.Slewing)

    def test_the_endpoint_refuses_a_disconnected_mount(self):
        stub = Stub(connected=False)
        response = Mount.endpoint_goto_ra_dec_j2000(stub, 12.5, -45.0)  # type: ignore[arg-type]

        assert response.failed and "not connected" in response.errors[0]
        assert stub.pw.calls == []

    @pytest.mark.parametrize(("ra", "dec"), [("12:30:45", "-45:30:00"), (12.5, -45.5)], ids=["sexagesimal", "decimal"])
    def test_both_coordinate_forms_are_accepted(self, ra, dec):
        """As unit.expose accepts them. #78 notes RA/Dec is conventionally sexagesimal."""
        stub = Stub()
        assert Mount.endpoint_goto_ra_dec_j2000(stub, ra, dec).succeeded  # type: ignore[arg-type]

    @pytest.mark.parametrize(("ra", "dec"), [(25.0, 0.0), (0.0, 95.0)], ids=["ra past 24h", "dec past 90"])
    def test_out_of_range_coordinates_are_refused(self, ra, dec):
        stub = Stub()
        response = Mount.endpoint_goto_ra_dec_j2000(stub, ra, dec)  # type: ignore[arg-type]

        assert response.failed
        assert stub.pw.calls == [], "the mount must not be driven with a value that failed validation"


class TestGotoRaDecApparent:
    """The of-date sibling. PWI4 reports and accepts both frames, so both are routed."""

    def test_it_slews_and_reports_ok(self):
        stub = Stub()
        assert Mount.endpoint_goto_ra_dec_apparent(stub, 12.5, -45.5).succeeded  # type: ignore[arg-type]
        assert stub.pw.calls == [(12.5, -45.5)]

    def test_the_target_names_the_frame(self):
        """A bare RA/Dec target would be indistinguishable from a J2000 one in the
        operator's status view, and the two frames differ by ~22 arcmin."""
        stub = Stub()
        Mount.goto_ra_dec_apparent(stub, 12.5, -45.5)  # type: ignore[arg-type]

        assert isinstance(stub.target, str)
        assert "apparent" in stub.target, f"the frame must be visible: {stub.target}"
        assert "12:30:00" in stub.target and "-45:30:00" in stub.target

    def test_it_raises_rather_than_returning(self):
        stub = Stub(raises=True)
        with pytest.raises(RuntimeError):
            Mount.goto_ra_dec_apparent(stub, 12.5, -45.5)  # type: ignore[arg-type]
        assert not stub.is_active(MountActivities.Slewing)
        assert stub.target is None

    def test_the_endpoint_refuses_a_disconnected_mount(self):
        stub = Stub(connected=False)
        response = Mount.endpoint_goto_ra_dec_apparent(stub, 12.5, -45.5)  # type: ignore[arg-type]
        assert response.failed and "not connected" in response.errors[0]
        assert stub.pw.calls == []

    @pytest.mark.parametrize(("ra", "dec"), [(25.0, 0.0), (0.0, 95.0)], ids=["ra past 24h", "dec past 90"])
    def test_out_of_range_is_refused(self, ra, dec):
        stub = Stub()
        assert Mount.endpoint_goto_ra_dec_apparent(stub, ra, dec).failed  # type: ignore[arg-type]
        assert stub.pw.calls == []


class TestTargetRendering:
    """What `status()` shows the operator for `target`."""

    def test_a_tuple_declination_is_read_as_degrees(self):
        """It was read as ARCSECONDS, so a target at -45.5 degrees was displayed as
        "-0:00:45.500" -- 3600x too small, and plausible enough to go unnoticed."""
        rendered = target_as_text((12.5, -45.5))

        assert rendered is not None
        assert "12:30:00" in rendered, rendered
        assert "-45:30:00" in rendered, f"declination must be read as degrees, got {rendered}"
        assert "-0:00:45" not in rendered, "the arcsecond misreading"

    def test_a_string_target_is_passed_through(self):
        """Home, and the alt/az and apparent verbs, carry their own frame."""
        assert target_as_text("Home") == "Home"
        assert target_as_text("alt=45, az=180") == "alt=45, az=180"

    def test_no_target_renders_as_nothing(self):
        assert target_as_text(None) is None
