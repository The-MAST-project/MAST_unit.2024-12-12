"""Virtual time for tests of bounded waits: `sleep` advances the clock at once.

A wait loop under test keeps its real logic -- its deadline, its polls, its abort check --
and only stops costing wall-clock time. Patch a module's `time` with `patch_time`:

    clock = patch_time(monkeypatch, "calibration.phases.focuser")
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace


class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.now = start
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def patch_time(monkeypatch, module: str) -> FakeClock:
    """Replace ``module.time`` with a `FakeClock`; returns it."""
    clock = FakeClock()
    fake = SimpleNamespace(monotonic=clock.monotonic, time=clock.time, sleep=clock.sleep, perf_counter=clock.monotonic)
    monkeypatch.setattr(importlib.import_module(module), "time", fake)
    return clock
