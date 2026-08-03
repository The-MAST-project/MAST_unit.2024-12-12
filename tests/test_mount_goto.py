"""`/mount/goto` delegates to the maintained slew instead of forking it.

Covers MAST_unit #37: the endpoint used to be a second copy of the slew that called
`pw.mount_goto_ra_dec_j2000` directly and skipped the activity/target bookkeeping, so a
slew started through the API was invisible to `wait_until_settled(SettleMode.SLEW)` and to
mount status. These tests pin the delegation and the target rendering.

Windows-only by import chain (`mount` imports `win32com`), like the rest of the suite. No
hardware: `Mount` comes from `object.__new__` with a recording stand-in for the PWI4 client,
and the methods under test are the real ones.
"""

from __future__ import annotations

import pytest

pytest.importorskip("win32com", reason="mount.py is Windows-only")

from common.activities import MountActivities  # noqa: E402
from common.canonical import CanonicalResponse  # noqa: E402


class _RecordingPw:
    """Stands in for the PWI4 client, recording the slew commands it is given."""

    def __init__(self):
        self.calls: list[tuple] = []

    def mount_goto_ra_dec_j2000(self, ra_hours, dec_degs):
        self.calls.append(("j2000", ra_hours, dec_degs))


def make_mount(connected: bool = True):
    from mount import Mount

    class _Mount(Mount):
        def __init__(self):  # bypass hardware init
            pass

        @property
        def connected(self):
            return connected

        def start_activity(self, activity, **kwargs):
            self.started.append(activity)

    mount = object.__new__(_Mount)
    mount.pw = _RecordingPw()
    mount.started = []
    mount.target = None
    return mount


def assert_refused(response, expected: str) -> None:
    assert isinstance(response, CanonicalResponse), f"expected a CanonicalResponse, got {type(response).__name__}"
    assert response.errors, f"expected errors, got {response!r}"
    assert any(expected in err for err in response.errors), f"{expected!r} not in {response.errors}"


def test_equatorial_goto_delegates_to_the_maintained_slew():
    mount = make_mount()

    response = mount.endpoint_goto(ra_j2000_hours=12.5, dec_j2000_degs=-30.25)

    assert not response.errors
    assert mount.pw.calls == [("j2000", 12.5, -30.25)]
    # the bookkeeping the old fork skipped:
    assert MountActivities.Slewing in mount.started
    assert mount.target == (12.5, -30.25)


def test_goto_refuses_when_not_connected():
    mount = make_mount(connected=False)

    assert_refused(mount.endpoint_goto(ra_j2000_hours=1.0, dec_j2000_degs=2.0), "not connected")
    assert mount.pw.calls == []


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"ra_j2000_hours": 1.0}, "both ra_j2000_hours and dec_j2000_degs are required"),
        ({"dec_j2000_degs": 2.0}, "both ra_j2000_hours and dec_j2000_degs are required"),
        ({"alt_degs": 45.0}, "both alt_degs and az_degs are required"),
        ({"az_degs": 200.0}, "both alt_degs and az_degs are required"),
    ],
)
def test_target_verbal_renders_declination_in_degrees():
    """Regression: Dec was rendered as `unit='arcsec'`, dividing it by 3600 in status."""
    mount = make_mount()
    mount.target = (12.5, 30.5)

    assert mount.target_verbal() == "[12:30:00.000, 30:30:00.000]"


def test_target_verbal_passes_strings_through():
    mount = make_mount()
    mount.target = "Home"

    assert mount.target_verbal() == "Home"


def test_target_verbal_is_none_without_a_target():
    assert make_mount().target_verbal() is None
