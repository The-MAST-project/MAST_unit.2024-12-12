from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from common.activities import ImagerActivities, UnitActivities
from common.canonical import CanonicalResponse, CanonicalResponse_Ok
from common.config import Config, ImagerBinningConfig
from common.utils import function_name

# from phd2.phd2 import PHD2Connector
from solving_guider import SolvingGuider

if TYPE_CHECKING:
    from unit import Unit

from common.activities import Activities
from common.interfaces.guiding import GuiderInterface, GuiderTypes
from common.interfaces.imager import ImagerBinning, ImagerRoi, ImagerSettings
from common.mast_logging import init_log

logger = logging.Logger("mast.unit." + __name__)
init_log(logger)

guider_address_port = ("127.0.0.1", 8001)


class Guider(GuiderInterface):

    @staticmethod
    def valid_guiding_methods():
        from common.config import Config

        return Config().get_unit().guider.method

    def __init__(self, unit: "Unit" | None, guider_type: str | None = None):  # type: ignore # noqa: UP037
        from phd2.phd2 import PHD2Connector

        self.unit = unit
        self._backend = None
        valid_guiding_methods = Guider.valid_guiding_methods()

        if guider_type is not None:
            if guider_type not in valid_guiding_methods:
                raise ValueError(
                    f"{function_name()}: bad guider_type argument '{guider_type}' "
                    + f"(valid methods={valid_guiding_methods})"
                )
        elif self.unit is not None and self.unit.unit_conf.guider.method is not None:
            if self.unit.unit_conf.guider.method not in valid_guiding_methods:
                raise ValueError(
                    f"{function_name()}: bad guider_type configuration '{self.unit.unit_conf.guider.method} "
                    + f"(valid types={valid_guiding_methods})"
                )
            guider_type = self.unit.unit_conf.guider.method
        else:
            guider_type = Config().get_unit().guider.method

        Activities.__init__(self)
        if guider_type == "phd2":
            self._backend = PHD2Connector(
                parent_imager=self.unit.imager if self.unit else None
            )
            self.guider_type = GuiderTypes.Phd2
        elif guider_type == "solving":
            self._backend = SolvingGuider(self.unit)
            self.guider_type = GuiderTypes.Solving

    def status(self):
        return {
            "type": self.guider_type,
            "activities": self.activities,
            "activities_verbal": self.activities.__repr__(),
            "backend": self._backend.status() if self._backend else None,
        }

    def start_guiding(self):
        if self._backend:
            self._backend.start_guiding()

    def stop_guiding(self):
        if self._backend:
            self._backend.stop_guiding()
        if self.unit:
            self.unit.end_activity(UnitActivities.Guiding)
        logger.info("guiding ended")

    def make_guiding_settings(self, base_folder: str | None = None) -> ImagerSettings:
        """
        The 'guiding' camera exposure settings are used
        - In the second acquisition phase (stage at 'spec' position)
        - While guiding

        :param base_folder:
        :return: camera settings for guiding exposures
        """

        h_margin = 1000  # 300  # right and left
        v_margin = 200  # top and bottom

        if self.unit:
            guiding_conf = self.unit.unit_conf.guiding
            camera_x_size = self.unit.imager.camera_x_size
            camera_y_size = self.unit.imager.camera_y_size
            if camera_x_size is None or camera_y_size is None:
                raise Exception(
                    f"Cannot make guiding settings - camera {camera_x_size=}, {camera_y_size=}"
                )
        else:
            camera_x_size = 8828  # YUCK, YUCK, YUCK
            camera_y_size = 5644
            guiding_conf = Config().get_unit().guiding

        half_width = (
            min(guiding_conf.roi.fiber_x, camera_x_size - guiding_conf.roi.fiber_x)
            - h_margin
        )
        half_height = (
            min(guiding_conf.roi.fiber_y, camera_y_size - guiding_conf.roi.fiber_y)
            - v_margin
        )

        if guiding_conf.roi:
            imager_roi = ImagerRoi(
                x=guiding_conf.roi.fiber_x - half_width,
                y=guiding_conf.roi.fiber_y - half_height,
                width=guiding_conf.roi.width,
                height=guiding_conf.roi.height,
            )
        else:
            imager_roi = ImagerRoi(
                x=guiding_conf.roi.fiber_x - half_width,
                y=guiding_conf.roi.fiber_y - half_height,
                width=half_width * 2,
                height=half_height * 2,
            )

        guiding_binning: ImagerBinningConfig = guiding_conf.binning
        imager_binning = ImagerBinning(
            x=guiding_binning.x if guiding_binning.x is not None else 1,
            y=guiding_binning.y if guiding_binning.y is not None else 1,
        )

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

        if self.unit is not None:
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
        if self._backend:
            return self._backend.is_guiding
        return False


if __name__ == "__main__":
    import sys

    def test_atexit():
        import atexit
        import traceback

        def on_shutdown():
            print("Interpreter is shutting down.")
            traceback.print_stack()

        atexit.register(on_shutdown)

        guider = Guider(unit=None)
        guider.start_guiding()

    def test_make_guiding_settings():
        import json

        guider = Guider(unit=None)
        settings = guider.make_guiding_settings(base_folder="c:/mast/test")
        json.dumps(settings.model_dump(), indent=2)

    test_make_guiding_settings()
    sys.exit(0)
