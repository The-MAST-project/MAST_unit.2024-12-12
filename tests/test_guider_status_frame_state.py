"""`guider_status` reads PHD2's frame state on every poll; a PHD2 that is down must not flood the log.

After an end-of-night shutdown on mast01 and mast04 (2026-10-19) every status poll, about
every 7 s, logged a full "no connection to PHD2 server" traceback. A disconnected PHD2 is
not read at all; a read that fails is logged once per streak, as the other status reads are
(#260).
"""

from __future__ import annotations

import logging

import pytest

try:
    from test_phd2_state_is_read import DERIVED, FakePhd2

    from failure_streaks import FailureStreaks
    from phd2.phd2 import PHD2Connector
except Exception as ex:  # noqa: BLE001 -- the import chain is Windows-and-hardware-only
    pytest.skip(f"unit import chain unavailable here ({ex!r})", allow_module_level=True)


class Down:
    """A PHD2 whose socket is gone."""

    def __init__(self):
        self.calls = 0

    def __call__(self, method, *args, **kwargs):
        self.calls += 1
        raise RuntimeError("no connection to PHD2 server")


def connector(call, connected: bool = True) -> PHD2Connector:
    c = object.__new__(PHD2Connector)
    c.call = call
    c._connected = connected
    c._read_failures = FailureStreaks()
    return c


def errors(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_a_disconnected_phd2_is_not_read():
    down = Down()

    assert connector(down, connected=False)._frame_state() == (None, None, None)
    assert down.calls == 0


def test_a_failing_read_is_logged_once_per_streak(caplog):
    c = connector(Down())

    for _ in range(5):
        assert c._frame_state() == (None, None, None)

    assert len(errors(caplog)) == 1


def test_recovery_is_logged_and_rearms_the_streak(caplog):
    caplog.set_level(logging.INFO)
    c = connector(Down())
    c._frame_state()
    c.call = FakePhd2(limit_frame=DERIVED)

    limit_frame, _, _ = c._frame_state()

    assert (limit_frame.x, limit_frame.width) == (520, 7760)
    assert any("succeed again" in r.getMessage() for r in caplog.records)
    c.call = Down()
    c._frame_state()
    assert len(errors(caplog)) == 2
