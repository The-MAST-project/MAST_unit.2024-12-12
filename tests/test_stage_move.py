"""A stuck or refused stage must fail the sweep loudly, not corrupt it.

Two failure modes pinned, both cousins of on-sky incidents from 2026-07-21:

* the old ``_move_stage`` waited unbounded on ``is_moving`` with no abort check
  -- the same class of wait that pinned the focus phase for 10+ minutes with
  ``/calibrate/abort`` powerless;
* on a *refused* move it appended an error and RETURNED, so the sweep exposed
  at the wrong stage position and paired the commanded position with a shadow
  measured elsewhere -- corrupting the linear fit rather than failing it.  The
  geometry solver cannot detect that; only the move layer can.
"""

import time
from unittest.mock import MagicMock

import pytest

from calibration.phases.stage import StageCalibrator, StageMoveError


class FakeStage:
    """A stage that accepts the command but never stops moving."""

    def __init__(self):
        self.position = 31000
        self.is_moving = True
        self.commanded: list[int] = []

    def move_absolute(self, position):
        self.commanded.append(position)
        return None  # accepted (no .failed)

    def is_active(self, _activity):
        return False  # the raw is_moving flag alone keeps the wait alive


@pytest.fixture
def calibrator():
    unit = MagicMock()
    unit.stage = FakeStage()
    unit.is_active.return_value = True  # calibration active -> no abort
    return StageCalibrator(unit)


def test_stalled_stage_raises_instead_of_hanging(calibrator, monkeypatch):
    monkeypatch.setattr("calibration.phases.stage.STAGE_MOVE_TIMEOUT_SECONDS", 1.0)

    started = time.monotonic()
    with pytest.raises(StageMoveError) as excinfo:
        calibrator._move_stage(28000)

    assert time.monotonic() - started < 10, "must give up promptly"
    message = str(excinfo.value)
    assert "28000" in message, "names the commanded position"
    assert "31000" in message, "names where the stage actually is"
    assert calibrator.unit.stage.commanded == [28000], "the move WAS commanded"


def test_abort_breaks_the_wait(calibrator, monkeypatch):
    """``/calibrate/abort`` clears the flags; the wait must honour that."""
    monkeypatch.setattr("calibration.phases.stage.STAGE_MOVE_TIMEOUT_SECONDS", 60.0)
    calibrator.unit.is_active.return_value = False  # operator cleared the flags

    started = time.monotonic()
    with pytest.raises(StageMoveError, match="aborted"):
        calibrator._move_stage(28000)

    # well inside the 60s cap: it stopped because of the abort, not the timeout
    assert time.monotonic() - started < 10


def test_refused_move_raises_immediately(calibrator):
    """A refused command must not let the sweep continue from the wrong place."""
    refusal = MagicMock()
    refusal.failed = True
    refusal.errors = ["out of travel"]
    calibrator.unit.stage.move_absolute = lambda p: refusal

    with pytest.raises(StageMoveError, match="refused"):
        calibrator._move_stage(28000)


def test_completed_move_returns_quietly(calibrator):
    calibrator.unit.stage.is_moving = False

    calibrator._move_stage(28000)  # no exception

    assert calibrator.unit.stage.commanded == [28000]
