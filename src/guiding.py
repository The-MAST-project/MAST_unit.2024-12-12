from __future__ import annotations

import logging
from enum import Enum, auto
from typing import Literal

from common.activities import CameraActivities, UnitActivities
from common.canonical import CanonicalResponse, CanonicalResponse_Ok
from common.imagers import ImagerBinning, ImagerSettings
from common.mast_logging import init_log
from common.process import WatchedProcess
from common.utils import UnitRoi

logger = logging.Logger("mast.unit." + __name__)
init_log(logger)

guider_address_port = ("127.0.0.1", 8001)


class GuidingMode(Enum):
    NoGuiding = auto()
    PlateSolving = auto()
    PHD2 = auto()


GuidingModes = Literal["NoGuiding", "PlateSolving", "PHD2"]


class Guider:
    from unit import Unit

    def __init__(self, unit: Unit):
        from unit import Unit

        self.unit: Unit = unit

        if self.unit.unit_conf["guider"]["method"] == "phd2":
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

        guiding_conf = self.unit.unit_conf["guiding"]

        h_margin = 1000  # 300  # right and left
        v_margin = 200  # top and bottom

        d = guiding_conf["roi"]
        d["sky_x"] = d["fiber_x"]
        d["sky_y"] = d["fiber_y"]
        unit_roi = UnitRoi.from_dict(
            guiding_conf["roi"]
        )  # we use only the center and compute the sizes
        unit_roi.width = (
            min(unit_roi.x, self.unit.camera.cameraXSize - unit_roi.x) - h_margin
        ) * 2
        unit_roi.height = (
            min(unit_roi.y, self.unit.camera.cameraYSize - unit_roi.y) - v_margin
        ) * 2

        x_binning = guiding_conf["binning"]
        binning: ImagerBinning = ImagerBinning(x_binning, x_binning)

        return ImagerSettings(
            seconds=guiding_conf["exposure"],
            base_folder=base_folder,
            gain=guiding_conf["gain"],
            binning=binning,
            roi=unit_roi.to_imager_roi(binning=binning),
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

        if self.unit.camera.is_active(CameraActivities.Exposing):
            self.unit.camera.stop_exposure()
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
