"""An exposure must leave PHD2's limit frame exactly as it found it (#245).

`set_limit_frame` armed `need_to_reset_limit_frame` on the guiding path and the
exposure path consumed it: a value written by one caller and read by a different
one. Two incidents came out of that.

- **mast00, 2026-08-17.** Nothing read the flag at all, so a limit frame set for
  one exposure stayed on PHD2 indefinitely -- including for an operator driving
  PHD2 by hand afterwards. PHD2 was found holding `[7, 1, 8272, 5640]` and
  reporting its camera frame size as 8272x5640 rather than the sensor's
  8288x5644. The sequence that broke was acquisition, stop guiding by hand, then
  a spiral: each stage left its constraint behind for the next. The fix made the
  exposure path clear the frame.
- **mast01, 2026-09-08.** Clearing then took a frame the exposure had never set
  -- the guide loop's own. PHD2 went on reporting star positions in full-sensor
  coordinates while the exclusion rectangle it still held had been translated for
  a crop origin that no longer applied, so a re-selection could land inside the
  fold mirror's shadow.

The second incident was patched here by restoring the previous frame instead of
clearing, which made the symptom rare rather than impossible: it still rested on a
remembered value being right.

The fix is not a better memory. An exposure never had to touch the limit frame:
`capture_single_frame` carries its own `limit_frame` parameter and the guiding
path uses `save_image`, so nothing on the exposure path needs to mutate PHD2's
state and there is nothing to put back because nothing was disturbed.

These tests therefore assert an absence -- across a MAST exposure, no
limit-frame traffic reaches PHD2 and whatever it held before it still holds
after.
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
        if method == "set_limit_frame":
            self.limit_frame = params["roi"] if isinstance(params, dict) else params
        return {"result": 0}


def _connector(phd2: RecordingPhd2) -> PHD2Connector:
    """`image_was_saved=True` so the wait returns at once."""
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
    """mast01 2026-09-08, and the regression this branch still carries."""
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
