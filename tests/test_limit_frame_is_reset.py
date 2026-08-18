"""PHD2 must not be left holding a limit frame after a MAST exposure.

`set_limit_frame` sets `need_to_reset_limit_frame`, and nothing read it -- so a limit frame
set for one exposure stayed on PHD2 indefinitely, including for an operator driving PHD2 by
hand afterwards. Found on mast00 on 2026-08-17: PHD2 was still holding `[7, 1, 8272, 5640]`
from an earlier exposure and reported its camera frame size as 8272x5640 rather than the
sensor's 8288x5644.

That mattered for the sequence that broke: acquisition, stop guiding by hand, then a spiral.
Each stage left its constraint behind for the next.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

try:
    from phd2.phd2 import PHD2Connector
except Exception as ex:  # noqa: BLE001 -- the import chain is Windows-and-hardware-only
    pytest.skip(f"unit import chain unavailable here ({ex!r})", allow_module_level=True)


def _connector(need_reset: bool, raises: bool = False):
    calls: list = []

    def set_limit_frame(roi=None):
        calls.append(roi)
        if raises:
            raise RuntimeError("PHD2 went away")

    stub = SimpleNamespace(
        need_to_reset_limit_frame=need_reset,
        set_limit_frame=set_limit_frame,
        image_was_saved=True,
    )
    stub.reset_limit_frame_if_needed = lambda: PHD2Connector.reset_limit_frame_if_needed(stub)
    return stub, calls


def test_the_limit_frame_is_cleared_after_a_frame_is_saved():
    stub, calls = _connector(need_reset=True)

    PHD2Connector.reset_limit_frame_if_needed(stub)

    assert calls == [None], "PHD2 must be told to drop the limit frame"


def test_nothing_is_sent_when_no_limit_frame_was_set():
    stub, calls = _connector(need_reset=False)

    PHD2Connector.reset_limit_frame_if_needed(stub)

    assert calls == [], "an exposure that set no limit frame must not send a reset"


def test_a_failure_to_reset_does_not_break_the_exposure():
    """Tidying up must never be the thing that fails a frame that was already saved."""
    stub, calls = _connector(need_reset=True, raises=True)

    PHD2Connector.reset_limit_frame_if_needed(stub)

    assert calls == [None]


def test_waiting_for_the_image_performs_the_reset():
    """Wired into `wait_for_image_saved`, not `stop_exposure`.

    The non-guiding path a single frame takes never calls `stop_exposure`, so a reset
    hung there would never run.
    """
    stub, calls = _connector(need_reset=True)

    PHD2Connector.wait_for_image_saved(stub)

    assert calls == [None]
