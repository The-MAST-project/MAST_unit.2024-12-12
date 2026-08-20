"""`start_tracking` / `stop_tracking` must refuse audibly, and must not wait for ever.

Both used to begin with

    if not self.connected:
        return

which commanded nothing, logged nothing, and handed the caller `None`. On 2026-08-17 a
spiral search called `start_tracking()` on a mount whose `connected` was false, got that
bare `None`, and ran three sessions to completion against a mount that was never told to
track. The frames trailed; the last session reported a shift of (-0.0, 0.01) at confidence
0.93 -- an answer that was wrong and looked healthy. Nothing anywhere said why.

Both also spun on `while ... is_tracking` with no deadline, so a mount that would not
engage hung its caller indefinitely -- including `spiral_search.start()`, which holds the
session lock while it waits.

The condition they refuse on is now the servo axes rather than the connection: tracking
needs energized axes, and `connected` no longer implies them (MAST_unit#175).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from common.canonical import CanonicalResponse
from mount import Mount


class FakePw:
    """A PWI4 client whose reported tracking state is scripted."""

    def __init__(self, reports: list[bool]):
        self.reports = list(reports)
        self.commanded: list[str] = []

    def status(self):
        value = self.reports.pop(0) if self.reports else self.reports_default
        return SimpleNamespace(mount=SimpleNamespace(is_tracking=value))

    reports_default = False

    def mount_tracking_on(self):
        self.commanded.append("on")

    def mount_tracking_off(self):
        self.commanded.append("off")


def _mount(deployed: bool, reports: list[bool], timeout: int = 30):
    pw = FakePw(reports)
    stub = SimpleNamespace(connected=True, deployed=deployed, pw=pw, TRACKING_TIMEOUT_SECONDS=timeout)
    stub._await_tracking = lambda desired: Mount._await_tracking(stub, desired)
    return stub, pw


@pytest.mark.parametrize("method", ["start_tracking", "stop_tracking"])
def test_refuses_audibly_when_the_axes_are_not_enabled(method):
    """The regression: a bare `return` here cost a night's frames."""
    stub, pw = _mount(deployed=False, reports=[])

    result = getattr(Mount, method)(stub)

    assert isinstance(result, CanonicalResponse), "must return an envelope, not None"
    assert result.failed
    assert "axes are not enabled" in result.errors[0]
    assert pw.commanded == [], "must not command a mount whose servos are de-energized"


@pytest.mark.parametrize(
    ("method", "verb", "engaged"),
    [("start_tracking", "on", True), ("stop_tracking", "off", False)],
)
def test_succeeds_when_the_mount_obeys(method, verb, engaged):
    stub, pw = _mount(deployed=True, reports=[engaged])

    result = getattr(Mount, method)(stub)

    assert result.succeeded
    assert pw.commanded == [verb]


@pytest.mark.parametrize(
    ("method", "never"),
    [("start_tracking", False), ("stop_tracking", True)],
)
def test_times_out_rather_than_hanging(method, never):
    """The mount never reaches the requested state; the call must end and say so.

    A 1-second budget keeps the test quick -- the point is that a deadline exists at all,
    not its production value.
    """
    stub, pw = _mount(deployed=True, reports=[], timeout=1)
    pw.reports_default = never  # always the wrong state

    result = getattr(Mount, method)(stub)

    assert isinstance(result, CanonicalResponse)
    assert result.failed
    assert "did not" in result.errors[0]


def test_does_not_connect_as_a_side_effect():
    """Acquiring the device is `startup()`'s job, not an operation's.

    `startup()` energizes both axes and goes on to `find_home()`, which moves the telescope.
    An operation asking to track must not do any of that implicitly.
    """
    stub, pw = _mount(deployed=False, reports=[])
    stub.connect = lambda: pytest.fail("start_tracking must not connect")
    stub.startup = lambda: pytest.fail("start_tracking must not run startup")

    Mount.start_tracking(stub)
