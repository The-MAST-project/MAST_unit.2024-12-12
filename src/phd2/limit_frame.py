"""Which limit frames PHD2's ZWO driver can apply, and the guiding frame derived within them.

PHD2 widens a limit frame to multiples of `PHD2_ROI_ALIGNMENT`, in binned coordinates, and does
not keep the result inside the sensor (`constrain_roi` in upstream `cam_zwo.cpp`). The camera
rejects a frame widened past the sensor; PHD2 logs that and goes on copying the camera's rows
with the rejected frame's stride, shearing every frame (MAST_unit#330).

Kept free of the connector's imports so the solver can derive the same guiding frame.
"""

from __future__ import annotations

from common.config.rois import SpecRoiConfig
from common.models.statuses import ImagerRoi
from common.rois import SpecRoi

PHD2_ROI_ALIGNMENT = 32


def _binned_frame_size(sensor_width: int, sensor_height: int, binning: int) -> tuple[int, int]:
    """PHD2's binned frame: the binned sensor, its width floored to 8 and its height to 2."""
    return (sensor_width // binning) & ~7, (sensor_height // binning) & ~1


def _round_up(value: int) -> int:
    return -(-value // PHD2_ROI_ALIGNMENT) * PHD2_ROI_ALIGNMENT


def limit_frame_extent(sensor_width: int, sensor_height: int, binning: int) -> tuple[int, int]:
    """How far right and down, in unbinned pixels, a limit frame may reach and still be accepted."""
    width, height = _binned_frame_size(sensor_width, sensor_height, binning)
    return (width - width % PHD2_ROI_ALIGNMENT) * binning, (height - height % PHD2_ROI_ALIGNMENT) * binning


def limit_frame_accepted(roi: ImagerRoi, sensor_width: int, sensor_height: int, binning: int) -> bool:
    width, height = limit_frame_extent(sensor_width, sensor_height, binning)
    return roi.x + roi.width <= width and roi.y + roi.height <= height


def limit_frame_covers_sensor(roi: ImagerRoi, sensor_width: int, sensor_height: int, binning: int) -> bool:
    """Whether PHD2 would widen `roi` to the whole sensor.

    True of the full frame itself and of a full frame trimmed by `ImagerRoi` conditioning, such as
    8272x5640@7,1 (MAST_common#17).
    """
    width, height = _binned_frame_size(sensor_width, sensor_height, binning)
    x, y = roi.x // binning, roi.y // binning
    right = min(x + roi.width // binning, width)
    bottom = min(y + roi.height // binning, height)
    return x < PHD2_ROI_ALIGNMENT and y < PHD2_ROI_ALIGNMENT and _round_up(right) >= width and _round_up(bottom) >= height


def derived_guiding_roi(cfg: SpecRoiConfig, sensor_width: int, sensor_height: int, binning: int) -> ImagerRoi:
    """The guiding frame centered on the fiber, as large as the margins and PHD2 allow."""
    usable_x, usable_y = limit_frame_extent(sensor_width, sensor_height, binning)
    half_width = min(cfg.fiber_x - cfg.margin_horizontal, usable_x - cfg.margin_horizontal - cfg.fiber_x)
    half_height = min(cfg.fiber_y - cfg.margin_vertical, usable_y - cfg.margin_vertical - cfg.fiber_y)
    return ImagerRoi.from_other(
        SpecRoi(width=half_width * 2, height=half_height * 2, fiber_x=cfg.fiber_x, fiber_y=cfg.fiber_y)
    )
