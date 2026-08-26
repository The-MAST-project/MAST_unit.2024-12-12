"""Resolving a name to a target, in the mount and in the acquirer.

Neither service is reached: `resolve_object_name` is replaced throughout. What is pinned
is the part a wrong answer would be silent about -- which source won, what happens when
resolution fails, and whether the provenance survives as far as the operator.

The failure this guards is the one the resolver's own module note describes: a
misresolution is indistinguishable from success at every layer below it. The mount slews
normally, guiding locks, and a spectrum is taken of the wrong object. So "we could not
resolve it" must never quietly become "acquire wherever we happen to be pointing".
"""

from __future__ import annotations

import pytest

from acquirer import Acquirer
from common.activities import MountActivities
from common.object_resolver import MovingTargetError, ObjectNameError, ResolvedObject
from mount import Mount

M31 = ResolvedObject(
    name="M31",
    ra_j2000_hours=0.712305,
    dec_j2000_degs=41.268750,
    resolver="sesame",
    canonical_name="M 31",
    database="simbad@cds",
)


class FakePw:
    def __init__(self, raises=False):
        self.calls: list[tuple] = []
        self.raises = raises

    def mount_goto_ra_dec_j2000(self, ra, dec):
        if self.raises:
            raise RuntimeError("PWI4 refused the slew")
        self.calls.append((ra, dec))


class MountStub:
    """Enough of a Mount to run the real endpoint bodies."""

    def __init__(self, connected=True, raises=False):
        self.connected = connected
        self.pw = FakePw(raises)
        self.target = None
        self.activities = MountActivities(0)
        self.activity_details: dict = {}

    def start_activity(self, activity, details=None):
        self.activities |= activity
        self.activity_details[activity] = details

    def end_activity(self, activity):
        self.activities &= ~activity

    def goto_ra_dec_j2000(self, ra, dec, target_label=None):
        return Mount.goto_ra_dec_j2000(self, ra, dec, target_label)  # type: ignore[arg-type]

    def _goto_equatorial(self, slew, ra_in, dec_in, op):
        return Mount._goto_equatorial(self, slew, ra_in, dec_in, op)  # type: ignore[arg-type]


@pytest.fixture
def resolver(monkeypatch):
    """Replace the resolver in BOTH modules that import it by name."""

    class Fake:
        answer: ResolvedObject | None = M31
        fail_with: Exception | None = None

        def __call__(self, name, total_timeout=None):
            self.calls.append((name, total_timeout))
            if self.fail_with is not None:
                raise self.fail_with
            return self.answer

    fake = Fake()
    fake.calls = []
    import acquirer as acquirer_module
    import mount as mount_module

    monkeypatch.setattr(mount_module, "resolve_object_name", fake)
    monkeypatch.setattr(acquirer_module, "resolve_object_name", fake)
    return fake


def goto_object(stub, name="M31", timeout=15.0):
    return Mount.endpoint_goto_object(stub, name, timeout)  # type: ignore[arg-type]


class TestMountGotoObject:
    def test_it_resolves_then_slews_to_the_resolved_position(self, resolver):
        stub = MountStub()

        response = goto_object(stub, "M31")

        assert response.succeeded
        assert resolver.calls == [("M31", 15.0)]
        assert stub.pw.calls == [(M31.ra_j2000_hours, M31.dec_j2000_degs)], "the slew uses the resolved coordinates"

    def test_the_target_records_the_name_and_who_answered(self, resolver):
        """A bare (ra, dec) tuple in `target` discards the only thing that could later
        explain why the night pointed there. `goto_ra_dec_apparent` labels its target for
        the same reason."""
        stub = MountStub()

        goto_object(stub, "M31")

        assert isinstance(stub.target, str), "not a tuple: two numbers cannot say where they came from"
        assert "M31" in stub.target, "the name that was asked for"
        assert "M 31" in stub.target, "the identifier that actually matched"
        assert "simbad@cds" in stub.target, "and the catalogue that said so"

    def test_a_name_that_does_not_resolve_slews_nothing(self, resolver):
        resolver.fail_with = ObjectNameError("'Zaphod' could not be resolved")
        stub = MountStub()

        response = goto_object(stub, "Zaphod")

        assert response.failed
        assert stub.pw.calls == [], "an unresolved name must not become a slew to anywhere"
        assert not (stub.activities & MountActivities.Slewing), "and must not leave Slewing running"

    def test_a_moving_target_is_refused_without_slewing(self, resolver):
        """Comets have no fixed J2000, only an ephemeris at an instant."""
        resolver.fail_with = MovingTargetError("'C/2023 A3' is a moving target")
        stub = MountStub()

        response = goto_object(stub, "C/2023 A3")

        assert response.failed and "moving target" in response.errors[0]
        assert stub.pw.calls == []

    def test_a_disconnected_mount_is_reported_not_slewed(self, resolver):
        response = goto_object(MountStub(connected=False))
        assert response.failed and "not connected" in response.errors[0]

    def test_the_resolve_timeout_is_passed_through(self, resolver):
        goto_object(MountStub(), "M31", timeout=3.0)
        assert resolver.calls == [("M31", 3.0)]


class PwStatus:
    def __init__(self, connected=True, ra=12.0, dec=34.0):
        self.mount = type("M", (), {"is_connected": connected, "ra_j2000_hours": ra, "dec_j2000_degs": dec})()


def coordinates(resolver_unused=None, *, ra=None, dec=None, name=None, timeout=15.0, pw=None):
    """Run the real `_target_coordinates` against a bare Acquirer."""
    acquirer = Acquirer.__new__(Acquirer)
    return Acquirer._target_coordinates(acquirer, "op", ra, dec, name, timeout, pw or PwStatus())


class TestAcquisitionPrecedence:
    def test_valid_coordinates_win_and_the_name_is_never_resolved(self, resolver):
        ra, dec, problem = coordinates(ra=1.5, dec=-20.0, name="M31")

        assert (ra, dec, problem) == (1.5, -20.0, None)
        assert resolver.calls == [], "explicit coordinates are the most certain thing a caller can supply"

    def test_zero_is_a_coordinate_not_a_missing_one(self, resolver):
        """RA 0h is in Pisces and dec 0 is the celestial equator. Both are falsy, and the
        truthiness test this replaces sent them to the "not supplied" branch -- silently
        acquiring wherever the mount happened to point. Reachable from Python, not only
        over HTTP: unit.py's assignment path calls the endpoint directly with plan floats.
        """
        ra, dec, problem = coordinates(ra=0.0, dec=0.0, pw=PwStatus(ra=12.0, dec=34.0))

        assert (ra, dec, problem) == (0.0, 0.0, None), "the telescope's position must not have been substituted"

    @pytest.mark.parametrize(
        ("ra", "dec"),
        [(None, -20.0), (1.5, None), (None, None)],
        ids=["no-ra", "no-dec", "neither"],
    )
    def test_a_half_supplied_pair_falls_through_to_the_name(self, resolver, ra, dec):
        """Half a pair cannot be completed from the other half; mixing one supplied axis
        with one from the telescope points at neither."""
        got_ra, got_dec, problem = coordinates(ra=ra, dec=dec, name="M31")

        assert problem is None
        assert (got_ra, got_dec) == (M31.ra_j2000_hours, M31.dec_j2000_degs)
        assert resolver.calls == [("M31", 15.0)]

    @pytest.mark.parametrize("bad", ["not a coordinate", "99:99:99"], ids=["garbage", "out-of-range"])
    def test_unusable_coordinates_fall_through_to_the_name(self, resolver, bad):
        got_ra, _dec, problem = coordinates(ra=bad, dec=-20.0, name="M31")

        assert problem is None and got_ra == M31.ra_j2000_hours
        assert resolver.calls == [("M31", 15.0)]

    def test_no_coordinates_and_no_name_still_uses_the_telescope(self, resolver):
        """The behaviour that existed before object_name did, unchanged."""
        ra, dec, problem = coordinates(pw=PwStatus(ra=7.0, dec=8.0))

        assert (ra, dec, problem) == (7.0, 8.0, None)
        assert resolver.calls == []

    def test_unusable_coordinates_with_no_name_are_an_error(self, resolver):
        """Not a silent fall-through to the telescope: a caller who mistyped an RA needs
        to be told that, not pointed somewhere else."""
        _ra, _dec, problem = coordinates(ra="not a coordinate", dec=-20.0)

        assert problem is not None and "unusable" in problem

    def test_a_name_that_fails_to_resolve_is_an_error_not_a_fallback(self, resolver):
        """The misresolution failure in its purest form: everything below works perfectly
        and the spectrum is of the wrong sky."""
        resolver.fail_with = ObjectNameError("no such object")

        ra, dec, problem = coordinates(name="Zaphod", pw=PwStatus(ra=7.0, dec=8.0))

        assert (ra, dec) == (None, None)
        assert problem is not None and "Zaphod" in problem
        assert "7.0" not in str(problem), "the telescope's position must not be used instead"

    def test_a_moving_target_is_refused(self, resolver):
        resolver.fail_with = MovingTargetError("'Mars' is a moving target")

        _ra, _dec, problem = coordinates(name="Mars")

        assert problem is not None and "moving target" in problem

    def test_a_disconnected_mount_with_nothing_else_is_reported(self, resolver):
        _ra, _dec, problem = coordinates(pw=PwStatus(connected=False))
        assert problem is not None and "not connected" in problem
