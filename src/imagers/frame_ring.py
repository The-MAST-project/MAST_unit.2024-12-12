"""A small ring of preallocated frame buffers, so a camera streaming at a guide cadence does not allocate a frame each time.

``FrameRing.next(height, width, dtype)`` hands out the buffers in turn. **The rule that comes with it:** the frame read
into a buffer is valid until the buffer is handed out again, which with a ring of ``depth`` is ``depth`` readouts later. A
consumer that needs a frame for longer must copy it. A ring of two gives the frame being processed one whole exposure
before its buffer is needed again, and the thread that saves a frame is handed its own copy
(``ZWOImager._frame_for_saver``).
"""

from __future__ import annotations

import numpy as np


class FrameRing:
    def __init__(self, depth: int = 2):
        if depth < 2:
            raise ValueError(f"a ring of {depth} buffers would hand out the frame it was just asked to keep")
        self._slots: list[np.ndarray | None] = [None] * depth
        self._next = 0

    @property
    def depth(self) -> int:
        return len(self._slots)

    def next(self, height: int, width: int, dtype) -> np.ndarray:
        """The next buffer of shape ``(height, width)``, writable and C-contiguous; its contents are whatever the last
        frame left in it. It is allocated the first time it is asked for, and again if the shape or the type changed."""
        i = self._next
        self._next = (self._next + 1) % len(self._slots)
        slot = self._slots[i]
        dtype = np.dtype(dtype)
        if slot is None or slot.shape != (height, width) or slot.dtype != dtype:
            slot = np.empty((height, width), dtype=dtype)
            self._slots[i] = slot
        return slot

    def clear(self) -> None:
        """Let the buffers go (the camera was turned off, or the setting that wanted them was)."""
        self._slots = [None] * len(self._slots)
        self._next = 0
