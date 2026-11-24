"""`Slewing` must not end before the slew it announces has started.

The mount timer ends `Slewing` once the axes are quiet. Right after a goto they are quiet
because nothing has moved yet: on mast07 (2026-11-23) the first read after an alt/az goto ended
`Slewing` 0.03 s in. Quiet axes now count as "arrived" only once PWI4 has been seen slewing,
or `SLEW_START_GRACE_SECONDS` after the goto.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

try:
    import mount as mount_module
    from mount import Mount
except Exception as ex:  # noqa: BLE001 -- the import chain is Windows-and-hardware-only
    pytest.skip(f"unit import chain unavailable here ({ex!r})", allow_module_level=True)


@pytest.fixture
def clock(monkeypatch):
    now = SimpleNamespace(t=500.0)
    monkeypatch.setattr(mount_module.time, "monotonic", lambda: now.t)
    return now


@pytest.fixture
def mount(clock):
    m = object.__new__(Mount)
    m.target = "alt=80, az=180"
    m.start_activity = lambda *a, **k: None  # type: ignore[method-assign]
    m._start_slewing()
    return m


def test_quiet_axes_right_after_the_goto_are_not_an_arrival(mount, clock):
    clock.t += 0.03
    assert not mount._slew_may_have_ended()


def test_once_pwi4_was_seen_slewing_quiet_axes_are_an_arrival(mount, clock):
    clock.t += 1.0
    mount._slew_seen = True
    assert mount._slew_may_have_ended()


def test_a_slew_never_seen_ends_after_the_grace(mount, clock):
    """A short slew can start and finish between two timer reads."""
    clock.t += mount_module.SLEW_START_GRACE_SECONDS
    assert mount._slew_may_have_ended()


def test_each_goto_restarts_the_evidence(mount, clock):
    mount._slew_seen = True
    clock.t += 10
    mount._start_slewing()
    assert not mount._slew_may_have_ended()


def test_a_mount_that_never_commanded_a_slew_is_not_held(clock):
    assert object.__new__(Mount)._slew_may_have_ended()
