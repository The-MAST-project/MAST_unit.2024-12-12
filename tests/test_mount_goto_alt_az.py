"""What `Mount.goto_alt_az` does around the PWI4 call.

The mount is never constructed -- that reaches for ASCOM, a power switch and PWI4 -- so
the method is called on a stand-in carrying only what it touches.
"""

from __future__ import annotations

from common.activities import MountActivities
from mount import MAX_ALTITUDE_DEGREES, MIN_ALTITUDE_DEGREES, Mount


class FakePw:
    def __init__(self, raises=False):
        self.calls: list[tuple] = []
        self.raises = raises

    def mount_goto_alt_az(self, alt_degs, az_degs):
        if self.raises:
            raise RuntimeError("PWI4 refused the slew")
        self.calls.append((alt_degs, az_degs))


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

        assert bounds(hints["alt_degs"]) == {"ge": MIN_ALTITUDE_DEGREES, "le": MAX_ALTITUDE_DEGREES}
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
