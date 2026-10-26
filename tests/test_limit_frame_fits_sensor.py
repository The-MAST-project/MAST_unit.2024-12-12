"""PHD2 is never sent a limit frame its camera will reject (MAST_unit#330).

PHD2's ZWO driver widens a limit frame to 32-px boundaries (`constrain_roi` in upstream
`cam_zwo.cpp`), in binned coordinates and without keeping the result inside the sensor.
The camera rejects a frame that ends up past the sensor; PHD2 logs it and carries on,
copying the camera's rows with the rejected frame's stride. On mast03 that sheared every
guide frame of 2026-10-12. `phd2_requested_frame` below restates PHD2's arithmetic
independently of the unit's own check, so these tests measure against PHD2, not against
the code under test.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

try:
    import guiding as guiding_module
    from guiding import Guider
    from phd2.phd2 import PHD2Connector, PHD2ConnectorError
except (ImportError, NameError) as ex:  # NameError: stage.py off-Windows
    pytest.skip(f"unit import chain unavailable here ({ex!r})", allow_module_level=True)

from common import asi
from common.activities import UnitActivities
from common.config.phd2 import LimitFrameConfig, LimitFrameMode, PHD2Config
from common.config.rois import FcuVersion, SpecRoiConfig
from common.models.statuses import ImagerRoi, ImagerSettings
from common.rois import UnitRoi
from failure_streaks import FailureStreaks

SENSOR_WIDTH, SENSOR_HEIGHT = asi.ASI_294MM_WIDTH, asi.ASI_294MM_HEIGHT
PHD2_ALIGNMENT = 32

# fcu_v2 fiber calibrations from the control DB, 2026-10-13, and the frame each derived
# before MAST_unit#330 (x, y, width, height).
FLEET = {
    "mast03": ((3987, 2829), (10, 14, 7952, 5628)),
    "mast00": ((4335, 3095), (390, 546, 7888, 5096)),
    "mast01-and-mast04-08": ((4401, 2407), (520, 0, 7760, 4812)),
    "mast02": ((4050, 2439), (9, 0, 8080, 4876)),
    "mastw": ((3803, 2746), (10, 1, 7584, 5488)),
}
ALREADY_FIT = ["mast01-and-mast04-08", "mast02", "mastw"]

MAST03_OLD_FRAME = FLEET["mast03"][1]
# Fits at binning 1; at binning 2 its right edge, 8278 -> 4139, widens to 4160 > 4144.
FITS_AT_BIN_1_ONLY = (390, 0, 7888, 4000)


def phd2_requested_frame(frame: tuple[int, int, int, int], binning: int) -> tuple[int, int, int, int]:
    """What PHD2 asks the camera for: binned, intersected with the sensor, widened to 32."""
    x, y, w, h = (v // binning for v in frame)
    sensor_w = (SENSOR_WIDTH // binning) & ~7
    sensor_h = (SENSOR_HEIGHT // binning) & ~1
    right, bottom = min(x + w, sensor_w), min(y + h, sensor_h)
    left, top = x - x % PHD2_ALIGNMENT, y - y % PHD2_ALIGNMENT
    right = -(-right // PHD2_ALIGNMENT) * PHD2_ALIGNMENT
    bottom = -(-bottom // PHD2_ALIGNMENT) * PHD2_ALIGNMENT
    return left, top, right - left, bottom - top


def camera_accepts(frame: tuple[int, int, int, int], binning: int = 1) -> bool:
    x, y, w, h = phd2_requested_frame(frame, binning)
    return x + w <= (SENSOR_WIDTH // binning) & ~7 and y + h <= (SENSOR_HEIGHT // binning) & ~1


def as_tuple(roi: ImagerRoi) -> tuple[int, int, int, int]:
    return roi.x, roi.y, roi.width, roi.height


def center(frame: tuple[int, int, int, int]) -> tuple[int, int]:
    x, y, w, h = frame
    return x + w // 2, y + h // 2


def derived_frame(fiber: tuple[int, int], monkeypatch) -> tuple[int, int, int, int]:
    fiber_x, fiber_y = fiber
    guiding_conf = SimpleNamespace(
        rois={FcuVersion.v2: SpecRoiConfig(margin_horizontal=0, margin_vertical=0, fiber_x=fiber_x, fiber_y=fiber_y)},
        exposure=3.0,
        gain=170,
        binning=1,
    )
    monkeypatch.setattr(
        guiding_module, "Config", lambda: SimpleNamespace(get_unit=lambda: SimpleNamespace(guiding=guiding_conf))
    )
    g = object.__new__(Guider)
    unit = SimpleNamespace(
        imager=SimpleNamespace(camera_x_size=SENSOR_WIDTH, camera_y_size=SENSOR_HEIGHT), fcu_version=FcuVersion.v2
    )
    monkeypatch.setattr(g, "unit", unit, raising=False)
    settings = Guider.make_guiding_settings(g, save=False)
    assert settings.roi is not None
    return as_tuple(settings.roi)


@pytest.mark.parametrize("unit", list(FLEET))
def test_every_derived_frame_is_one_the_camera_accepts(unit, monkeypatch):
    fiber, _ = FLEET[unit]

    assert camera_accepts(derived_frame(fiber, monkeypatch))


@pytest.mark.parametrize("unit", list(FLEET))
def test_a_derived_frame_keeps_its_center(unit, monkeypatch):
    fiber, _ = FLEET[unit]

    assert center(derived_frame(fiber, monkeypatch)) == fiber


@pytest.mark.parametrize("unit", ALREADY_FIT)
def test_a_frame_that_already_fit_is_unchanged(unit, monkeypatch):
    fiber, before = FLEET[unit]
    frame = derived_frame(fiber, monkeypatch)

    assert frame[2] >= before[2] and frame[3] >= before[3]
    assert center(frame) == fiber


@dataclass
class Rig:
    connector: PHD2Connector
    rpc: MagicMock
    unit: MagicMock
    parent: MagicMock

    def sent_frames(self) -> list:
        return [c.kwargs["params"]["roi"] for c in self.rpc.call_args_list if c.args and c.args[0] == "set_limit_frame"]

    def methods(self) -> list[str]:
        return [c.args[0] for c in self.rpc.call_args_list if c.args]


GET_EXPOSURE_DURATIONS = {"get_exposure_durations": {"result": [1000, 2000, 3000, 5000]}}


def rig(binning: asi.ASI_294MM_SUPPORTED_BINNINGS_LITERAL = 1, limit_frame: LimitFrameConfig | None = None) -> Rig:
    """A real PHD2Connector minus the heavy __init__, recording its RPC calls."""
    p = object.__new__(PHD2Connector)
    p._connected = True
    p.watched_process = None
    p.settle = None
    p.settle_px = 0
    p.app_state = ""
    p.lock = threading.Lock()
    p.errors = []
    p._read_failures = FailureStreaks()
    p.image_saved_event = threading.Event()
    p._guide_session = 0
    p._nudge_onto_target = False
    rpc = MagicMock(name="call", side_effect=lambda method, params=None: GET_EXPOSURE_DURATIONS.get(method, {}))
    p.call = rpc
    doc = {
        "profile": f"PWI4+ASI-native,binning={binning},bpp=16",
        "settle": {"pixels": 1, "time": 0, "timeout": 0},
        "validation_interval": 0.0,
    }
    if limit_frame is not None:
        doc["limit_frame"] = limit_frame.model_dump()
    unit = MagicMock(name="unit")
    unit.unit_conf.phd2 = PHD2Config(**doc)
    unit.guider.make_guiding_settings.return_value = ImagerSettings(
        seconds=3.0,
        binning=binning,
        gain=170,
        format="raw16",
        roi=ImagerRoi.verbatim(*FLEET["mastw"][1]),
        image_path="unused",
    )
    parent = MagicMock(name="parent")
    parent.unit = unit
    p.parent = parent
    return Rig(p, rpc, unit, parent)


def test_a_full_sensor_frame_is_sent_as_no_limit_frame():
    r = rig()

    r.connector.set_limit_frame(roi=ImagerRoi.verbatim(0, 0, SENSOR_WIDTH, SENSOR_HEIGHT))

    assert r.sent_frames() == [None]


def test_a_frame_the_camera_would_reject_is_never_sent():
    r = rig()

    with pytest.raises(PHD2ConnectorError):
        r.connector.set_limit_frame(roi=ImagerRoi.verbatim(*MAST03_OLD_FRAME))

    assert r.sent_frames() == []


def test_a_frame_the_camera_accepts_is_sent_as_is():
    r = rig()

    r.connector.set_limit_frame(roi=ImagerRoi.verbatim(*FLEET["mastw"][1]))

    assert r.sent_frames() == [list(FLEET["mastw"][1])]


@pytest.mark.parametrize(
    ("binning", "frame"),
    [(1, MAST03_OLD_FRAME), (2, FITS_AT_BIN_1_ONLY)],
    ids=["bottom-edge-at-bin-1", "right-edge-at-bin-2"],
)
def test_guiding_does_not_start_with_a_frame_the_camera_would_reject(binning, frame):
    assert not camera_accepts(frame, binning)
    x, y, w, h = frame
    r = rig(binning, LimitFrameConfig(mode=LimitFrameMode.FIXED, x=x, y=y, width=w, height=h))

    response = r.connector.start_guiding()

    assert response.errors
    assert "guide" not in r.methods()
    assert r.sent_frames() == []
    r.unit.start_activity.assert_not_called()


def test_guiding_starts_with_a_frame_the_camera_accepts():
    x, y, w, h = FITS_AT_BIN_1_ONLY
    r = rig(1, LimitFrameConfig(mode=LimitFrameMode.FIXED, x=x, y=y, width=w, height=h))

    r.connector.start_guiding()

    assert r.sent_frames() == [list(FITS_AT_BIN_1_ONLY)]
    r.unit.start_activity.assert_called_once_with(UnitActivities.Guiding)


TRIMMED_FULL_FRAMES = {
    "conditioned-full-sensor": lambda: ImagerRoi(x=0, y=0, width=SENSOR_WIDTH, height=SENSOR_HEIGHT),
    "default-expose": lambda: ImagerRoi.from_other(
        UnitRoi(SENSOR_WIDTH // 2, SENSOR_HEIGHT // 2, SENSOR_WIDTH, SENSOR_HEIGHT)
    ).binned(1),
    "trimmed-three-times": lambda: ImagerRoi.verbatim(21, 3, 8240, 5632),
}


@pytest.mark.parametrize("make", list(TRIMMED_FULL_FRAMES.values()), ids=list(TRIMMED_FULL_FRAMES))
def test_a_full_frame_trimmed_by_conditioning_is_sent_as_no_limit_frame(make):
    """ImagerRoi conditioning trims a full frame by a few px (MAST_common#17); PHD2 widens it back."""
    roi = make()
    assert phd2_requested_frame(as_tuple(roi), 1) == (0, 0, SENSOR_WIDTH, 5664)
    r = rig()

    r.connector.set_limit_frame(roi=roi)

    assert r.sent_frames() == [None]


def exposure(roi: ImagerRoi) -> ImagerSettings:
    return ImagerSettings(seconds=0.01, binning=1, gain=170, format="raw16", roi=roi, image_path="unused.fits")


def test_an_exposure_with_a_frame_the_camera_would_reject_is_refused_before_it_starts():
    r = rig()

    response = r.connector.start_exposure(exposure(ImagerRoi.verbatim(*MAST03_OLD_FRAME)))

    assert response.errors
    assert "capture_single_frame" not in r.methods()
    r.parent.start_activity.assert_not_called()


def test_a_default_expose_still_runs_with_no_limit_frame():
    r = rig()

    response = r.connector.start_exposure(exposure(TRIMMED_FULL_FRAMES["default-expose"]()))

    assert not response.errors
    assert r.sent_frames() == [None]
    assert "capture_single_frame" in r.methods()


@pytest.mark.parametrize(
    ("frame", "expected"),
    [
        ((0, 0, 8288, 5600), [0, 0, 8288, 5600]),  # short of the bottom: a real crop, kept
        ((400, 0, 7888, 5644), PHD2ConnectorError),  # full height but offset: refused, not full frame
        ((0, 640, 8288, 5000), PHD2ConnectorError),  # full width but offset: refused, not full frame
    ],
    ids=["cropped-bottom", "offset-left", "offset-top"],
)
def test_only_a_frame_widened_to_the_whole_sensor_becomes_no_limit_frame(frame, expected):
    r = rig()

    if expected is PHD2ConnectorError:
        with pytest.raises(PHD2ConnectorError):
            r.connector.set_limit_frame(roi=ImagerRoi.verbatim(*frame))
        assert r.sent_frames() == []
    else:
        r.connector.set_limit_frame(roi=ImagerRoi.verbatim(*frame))
        assert r.sent_frames() == [expected]


def test_whether_a_frame_covers_the_sensor_is_judged_at_the_profile_binning():
    frame = (40, 40, 8248, 5604)  # at bin 2: 20,20 -> widened to the whole 4144x2822; at bin 1: x=40 is not
    assert phd2_requested_frame(frame, 2) == (0, 0, 4160, 2848)
    r = rig(binning=2)

    r.connector.set_limit_frame(roi=ImagerRoi.verbatim(*frame))

    assert r.sent_frames() == [None]
