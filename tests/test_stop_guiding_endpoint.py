"""`stop_guiding`: the route an operator needs to act on a lock-validity assessment.

The supervisor reports that the guider is not on a star; a person reads the case and
decides. Before this route there was nothing to decide *with*: `pause_guiding` keeps
the lock, which is the last thing wanted when the lock is the problem, and
`stop_acquisition_and_guiding` also unwinds the acquisition and stops the mount
tracking -- more than "stop guiding" should mean, and not recoverable by a
`start_guiding`.

Runs in the unit venv (Windows): the import chain is Windows-only today (`stage.py`
uses pyximc names at module level). Skips cleanly elsewhere.
"""

from __future__ import annotations

import pytest

try:
    from guiding import Guider
except (ImportError, NameError) as ex:  # NameError: stage.py off-Windows
    pytest.skip(f"unit import chain unavailable here ({ex!r})", allow_module_level=True)


class FakeBackend:
    def __init__(self, *, guiding: bool = True) -> None:
        self.stopped = 0
        self._guiding = guiding

    @property
    def is_guiding(self) -> bool:
        return self._guiding

    def stop_guiding(self) -> None:
        self.stopped += 1


class FakeUnit:
    def __init__(self) -> None:
        self.ended: list = []

    def end_activity(self, activity) -> None:
        self.ended.append(activity)


def make_guider(backend, unit=None) -> Guider:
    guider = object.__new__(Guider)
    guider.unit = unit
    guider._backend = backend
    return guider


def test_stopping_reaches_the_backend_and_ends_the_activity():
    backend, unit = FakeBackend(), FakeUnit()
    assert not make_guider(backend, unit).endpoint_stop_guiding().failed
    assert backend.stopped == 1
    assert unit.ended, "the unit's Guiding activity must end, or /status still claims it"


def test_refuses_when_not_guiding():
    """Not an error worth hiding: it says the thing the operator meant to stop
    is already stopped, rather than reporting a success that did nothing."""
    response = make_guider(FakeBackend(guiding=False), FakeUnit()).endpoint_stop_guiding()
    assert response.failed
    assert "not guiding" in str(response.errors)


def test_refuses_without_a_unit():
    assert make_guider(FakeBackend(), None).endpoint_stop_guiding().failed
