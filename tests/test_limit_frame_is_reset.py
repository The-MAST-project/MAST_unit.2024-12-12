"""PHD2 must be left holding the limit frame it had before a MAST exposure.

`set_limit_frame` sets `need_to_reset_limit_frame`, and nothing read it -- so a limit frame
set for one exposure stayed on PHD2 indefinitely, including for an operator driving PHD2 by
hand afterwards. Found on mast00 on 2026-08-17: PHD2 was still holding `[7, 1, 8272, 5640]`
from an earlier exposure and reported its camera frame size as 8272x5640 rather than the
sensor's 8288x5644.

That mattered for the sequence that broke: acquisition, stop guiding by hand, then a spiral.
Each stage left its constraint behind for the next.

Clearing to `None` was right only because nothing was in force beforehand. An exposure
taken while a guide loop holds its own limit frame -- a paused loop mid-handover -- had
that frame silently dropped, so PHD2 went on reporting star positions in full-sensor
coordinates while the exclusion rectangle it still held was translated for a crop origin
that no longer applied. Observed on mast01 2026-09-08. The reset now restores the previous
frame, which is the same thing as clearing whenever there was nothing to restore.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

try:
    from phd2.phd2 import PHD2Connector
except Exception as ex:  # noqa: BLE001 -- the import chain is Windows-and-hardware-only
    pytest.skip(f"unit import chain unavailable here ({ex!r})", allow_module_level=True)


ROI = SimpleNamespace(x=520, y=0, width=7760, height=4812)


def _connector(need_reset: bool, raises: bool = False, previous=None):
    """A stub carrying only the four attributes the reset path reads.

    `previous` is what was in force before the exposure -- `None` for the
    standalone case this was written for, a guiding crop for the handover case.
    """
    calls: list = []

    def send_limit_frame(roi=None):
        calls.append(roi)
        if raises:
            raise RuntimeError("PHD2 went away")

    stub = SimpleNamespace(
        need_to_reset_limit_frame=need_reset,
        limit_frame_to_restore=previous,
        limit_frame_in_force=None,
        _send_limit_frame=send_limit_frame,
        image_was_saved=True,
    )
    stub.reset_limit_frame_if_needed = lambda: PHD2Connector.reset_limit_frame_if_needed(stub)
    return stub, calls


def test_the_limit_frame_is_cleared_when_nothing_was_in_force():
    """The standalone-exposure case, unchanged: nothing to restore means clear."""
    stub, calls = _connector(need_reset=True, previous=None)

    PHD2Connector.reset_limit_frame_if_needed(stub)

    assert calls == [None], "PHD2 must be told to drop the limit frame"


def test_a_limit_frame_that_was_in_force_is_restored():
    """The handover case: an exposure inside a paused guide loop must give it back."""
    stub, calls = _connector(need_reset=True, previous=ROI)

    PHD2Connector.reset_limit_frame_if_needed(stub)

    assert calls == [ROI], "the guiding crop must survive an exposure taken over it"


def test_the_reset_does_not_fire_twice():
    """A second call must not clear the frame it has just restored."""
    stub, calls = _connector(need_reset=True, previous=ROI)

    PHD2Connector.reset_limit_frame_if_needed(stub)
    PHD2Connector.reset_limit_frame_if_needed(stub)

    assert calls == [ROI]
    assert stub.limit_frame_to_restore is None


def test_nothing_is_sent_when_no_limit_frame_was_set():
    stub, calls = _connector(need_reset=False)

    PHD2Connector.reset_limit_frame_if_needed(stub)

    assert calls == [], "an exposure that set no limit frame must not send a reset"


def test_a_failure_to_reset_does_not_break_the_exposure():
    """Tidying up must never be the thing that fails a frame that was already saved."""
    stub, calls = _connector(need_reset=True, raises=True, previous=ROI)

    PHD2Connector.reset_limit_frame_if_needed(stub)

    assert calls == [ROI]
    assert not stub.need_to_reset_limit_frame, "a failed restore must not leave the flag armed"


def test_waiting_for_the_image_performs_the_reset():
    """Wired into `wait_for_image_saved`, not `stop_exposure`.

    The non-guiding path a single frame takes never calls `stop_exposure`, so a reset
    hung there would never run.
    """
    stub, calls = _connector(need_reset=True)

    PHD2Connector.wait_for_image_saved(stub)

    assert calls == [None]
