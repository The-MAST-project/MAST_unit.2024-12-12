"""`pause_guiding` / `resume_guiding`: the loop keeps its lock, and a paused loop can be recovered.

Pausing is not stopping, and the difference is the whole reason these routes exist.
`stop_acquisition_and_guiding` discards the selected star and the lock position; a pause
keeps both, which is what lets the fold-mirror handover bracket ~27 s of stage travel and
resume correcting toward the same lock.

The case that matters most here is `resume` on a loop that is *not* currently guiding by
the unit's reckoning. That is precisely the state the handover leaves behind when the
mirror fails to insert -- it pauses deliberately rather than resume onto a half-occulted
field -- and before these routes there was no way out of it but restarting the acquisition
or the service. A `resume` that refused because "not guiding" would reintroduce the trap.

Runs in the unit venv (Windows): the import chain is Windows-only today (`stage.py` uses
pyximc names at module level). Skips cleanly elsewhere.
"""

from __future__ import annotations

import pytest

try:
    from guiding import Guider
except (ImportError, NameError) as ex:  # NameError: stage.py off-Windows
    pytest.skip(f"unit import chain unavailable here ({ex!r})", allow_module_level=True)

from phd2.phd2 import PHD2Connector


class FakePhd2(PHD2Connector):
    """A PHD2Connector minus its __init__, recording what the endpoints ask of it."""

    def __init__(self, *, guiding: bool = True) -> None:  # noqa: D107
        self.paused_with: list[bool] = []
        self.unpaused = 0
        self._guiding = guiding

    @property
    def is_guiding(self) -> bool:
        return self._guiding

    def pause(self, full: bool = False) -> None:
        self.paused_with.append(full)

    def unpause(self) -> None:
        self.unpaused += 1


class NotPhd2:
    """Stands in for the solving backend, which has no pause concept."""

    is_guiding = True


def make_guider(backend) -> Guider:
    guider = object.__new__(Guider)
    guider.unit = None
    guider._backend = backend
    return guider


class TestPause:
    def test_full_is_the_default(self):
        """The handover needs the camera released, not just the corrections stopped."""
        backend = FakePhd2()
        assert not make_guider(backend).endpoint_pause_guiding().failed
        assert backend.paused_with == [True]

    def test_full_can_be_declined(self):
        backend = FakePhd2()
        make_guider(backend).endpoint_pause_guiding(full=False)
        assert backend.paused_with == [False]

    def test_pausing_a_loop_that_is_not_guiding_is_an_error(self):
        backend = FakePhd2(guiding=False)
        response = make_guider(backend).endpoint_pause_guiding()
        assert response.failed
        assert "not guiding" in response.errors[0]
        assert backend.paused_with == []

    def test_a_backend_that_cannot_pause_says_so(self):
        response = make_guider(NotPhd2()).endpoint_pause_guiding()
        assert response.failed
        assert "PHD2" in response.errors[0]


class TestResume:
    def test_resume_unpauses(self):
        backend = FakePhd2()
        assert not make_guider(backend).endpoint_resume_guiding().failed
        assert backend.unpaused == 1

    def test_resume_does_not_require_the_unit_to_think_it_is_guiding(self):
        """The recovery case: the handover's failure path leaves the loop paused.

        Gating resume on `is_guiding` would make the one state that needs recovering the
        one state that cannot be recovered.
        """
        backend = FakePhd2(guiding=False)
        assert not make_guider(backend).endpoint_resume_guiding().failed
        assert backend.unpaused == 1

    def test_a_backend_that_cannot_pause_says_so(self):
        response = make_guider(NotPhd2()).endpoint_resume_guiding()
        assert response.failed
        assert "PHD2" in response.errors[0]
