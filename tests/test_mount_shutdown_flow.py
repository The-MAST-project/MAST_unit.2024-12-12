"""Mount shutdown completes on its own, without a tick that can never come (#193, #253).

The bug these pin is not that the mount went unparked -- MAST has no defined park position,
and the OTA settles on its own balance once the axes are disabled. It is that
`MountActivities.ShuttingDown` could not be cleared by any path, so `Mount.powerdown()` spun
on an unbounded wait and `Unit.power_all_off()`, which walks the components in series with
the mount second of six, never reached the imager, covers, focuser or stage.

Two independent faults produced that, and a fix for either alone would have left the other:
`shutdown()` disconnected before calling `park()`, whose own `if self.connected:` then
no-oped so `Parking` never started; and `ontimer` returns early while disconnected, so the
block that ends `Parking` and `ShuttingDown` was unreachable in any case.
"""

from __future__ import annotations

import pytest

# No platform guard: `conftest` stubs the absent hardware modules, so this runs on a dev
# machine as well as on a unit (#52).
from common.activities import MountActivities


class RecordingActivities:
    """Stands in for the `Activities` mixin, recording transitions instead of notifying."""

    def __init__(self, active=()):
        self.active = set(active)
        self.started: list = []
        self.ended: list = []

    def is_active(self, activity):
        return activity in self.active

    def start_activity(self, activity, **kwargs):
        self.active.add(activity)
        self.started.append(activity)

    def end_activity(self, activity, **kwargs):
        self.active.discard(activity)
        self.ended.append(activity)


class FakePw:
    """The PWI4 client surface the shutdown path touches."""

    def __init__(self):
        self.requests: list[str] = []
        self.parked = False

    def request(self, path: str):
        self.requests.append(path)

    def mount_park(self):
        self.parked = True


def _mount(recorder, *, connected: bool, **attributes):
    """A Mount that runs its real methods over recorded flags and fake hardware."""
    from mount import Mount

    mount = object.__new__(Mount)
    for name in ("is_active", "start_activity", "end_activity"):
        setattr(mount, name, getattr(recorder, name))
    mount.pw = FakePw()
    mount._was_shut_down = False
    mount.disconnected = False
    mount.powered_off = False
    mount.disconnect = lambda: setattr(mount, "disconnected", True)
    mount.power_off = lambda: setattr(mount, "powered_off", True)
    type(mount).connected = property(lambda self, value=connected: value)
    for name, value in attributes.items():
        setattr(mount, name, value)
    return mount


def _release(mount):
    del type(mount).connected


# ------------------------------------------------------------------------------- shutdown


def test_shutdown_raises_shuttingdown_and_clears_it_itself():
    """The heart of #193. `ShuttingDown` stays part of the declared contract -- it is raised
    for the duration, so a concurrent reader of status sees the mount going down -- but it is
    ended HERE rather than left for `ontimer`, which returns early while disconnected and so
    could never clear it. Same shape as `Focuser.shutdown()`."""
    recorder = RecordingActivities()
    mount = _mount(recorder, connected=True)
    try:
        mount.shutdown()
    finally:
        _release(mount)

    assert MountActivities.ShuttingDown in recorder.started
    assert MountActivities.ShuttingDown in recorder.ended
    assert MountActivities.ShuttingDown not in recorder.active


def test_shutdown_still_declares_the_shuttingdown_completion():
    """The fix is the flag being CLEARABLE, not the flag going away. `is_shutting_down` is
    part of the `Component` contract and the declaration is what a consumer reads off Swagger,
    so demoting it to an immediate completion would remove a real signal to fix a real bug."""
    from common.endpoints import MARKER
    from mount import Mount

    declaration = getattr(Mount.shutdown, MARKER)
    assert declaration.completion is MountActivities.ShuttingDown


def test_shutdown_clears_the_flag_even_when_the_sequence_fails():
    """A failure part-way down must surface as an error, not as a flag nobody can clear and a
    `powerdown()` that never returns -- which is the failure mode this whole issue is about."""
    recorder = RecordingActivities()
    mount = _mount(recorder, connected=True)
    boom = RuntimeError("PDU unreachable")

    def explode():
        raise boom

    mount.power_off = explode
    try:
        with pytest.raises(RuntimeError):
            mount.shutdown()
    finally:
        _release(mount)

    assert MountActivities.ShuttingDown not in recorder.active


def test_shutdown_disconnects_and_powers_off_and_records_that_it_did():
    recorder = RecordingActivities()
    mount = _mount(recorder, connected=True)
    try:
        mount.shutdown()
    finally:
        _release(mount)

    assert mount.disconnected is True
    assert mount.powered_off is True
    assert mount._was_shut_down is True
    assert "/fans/off" in mount.pw.requests


def test_shutdown_does_not_park():
    """MAST has no defined park position; disconnecting disables both axes and the OTA
    settles on its own balance, and `startup()` re-homes on the way back up. Parking on the
    way down is what put a no-op between the disconnect and the flag that needed clearing."""
    recorder = RecordingActivities()
    mount = _mount(recorder, connected=True)
    try:
        mount.shutdown()
    finally:
        _release(mount)

    assert mount.pw.parked is False
    assert MountActivities.Parking not in recorder.started


def test_shutdown_of_an_already_disconnected_mount_still_completes():
    """The flag must clear whether or not there was a connection to drop -- otherwise a
    mount that lost PWI4 before shutdown strands the same waiter."""
    recorder = RecordingActivities()
    mount = _mount(recorder, connected=False)
    try:
        mount.shutdown()
    finally:
        _release(mount)

    assert MountActivities.ShuttingDown not in recorder.active
    assert mount.powered_off is True
    assert mount.disconnected is False


# ------------------------------------------------------------------------------ powerdown


def test_powerdown_returns_without_waiting_on_a_flag():
    """`Mount.powerdown()` had no deadline at all -- not a long one, none -- so a flag that
    could never clear hung it for ever, and `Unit.power_all_off()` with it."""
    recorder = RecordingActivities()
    mount = _mount(recorder, connected=True)
    try:
        mount.powerdown()
    finally:
        _release(mount)

    assert mount.powered_off is True
    assert MountActivities.ShuttingDown not in recorder.active


def test_powerdown_does_not_shut_down_twice():
    recorder = RecordingActivities()
    mount = _mount(recorder, connected=True)
    try:
        mount.shutdown()
        recorder.started.clear()
        mount.powerdown()
    finally:
        _release(mount)

    assert MountActivities.ShuttingDown not in recorder.started
    assert mount.powered_off is True


# ----------------------------------------------------------------------------------- park


def test_park_refuses_when_disconnected_instead_of_answering_ok():
    """`Parking` is ended only from `ontimer`, which returns early while disconnected. So a
    park accepted on a disconnected mount promises a move that cannot start and a flag that
    cannot clear. Refusing is the only honest answer (#156's family)."""
    recorder = RecordingActivities()
    mount = _mount(recorder, connected=False)
    try:
        response = mount.park()
    finally:
        _release(mount)

    assert response.failed
    assert mount.pw.parked is False
    assert MountActivities.Parking not in recorder.started


def test_park_still_parks_a_connected_mount():
    recorder = RecordingActivities()
    mount = _mount(recorder, connected=True)
    try:
        response = mount.park()
    finally:
        _release(mount)

    assert not response.failed
    assert mount.pw.parked is True
    assert MountActivities.Parking in recorder.started


# ------------------------------------------------------------------------------- contract


@pytest.mark.parametrize("method", ["shutdown", "powerdown"])
def test_shutdown_path_declares_no_activity_for_ontimer_to_finish(method):
    """A regression guard on the shape rather than the symptom (#253): nothing on the
    shutdown path may leave an activity for `ontimer` to end, because `ontimer` returns
    early while disconnected and shutting down is exactly when the mount is disconnected."""
    recorder = RecordingActivities()
    mount = _mount(recorder, connected=True)
    try:
        getattr(mount, method)()
    finally:
        _release(mount)

    assert recorder.active == set(), f"{method} left {recorder.active} for a tick that cannot come"
