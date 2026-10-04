"""A stalled focuser must fail the phase, not hang it.

Observed on sky 2026-07-21: the donut path commanded 27499 -> 26649, the focuser
stalled after ~14 ticks, and `/calibrate/focuser` hung for 10+ minutes.  Two
things made it unrecoverable, and both are pinned here:

* ``Focuser.ontimer`` re-commands a move that has gone stationary short of
  target, with no attempt limit -- so ``FocuserActivities.Moving`` never clears
  on its own and an unbounded wait is unbounded in practice, not just in theory;
* the wait did not consult the abort flag either, so ``POST /calibrate/abort``
  could not free it -- the run was advertised as abortable and was not.

No hardware: the unit is a mock whose focuser simply never stops Moving.
"""

import time
from unittest.mock import MagicMock

import pytest

from calibration.phases.focuser import FocuserCalibrator, FocuserMoveError


class FakeFocuser:
    """A focuser stalled short of its target.

    NOT a ``MagicMock``: assigning ``.position`` on a mock replaces the
    attribute, so a readback would return the commanded value and the test could
    never tell "stuck at 27485" from "stuck at 26649".  The real ``position`` is
    a property whose setter commands a move and whose getter reads PWI4 for the
    ACTUAL position -- that asymmetry is the whole point here, so it is modelled
    literally.
    """

    CLOSE_ENOUGH = 2

    def __init__(self, actual: int, target: int):
        self._actual = actual  # where the hardware really is; never moves
        self.target = target
        self.commanded: list[int] = []

    @property
    def position(self) -> int:
        return self._actual

    @position.setter
    def position(self, value: int):
        self.commanded.append(value)  # commanded, but the focuser does not move

    def is_active(self, _activity) -> bool:
        return True  # Moving, forever -- ontimer keeps re-commanding


@pytest.fixture
def stalled_unit():
    """A unit whose focuser reports Moving forever -- i.e. the stall."""
    unit = MagicMock()
    unit.focuser = FakeFocuser(actual=27485, target=26649)
    unit.is_active.return_value = True  # calibration still active
    return unit


@pytest.fixture
def settings():
    st = MagicMock()
    st.min_position = 0
    st.max_position = 49999
    return st


def test_stalled_focuser_raises_instead_of_hanging(stalled_unit, settings, monkeypatch):
    monkeypatch.setattr("calibration.phases.focuser.FOCUSER_MOVE_TIMEOUT_SECONDS", 1.0)
    calibrator = FocuserCalibrator(stalled_unit)

    started = time.monotonic()
    with pytest.raises(FocuserMoveError) as excinfo:
        calibrator._move_focuser(26649, settings)
    elapsed = time.monotonic() - started

    assert elapsed < 10, "must give up promptly, not hang"
    message = str(excinfo.value)
    # The message has to name where it actually got to: that is what
    # distinguishes "focuser stalled" from "target unreachable", and it had to be
    # dug out of PWI4 by hand the first time this happened.
    assert "26649" in message, "names the commanded position"
    assert "27485" in message, "names where it actually stopped"


def test_message_names_the_stall_not_the_command(stalled_unit, settings, monkeypatch):
    """Regression: the message must read the ACTUAL position, not echo the target.

    ``Focuser.position`` is a property -- the setter commands, the getter reads
    PWI4.  Reporting the commanded value would make every stall self-describe as
    "stuck at <exactly where I told it to go>", which is useless.
    """
    monkeypatch.setattr("calibration.phases.focuser.FOCUSER_MOVE_TIMEOUT_SECONDS", 1.0)

    with pytest.raises(FocuserMoveError, match=r"stuck at 27485"):
        FocuserCalibrator(stalled_unit)._move_focuser(26649, settings)

    assert stalled_unit.focuser.commanded == [26649], "the move WAS commanded"


def test_abort_breaks_the_wait(stalled_unit, settings, monkeypatch):
    """`/calibrate/abort` must free a wait that would otherwise never end."""
    monkeypatch.setattr("calibration.phases.focuser.FOCUSER_MOVE_TIMEOUT_SECONDS", 60.0)
    stalled_unit.is_active.return_value = False  # operator cleared the flags
    calibrator = FocuserCalibrator(stalled_unit)

    started = time.monotonic()
    with pytest.raises(FocuserMoveError, match="aborted"):
        calibrator._move_focuser(26649, settings)

    # Well inside the 60s timeout: it stopped because of the abort, not the cap.
    assert time.monotonic() - started < 10


def test_normal_move_does_not_raise(settings):
    """The move that arrives must stay silent -- no false stall."""
    unit = MagicMock()
    unit.is_active.return_value = True
    # Moving for the first few polls, then clears, as a real move does.
    unit.focuser.is_active.side_effect = [True, True, True, False]

    FocuserCalibrator(unit)._move_focuser(26649, settings)

    assert unit.focuser.position == 26649
