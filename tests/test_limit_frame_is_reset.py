"""An exposure must leave PHD2's limit frame exactly as it found it (#245).

The history this file guards is two incidents, both caused by one design: the
connector armed `need_to_reset_limit_frame` in `set_limit_frame` and consumed it
in the exposure path, so a flag written on the guiding path was read on a
different one.

- **mast00, 2026-08-17.** Nothing read the flag, so a limit frame set for one
  exposure stayed on PHD2 indefinitely -- including for an operator driving PHD2
  by hand afterwards. PHD2 was found holding `[7, 1, 8272, 5640]` and reporting
  its camera frame size as 8272x5640 rather than the sensor's 8288x5644. The
  sequence that broke was acquisition, stop guiding by hand, then a spiral: each
  stage left its constraint behind for the next.
- **mast01, 2026-09-08.** Once the flag *was* read, the reset cleared a frame the
  exposure had never set -- the guide loop's own. PHD2 went on reporting star
  positions in full-sensor coordinates while the exclusion rectangle it still held
  was translated for a crop origin that no longer applied, so a re-selection could
  land inside the fold mirror's shadow.

The second fix restored the previous frame instead of clearing, which made the
symptom rare rather than impossible: it still depended on a remembered value being
right. The root fix is that the exposure path does not touch the limit frame at
all. `capture_single_frame` carries its own `limit_frame` parameter and the
guiding path uses `save_image`, so neither needs to mutate PHD2's state, and
there is nothing to restore because nothing was disturbed.

These tests therefore assert an absence: across a MAST exposure, no limit-frame
traffic reaches PHD2 and whatever it held before it still holds after.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

try:
    from phd2.phd2 import PHD2Connector
except Exception as ex:  # noqa: BLE001 -- the import chain is Windows-and-hardware-only
    pytest.skip(f"unit import chain unavailable here ({ex!r})", allow_module_level=True)


GUIDING_CROP = [520, 0, 7760, 4812]


class RecordingPhd2:
    """Records every method reaching PHD2 and holds a limit frame nothing may change."""

    def __init__(self, limit_frame=None):
        self.limit_frame = limit_frame
        self.methods: list[str] = []

    def __call__(self, method, params=None, **kwargs):
        self.methods.append(method)
        if method == "get_limit_frame":
            return {"result": self.limit_frame}
        if method == "set_limit_frame":
            roi = params["roi"] if isinstance(params, dict) else params
            self.limit_frame = roi
            return {"result": 0}
        return {"result": 0}


def _connector(phd2: RecordingPhd2) -> PHD2Connector:
    c = object.__new__(PHD2Connector)
    c.call = phd2
    c._connected = True
    c.image_was_saved = True
    c.image_saved_event = threading.Event()
    c.image_saved_event.set()
    c.parent = SimpleNamespace(is_active=lambda _a: False, end_activity=lambda _a: None)
    return c


def test_waiting_for_the_image_sends_no_limit_frame_traffic():
    """The whole class of bug: an exposure path that reaches for the limit frame."""
    phd2 = RecordingPhd2(limit_frame=GUIDING_CROP)
    _connector(phd2).wait_for_image_saved(timeout=0.1)
    assert "set_limit_frame" not in phd2.methods


def test_a_guide_loops_crop_survives_an_exposure():
    """mast01 2026-09-08: the frame the loop was holding must still be there."""
    phd2 = RecordingPhd2(limit_frame=GUIDING_CROP)
    _connector(phd2).wait_for_image_saved(timeout=0.1)
    assert phd2.limit_frame == GUIDING_CROP


def test_nothing_is_left_behind_when_nothing_was_in_force():
    """mast00 2026-08-17, the other direction: no frame in, no frame out."""
    phd2 = RecordingPhd2(limit_frame=None)
    _connector(phd2).wait_for_image_saved(timeout=0.1)
    assert phd2.limit_frame is None
    assert "set_limit_frame" not in phd2.methods


@pytest.mark.parametrize("attribute", ["need_to_reset_limit_frame", "limit_frame_to_restore", "limit_frame_in_force"])
def test_the_flag_and_its_mirrors_are_gone(attribute):
    """A value written on one code path and read on another is the defect itself."""
    assert not hasattr(PHD2Connector, attribute)


def test_a_caller_that_needs_the_previous_frame_reads_it():
    """Putting something back means reading it first, not having been told to remember."""
    phd2 = RecordingPhd2(limit_frame=GUIDING_CROP)
    c = _connector(phd2)

    previous = c.get_limit_frame()
    c.set_limit_frame(roi=None)
    assert phd2.limit_frame is None

    c.set_limit_frame(roi=previous)
    assert phd2.limit_frame == GUIDING_CROP
