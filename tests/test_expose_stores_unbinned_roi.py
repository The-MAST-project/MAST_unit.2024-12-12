"""`/unit/expose` stores its ROI in unbinned sensor pixels, as every other writer does (#335).

PHD2's `set_limit_frame` takes the rect unbinned and divides it by the binning itself, and
`ImagerSettings.make_file_name` bins it once for the `binned_roi=` tag. A rect binned
before it is stored is binned twice: on a binning-2 PHD2 profile a full-frame expose would
read out the upper-left quarter of the sensor. Binning 1 is not tested: `binned(1)` is the
identity, so it cannot tell the two apart.
"""

from __future__ import annotations

import threading
import types
from contextlib import nullcontext
from typing import Literal

import pytest

import unit as unit_module
from common.models.statuses import ImagerRoi
from common.rois import UnitRoi
from unit import Unit

SENSOR_X, SENSOR_Y = 8288, 5644
FIBER_X, FIBER_Y = SENSOR_X // 2, SENSOR_Y // 2
FRAME_SECONDS = 5.0
GAIN = 100


class Recorder:
    """An imager that exposes instantly and keeps the settings it was given."""

    def __init__(self):
        self.settings = []
        self.latest_settings = None

    def start_exposure(self, settings):
        self.settings.append(settings)

    def wait_for_image_saved(self):
        pass


def make_unit() -> Unit:
    """Enough of a Unit to run `_expose_repeatedly`'s own body."""
    u = object.__new__(Unit)
    u.imager = Recorder()
    u.mount = types.SimpleNamespace()
    u._expose_cancelled = threading.Event()
    return u


@pytest.fixture(autouse=True)
def no_filesystem(monkeypatch, tmp_path):
    monkeypatch.setattr(unit_module, "PathMaker", lambda: types.SimpleNamespace(make_exposures_folder=lambda: str(tmp_path)))
    monkeypatch.setattr(unit_module, "MoveGuardian", lambda: types.SimpleNamespace(protect=lambda path: nullcontext()))
    monkeypatch.setattr(unit_module, "filer", types.SimpleNamespace(move_ram_to_shared=lambda path: None))


def stored_roi(binning: Literal[1, 2], width: int = SENSOR_X, height: int = SENSOR_Y) -> ImagerRoi:
    u = make_unit()
    u._expose_repeatedly(
        1,
        FRAME_SECONDS,
        None,
        GAIN,
        binning,
        FIBER_X,
        FIBER_Y,
        width,
        height,
        None,
        None,
        0.0,
    )
    (settings,) = u.imager.settings
    assert settings.binning == binning
    return settings.roi


@pytest.mark.parametrize("size", [(SENSOR_X, SENSOR_Y), (1000, 800)], ids=["full-frame", "sub-frame"])
def test_a_binning_2_expose_stores_unbinned_sensor_pixels(size):
    expected = ImagerRoi.from_other(roi=UnitRoi(FIBER_X, FIBER_Y, *size))

    roi = stored_roi(2, *size)

    assert (roi.x, roi.y, roi.width, roi.height) == (expected.x, expected.y, expected.width, expected.height)
