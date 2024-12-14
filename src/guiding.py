import logging
from common.utils import CanonicalResponse, CanonicalResponse_Ok
from common.mast_logging import init_log
from common.activities import UnitActivities
from common.utils import UnitRoi
from camera import CameraSettings, CameraBinning

logger = logging.Logger('mast.unit.' + __name__)
init_log(logger)

guider_address_port = ('127.0.0.1', 8001)


class Guider:

    def __init__(self, unit: 'Unit'):
        self.unit: 'Unit' = unit

    def end_guiding(self):
        self.unit.end_activity(UnitActivities.Guiding)
        logger.info(f'guiding ended')

    def make_guiding_settings(self, base_folder: str | None = None) -> CameraSettings:
        """
        The 'guiding' camera exposure settings are used:
        -In the second acquisition phase (stage at 'spec' position)
        - While guiding

        :param base_folder:
        :return: camera settings for guiding exposures
        """

        guiding_conf = self.unit.unit_conf['guiding']

        h_margin = 300  # right and left
        v_margin = 200  # top and bottom

        unit_roi = UnitRoi.from_dict(guiding_conf['roi'])  # we use only the center and compute the sizes
        unit_roi.width = (min(unit_roi.fiber_x, self.unit.camera.cameraXSize - unit_roi.fiber_x) - h_margin) * 2
        unit_roi.height = (min(unit_roi.fiber_y, self.unit.camera.cameraYSize - unit_roi.fiber_y) - v_margin) * 2

        x_binning = guiding_conf['binning']
        binning: CameraBinning = CameraBinning(x_binning, x_binning)

        return CameraSettings(
            seconds=guiding_conf['exposure'],
            base_folder=base_folder,
            gain=guiding_conf['gain'],
            binning=binning,
            roi=unit_roi.to_camera_roi(binning=binning),
            save=True
        )

    def stop_acquisition_and_guiding(self):
        """
        Stops the ``autoguide`` routine

        :mastapi:
        """
        # if not self.connected:
        #     logger.warning('Cannot stop guiding - not-connected')
        #     return

        if not self.unit.is_active(UnitActivities.Acquiring) and not not self.unit.is_active(UnitActivities.Guiding):
            error = "not acquiring or guiding"
            logger.error(error)
            return CanonicalResponse(errors=[error])

        self.unit.end_activity(UnitActivities.Acquiring)
        self.unit.end_activity(UnitActivities.Guiding)

        if not self.unit.was_tracking_before_guiding:
            self.unit.mount.stop_tracking()
            logger.info('stopped tracking')

        return CanonicalResponse_Ok

    @property
    def is_guiding(self) -> bool:
        if not self.unit.connected:
            return False

        return self.unit.is_active(UnitActivities.Guiding)
