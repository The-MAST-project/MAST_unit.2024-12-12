"""A frame that did not read out fully must stop the session.

On 2026-08-17 four spiral sessions on mast00 produced frames that stopped dead at row 4881
of 5640 -- the same row every time, in every frame -- with the remaining 758 rows entirely
zero. The sources in what did arrive were one pixel tall and ~78 wide, and PHD2's own
display showed the same, so the fault is at or below the camera. The FITS was written at its
declared size and only partly filled.

Nothing noticed. Every session ran to completion, and the last reported a shift of
(-0.0, 0.01) at confidence 0.93 -- wrong, and indistinguishable from healthy, because a
correlation between two frames sharing the same dead region locks onto it. `at_origin` did
not fire either: 0.01 is not exactly 0.0.

The cause is still unknown, so this checks the symptom. A truncated readout cannot yield a
usable measurement whatever produced it.
"""

from __future__ import annotations

import numpy as np
import pytest

from spiral_search import MAX_TRAILING_BLANK_ROWS, SpiralSearchError, _reject_truncated_frame


def frame(rows: int = 200, cols: int = 64, blank_tail: int = 0) -> np.ndarray:
    """A frame of plausible sky with `blank_tail` dead rows at the bottom."""
    rng = np.random.default_rng(1)
    a = rng.integers(1900, 2100, size=(rows, cols), dtype=np.uint16)
    if blank_tail:
        a[rows - blank_tail :] = 0
    return a


def test_a_good_frame_passes():
    _reject_truncated_frame(frame(), "reference.fits")


def test_the_mast00_shape_is_rejected():
    """4882 rows of 5640, the rest zero -- scaled down but the same shape."""
    with pytest.raises(SpiralSearchError) as excinfo:
        _reject_truncated_frame(frame(rows=200, blank_tail=27), "reference.fits")

    message = str(excinfo.value)
    assert "truncated" in message
    assert "27" in message and "200" in message, "the message must say how much is missing"
    assert "reference.fits" in message, "and which frame"


@pytest.mark.parametrize("blank_tail", [0, 1, MAX_TRAILING_BLANK_ROWS])
def test_a_dark_edge_row_is_tolerated(blank_tail):
    """One or two dead rows at the sensor edge are unremarkable; a block of them is not."""
    _reject_truncated_frame(frame(blank_tail=blank_tail), "final.fits")


def test_one_row_past_the_tolerance_is_rejected():
    with pytest.raises(SpiralSearchError):
        _reject_truncated_frame(frame(blank_tail=MAX_TRAILING_BLANK_ROWS + 1), "final.fits")


def test_blank_rows_elsewhere_do_not_trip_it():
    """Only a truncated TAIL means a partial readout.

    A dead band in the middle is a different fault -- and rejecting on it would fire on any
    frame with a large masked region.
    """
    a = frame()
    a[50:90] = 0

    _reject_truncated_frame(a, "reference.fits")


def test_an_entirely_blank_frame_is_rejected():
    with pytest.raises(SpiralSearchError):
        _reject_truncated_frame(np.zeros((200, 64), dtype=np.uint16), "reference.fits")


@pytest.mark.parametrize("value", [None, np.zeros((0, 4), dtype=np.uint16), np.zeros(8, dtype=np.uint16)])
def test_it_does_not_raise_on_shapes_it_cannot_judge(value):
    """The guard must never be the thing that breaks an exposure."""
    _reject_truncated_frame(value, "reference.fits")
