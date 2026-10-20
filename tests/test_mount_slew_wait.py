"""The slew wait checks for a slew at least once, even when no start grace is left.

A caller that reaches the wait well after the goto (the acquisition, after its stage wait)
passes `start_grace_seconds=0`. The slew may still be running then, or long over; neither
case is a warning (MAST_unit#315).
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

import mount as mount_module
from mount import Mount

FAR_DEADLINE_SECONDS = 60.0


class _Pw:
    def __init__(self, slewing: list[bool]):
        self.slewing = list(slewing)
        self.polls = 0

    def status(self):
        self.polls += 1
        is_slewing = self.slewing.pop(0) if self.slewing else False
        return SimpleNamespace(mount=SimpleNamespace(is_slewing=is_slewing))


class _Logger:
    def __init__(self):
        self.warnings: list[str] = []

    def info(self, msg):
        pass

    def warning(self, msg):
        self.warnings.append(msg)


@pytest.fixture
def log(monkeypatch):
    recorder = _Logger()
    monkeypatch.setattr(mount_module, "logger", recorder)
    return recorder


def _wait_slew(slewing: list[bool], start_grace_seconds: float, monkeypatch) -> tuple[bool, _Pw]:
    m = object.__new__(Mount)
    pw = _Pw(slewing)
    monkeypatch.setattr(m, "pw", pw, raising=False)
    monkeypatch.setattr(m, "_wait_dist_settle", lambda *args: True)
    settled = m._wait_slew(
        dist_tolerance_arcsec=10.0,
        stable_samples=2,
        start_grace_seconds=start_grace_seconds,
        poll_seconds=0.0,
        deadline=time.monotonic() + FAR_DEADLINE_SECONDS,
        op="test",
    )
    return settled, pw


def test_a_slew_still_running_is_waited_out_with_no_grace(log, monkeypatch):
    settled, pw = _wait_slew([True, True, False], start_grace_seconds=0.0, monkeypatch=monkeypatch)

    assert settled
    assert pw.slewing == []
    assert log.warnings == []


def test_a_finished_slew_with_no_grace_is_not_a_warning(log, monkeypatch):
    settled, _ = _wait_slew([False], start_grace_seconds=0.0, monkeypatch=monkeypatch)

    assert settled
    assert log.warnings == []


def test_a_slew_never_seen_within_a_requested_grace_is_still_a_warning(log, monkeypatch):
    _wait_slew([], start_grace_seconds=0.05, monkeypatch=monkeypatch)

    assert len(log.warnings) == 1
