from __future__ import annotations

import logging
from enum import Enum, auto
from typing import TYPE_CHECKING, Literal

from common.activities import ImagerActivities, UnitActivities
from common.canonical import CanonicalResponse, CanonicalResponse_Ok
from common.config import ImagerBinningConfig

if TYPE_CHECKING:
    from unit import Unit

from common.mast_logging import init_log
from common.process import WatchedProcess
from imagers import ImagerBinning, ImagerRoi, ImagerSettings

logger = logging.Logger("mast.unit." + __name__)
init_log(logger)

guider_address_port = ("127.0.0.1", 8001)


class GuidingMode(Enum):
    NoGuiding = auto()
    PlateSolving = auto()
    PHD2 = auto()


GuidingModes = Literal["NoGuiding", "PlateSolving", "PHD2"]


class Guider:

    def __init__(self, unit: "Unit"):
        self.unit = unit

        if self.unit.unit_conf.guider.method == "phd2":
            WatchedProcess(
                command="C:/Program Files (x86)/PHDGuiding2/phd2.exe",
                logger=logger,
            ).start()

    def end_guiding(self):
        self.unit.end_activity(UnitActivities.Guiding)
        logger.info("guiding ended")

    def make_guiding_settings(self, base_folder: str | None = None) -> ImagerSettings:
        """
        The 'guiding' camera exposure settings are used:
        -In the second acquisition phase (stage at 'spec' position)
        - While guiding

        :param base_folder:
        :return: camera settings for guiding exposures
        """

        guiding_conf = self.unit.unit_conf.guiding

        h_margin = 1000  # 300  # right and left
        v_margin = 200  # top and bottom

        camera_x_size = self.unit.imager.camera_x_size
        camera_y_size = self.unit.imager.camera_y_size
        if camera_x_size is None or camera_y_size is None:
            raise Exception(f"Cannot make guiding settings - camera {camera_x_size=}, {camera_y_size=}")

        half_width = min(guiding_conf.roi.fiber_x, camera_x_size - guiding_conf.roi.fiber_x) - h_margin
        half_height = min(guiding_conf.roi.fiber_y, camera_y_size - guiding_conf.roi.fiber_y) - v_margin

        guiding_binning: ImagerBinningConfig = guiding_conf.binning
        imager_binning = ImagerBinning(
            x=guiding_binning.x if guiding_binning.x is not None else 1,
            y=guiding_binning.y if guiding_binning.y is not None else 1,
        )

        if guiding_conf.roi:
            imager_roi = ImagerRoi(x=guiding_conf.roi.fiber_x,
                                   y=guiding_conf.roi.fiber_y,
                                   width=guiding_conf.roi.width,
                                   height=guiding_conf.roi.height)
        else:
            imager_roi = ImagerRoi(x=guiding_conf.roi.fiber_x - half_width,
                                   y=guiding_conf.roi.fiber_y - half_height,
                                   width=half_width * 2,
                                   height=half_height * 2)

        return ImagerSettings(
            seconds=guiding_conf.exposure,
            base_folder=base_folder,
            gain=guiding_conf.gain,
            binning=imager_binning,
            roi=imager_roi,
            save=True,
        )

    def stop_acquisition_and_guiding(self):
        """
        Stops the `autoguide` routine
        """
        # if not self.connected:
        #     logger.warning('Cannot stop guiding - not-connected')
        #     return

        if not self.unit.is_active(
            UnitActivities.Acquiring
        ) and not self.unit.is_active(UnitActivities.Guiding):
            error = "not acquiring or guiding"
            logger.error(error)
            return CanonicalResponse(errors=[error])

        self.unit.end_activity(UnitActivities.Acquiring)
        self.unit.end_activity(UnitActivities.Guiding)

        if self.unit.imager.is_active(ImagerActivities.Exposing):
            self.unit.imager.stop_exposure()
            logger.info("stopped exposure")

        if not self.unit.was_tracking_before_guiding:
            self.unit.mount.stop_tracking()
            logger.info("stopped tracking")

        return CanonicalResponse_Ok

    @property
    def is_guiding(self) -> bool:
        if not self.unit.connected:
            return False

        return self.unit.is_active(UnitActivities.Guiding)
