import datetime
import json
from threading import Event, Lock, Thread
from typing import Any

import numpy as np
import pyzwoasi as zwoasi

import common.asi as asi
from common.activities import ImagerActivities
from common.config import Config
from common.dlipowerswitch import OutletDomain, SwitchedOutlet
from common.interfaces.imager import ImagerExposureSeries, ImagerInterface
from common.mast_logging import get_logger
from common.models.statuses import ImagerExposure, ImagerRoi, ImagerSettings, ImagerStatus
from common.utils import RepeatTimer, function_name, time_stamp
from imagers import Imager

logger = get_logger(__name__)
# if TYPE_CHECKING:
#     from unit import Unit


class ZWOImager(ImagerInterface, SwitchedOutlet):
    """
    ZWOImager is a class that implements the ImagerInterface for ZWO cameras.
    It provides methods to interact with ZWO imaging software (asi SDK).
    """

    _instance = None
    _initialized = False

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(
        self,
        parent_imager: Imager,
        imager_params: dict[str, Any] | None = None,
        _from_imager: bool = False,
    ):

        ImagerInterface.__init__(self)
        SwitchedOutlet.group(
            domain=OutletDomain.UnitOutlets,
            group_name="Camera",
            outlet_names=["Camera", "CameraUSB"],
        ).transfer_attributes(self)
        self.parent_imager = parent_imager
        self.imager_params = imager_params or {}

        self.activities = ImagerActivities(0)
        self.errors: list[str] = []
        self.latest_settings: ImagerSettings | None = None
        self.latest_exposure: ImagerExposure | None = None
        self._image_array: np.ndarray | None = None
        self.image_read_event: Event = Event()
        self.image_saved_event: Event = Event()

        if not self.is_on():
            self.power_on()

        n_cameras = zwoasi.getNumOfConnectedCameras()
        logger.info(f"found {n_cameras} asi camera(s), SDK={zwoasi.getSDKVersion()}")
        self.cam_id = None
        self._connected: bool = False

        self.image_lock = Lock()

        if n_cameras > 0:
            self.cam_id = 0
            self.connected = True
            if self.connected:  # did we succeed to connect?
                self.timer = RepeatTimer(interval=1, function=self.ontimer)
                self.timer.start()

        self.image_was_read: bool = False
        self.image_was_saved = False
        self._setpoint = None

        self._initialized = True

    def __del__(self):
        zwoasi.closeCamera(self.cam_id)

    def save_in_thread(self):
        from imagers.saving import save_to_fits_file

        save_to_fits_file(self)
        self.image_was_saved = True
        self.image_saved_event.set()

        # del self._image_array

    def ontimer(self):
        op = function_name()

        if self.parent_imager and self.parent_imager.unit and self.parent_imager.unit.unit_shutdown_event.is_set():
            self.timer.cancel()
            return

        if not self.connected:
            return

        if self.connected and (self.parent_imager and self.parent_imager.is_active(ImagerActivities.Exposing)):
            assert self.latest_exposure is not None
            assert self.latest_settings is not None
            if (datetime.datetime.now() - self.latest_exposure.start) >= datetime.timedelta(
                seconds=self.latest_settings.seconds / 2
            ):
                self.ccd_temp_at_mid_exposure, _ = zwoasi.getControlValue(self.cam_id, asi.Control.Temperature)
                self.ccd_temp_at_mid_exposure /= 10

            try:
                exposure_status = zwoasi.getExpStatus(self.cam_id)
                # logger.info(f"{op}: {exposure_status=} ({ASIExposureStatus(exposure_status).name})")
                if exposure_status == asi.ExposureStatus.ASI_EXP_FAILED:
                    zwoasi.stopExposure(self.cam_id)
                    self.parent_imager.end_activity(ImagerActivities.Exposing)
                    logger.error(f"exposure failed with exposure_status={asi.ExposureStatus(exposure_status).name}")
                elif exposure_status == asi.ExposureStatus.ASI_EXP_SUCCESS:
                    assert self.latest_settings.roi is not None
                    buffer_size = self.latest_settings.roi.width * self.latest_settings.roi.height
                    if self.latest_settings.format == "raw16":
                        buffer_size *= 2
                        dtype = np.uint16
                    else:
                        dtype = np.uint8

                    self.parent_imager.start_activity(ImagerActivities.ReadingOut)
                    buffer = zwoasi.getDataAfterExp(self.cam_id, bufferSize=buffer_size)
                    assert self.latest_settings and self.latest_settings.roi

                    h = self.latest_settings.roi.height
                    w = self.latest_settings.roi.width
                    img = np.frombuffer(buffer=buffer, dtype=dtype)
                    img = img.reshape((h, w))
                    self.image_array = img

                    self.parent_imager.end_activity(ImagerActivities.ReadingOut)
                    self.image_was_read = True
                    self.image_read_event.set()

                    if self.latest_settings and self.latest_settings.save:
                        Thread(name="zwo-image-saver", target=self.save_in_thread).start()
                    else:
                        self.parent_imager.end_activity(ImagerActivities.Exposing)

            except Exception as ex:
                logger.error(f"{op}: could not get exposure status, {ex=}")

    @property
    def can_image_to_memory(self) -> bool:
        return True

    def capture(self):
        # Implement capture logic for ZWO
        pass

    @property
    def can_send_image_ready_event(self) -> bool:
        return True

    @property
    def can_send_image_saved_event(self) -> bool:
        return True

    def wait_for_image_in_memory(self):
        if self.image_read_event is not None:
            self.image_read_event.wait()

    def wait_for_image_saved(self):
        if not self.image_was_saved:
            self.image_saved_event.wait()
            self.image_saved_event.clear()

    @property
    def temperature(self) -> float:
        self.errors = []
        try:
            val, _ = zwoasi.getControlValue(self.cam_id, asi.Control.Temperature)
        except Exception as ex:
            self.errors.append(f"failed to get control AsiControl.Temperature ({asi.Control.Temperature}), {ex=}")
        return val / 10.0

    def cooler(self, onoff: bool):
        self.errors = []
        try:
            zwoasi.setControlValue(self.cam_id, asi.Control.CoolerOn, onoff, 0)
        except Exception as ex:
            self.errors.append(
                f"failed to set control AsiControl.ASI_COOLER_ON ({asi.Control.CoolerOn}), " + f"value={onoff}, {ex=}"
            )

    @property
    def cooler_power(self) -> float:
        self.errors = []
        try:
            val, _ = zwoasi.getControlValue(self.cam_id, asi.Control.CoolPowerPerc)
        except Exception as ex:
            self.errors.append(
                "failed to get control AsiControl.ASI_COOLER_POWER_PERC " + f"({asi.Control.CoolPowerPerc}), {ex=}"
            )
        return val / 10.0

    def endpoint_startup(self):
        return self.startup()

    def startup(self):
        # self.set_control(asi.Control.ASI_HIGH_SPEED_MODE, 1)
        self.set_control(asi.Control.TargetTemp, -5)
        self.set_control(asi.Control.CoolerOn, True)
        return super().startup()

    def endpoint_shutdown(self):
        return self.shutdown()

    def shutdown(self):
        self.start_activity(ImagerActivities.ShuttingDown)
        self.set_control(asi.Control.TargetTemp, 10)
        self.set_control(asi.Control.CoolerOn, False)
        # del self._image_array
        self.end_activity(ImagerActivities.ShuttingDown)
        return super().shutdown()

    @property
    def is_shutting_down(self) -> bool:
        return self.is_active(ImagerActivities.ShuttingDown)

    def powerdown(self):
        pass

    @property
    def operational(self) -> bool:
        return self._connected

    @property
    def why_not_operational(self) -> list[str]:
        reasons: list[str] = []
        if not self._connected:
            reasons.append("zwo: not connected")
        return reasons

    @property
    def connected(self) -> bool:
        return self._connected

    @connected.setter
    def connected(self, value: bool):
        self.errors = []

        if value:
            self._connected = False
            try:
                info = zwoasi.getCameraProperty(self.cam_id)
                self.width = info.MaxWidth
                self.height = info.MaxHeight
                self.model = info.Name.decode()
                self.depth = info.BitDepth

                zwoasi.openCamera(self.cam_id)
                zwoasi.initCamera(self.cam_id)
                self._connected = True
                self.serial = zwoasi.getSerialNumber(self.cam_id)
                self.pixel_size = info.PixelSize

                logger.info(
                    f"ZWO asi ID={self.cam_id}, SN='{self.serial}', "
                    + f"model='{self.model}', "
                    + f"size={self.width}x{self.height}, "
                    + f"depth={self.depth} bits, pixel-size={self.pixel_size} micron"
                )
            except Exception as ex:
                self.log_and_append(f"could not connect to {self.cam_id=}, {ex=}")
        else:
            try:
                zwoasi.closeCamera(self.cam_id)
                self._connected = False
            except Exception as ex:
                self.log_and_append(f"could not closeCamera {self.cam_id=}, {ex=}")

    @property
    def default_settings(self) -> ImagerSettings:
        """
        Produces default ImagerSettings for the "zwo" imager.
        """

        unit_conf = Config().get_unit()
        assert unit_conf is not None
        imager_conf = unit_conf.imager
        return ImagerSettings(
            seconds=0,
            roi=ImagerRoi(x=0, y=0, width=self.width, height=self.height),
            binning=1,
            base_folder="c:/temp/zwo-images",
            format=imager_conf.format,
            gain=imager_conf.gain,
        )

    def endpoint_abort(self):
        """
        Aborts the current exposure
        """
        return self.abort()

    def abort(self):
        if self.connected and (self.parent_imager and self.parent_imager.is_active(ImagerActivities.Exposing)):
            zwoasi.stopExposure(self.cam_id)

    def endpoint_status(self) -> ImagerStatus:
        """
        Gets the **MAST** imager status
        """
        return self.status()

    def status(self) -> ImagerStatus:
        """
        Gets the **MAST** imager status
        """

        self._setpoint = None
        if self.connected:
            self._setpoint, _ = zwoasi.getControlValue(self.cam_id, asi.Control.TargetTemp)

        return ImagerStatus(
            **self.power_status().model_dump(),
            **self.component_status().model_dump(),
            camera_x_size=self.width,
            camera_y_size=self.height,
            set_point=self._setpoint,
            temperature=self.temperature if self.connected else None,
            cooler_on=self.cooler_on if self.connected else None,
            cooler_power=self.cooler_power if self.connected else None,
            latest_settings=self.latest_settings,
            date=time_stamp(),
        )

    @property
    def set_point(self):
        return self._setpoint

    @property
    def name(self) -> str:
        return "zwo"

    @name.setter
    def name(self, value: str):
        raise NotImplementedError

    @property
    def detected(self) -> bool:
        return self._connected

    @property
    def was_shut_down(self) -> bool:
        return False

    @property
    def camera_x_size(self) -> int | None:
        return self.width

    @property
    def camera_y_size(self) -> int | None:
        return self.height

    def set_format(self, settings: ImagerSettings):
        """
        NOTES:
        - The width and height of the ROI must be divisible by 8 and 2 respectively.
        - The binning must be equal in both dimensions."""
        binning = settings.binning or 1
        if settings.roi:
            x = settings.roi.x
            y = settings.roi.y
            width = settings.roi.width
            height = settings.roi.height
        else:
            x = y = 0
            width = self.width
            height = self.height
        try:
            format: asi.OutputFormat = asi.OutputFormat.from_string(settings.format)
            zwoasi.setROIFormat(
                self.cam_id,
                width=width,
                height=height,
                binning=binning,
                imgType=format.value,
            )
            zwoasi.setStartPos(self.cam_id, x, y)
            logger.info(f"set_format(roi=({x=}, {y=}, {width=}, {height=}), {binning=}, output_format={format.name})")
        except Exception as ex:
            self.log_and_append(f"failed to set format to {x=},{y=},{width=},{height=},{binning=},{format.name=}: {ex=}")

    def start_exposure(self, settings: ImagerSettings):
        self.errors = []
        self.image_was_read = False
        self.image_was_saved = False

        self.set_control(asi.Control.Exposure, int(settings.seconds * 1000000))  # micro seconds

        assert self.default_settings is not None

        if settings.base_folder is None:
            settings.base_folder = self.default_settings.base_folder

        if settings.gain is None:
            settings.gain = self.default_settings.gain
        assert settings.gain is not None
        self.set_control(asi.Control.Gain, settings.gain)

        if settings.roi is None:
            settings.roi = self.default_settings.roi

        zwo_settings = settings.model_copy()
        assert zwo_settings.roi is not None and zwo_settings.binning is not None
        zwo_settings.roi.width //= zwo_settings.binning
        zwo_settings.roi.height //= zwo_settings.binning
        zwo_settings.roi.width -= zwo_settings.roi.width % 8
        zwo_settings.roi.height -= zwo_settings.roi.height % 2
        self.set_format(zwo_settings)

        self.latest_settings = settings.model_copy()

        try:
            self.latest_exposure = ImagerExposure(
                file=settings.image_path,
                seconds=settings.seconds,
                date=datetime.datetime.now(datetime.UTC).isoformat(),
            )

            if self.parent_imager and not self.parent_imager.connected:
                self.parent_imager.connect()
            if self.parent_imager:
                self.parent_imager.start_activity(ImagerActivities.Exposing)
            zwoasi.startExposure(self.cam_id, isDark=False)
            logger.info(f"started a {settings.seconds} seconds exposure")
        except Exception as ex:
            self.log_and_append(f"failed to start exposure, {ex=}")

    def set_control(self, control: asi.Control, value: int):
        try:
            zwoasi.setControlValue(self.cam_id, controlType=control, value=value, auto=0)
            logger.info(f"set_control('{control.name}', value={value})")
        except Exception as ex:
            self.log_and_append(f"failed to set_control('{control.name}' ({control.value}), value={value}), {ex=}")
            raise

    def log_and_append(self, err: str):
        self.errors.append(err)
        logger.error(err)

    def stop_exposure(self):
        zwoasi.stopExposure(self.cam_id)

    def abort_exposure(self):
        zwoasi.stopExposure(self.cam_id)

    def wait_for_image_ready(self):
        if not self.image_was_read:
            self.image_read_event.wait()
            self.image_read_event.clear()

    @property
    def cooler_on(self) -> bool:
        self.errors = []
        try:
            val, _ = zwoasi.getControlValue(self.cam_id, asi.Control.CoolerOn)
        except Exception as ex:
            self.errors.append(f"failed to get control AsiControl.CoolerOn ({asi.Control.CoolerOn}), {ex=}")
        return bool(val)

    @cooler_on.setter
    def cooler_on(self, onoff: bool):
        self.errors = []
        try:
            zwoasi.setControlValue(self.cam_id, asi.Control.CoolerOn, int(onoff), 0)
        except Exception as ex:
            self.errors.append(f"failed to set control AsiControl.CoolerOn ({asi.Control.CoolerOn}, {int(onoff)}), {ex=}")

    @property
    def image_array(self):
        return self._image_array

    @image_array.setter
    def image_array(self, value):
        self._image_array = value

    def start_exposure_series(self, series: ImagerExposureSeries):
        pass

    def end_exposure_series(self, series: ImagerExposureSeries):
        pass


if __name__ == "__main__":

    def test_imager():
        imager = Imager(imager_type="zwo")
        imager.startup()
        series = imager.start_exposure_series(purpose="testing zwo imager")
        imager.start_exposure(ImagerSettings.model_validate({"seconds": 5, "binning": 2}, context={"imager": imager}))
        d = imager.status()
        print(json.dumps(d, indent=2))
        if imager.can_send_image_ready_event:
            imager.wait_for_image_ready()
            logger.info("got image ready event")

        if imager.can_send_image_saved_event:
            imager.wait_for_image_saved()
            logger.info("got image saved event")
        imager.end_exposure_series(series)
        exit(0)

    def test_gain_percent():
        percent = asi.gain_absolute_to_percent(asi.ASI_294MM_DEFAULT_GAIN)
        print("")
        print(f"gain 170: {percent:2.0f}%")
        print(f"gain {percent:2.0f}%: {asi.gain_percent_to_absolute(percent)}")

        percent = asi.gain_absolute_to_percent(asi.ASI_294MM_DEFAULT_GAIN)
        print("")
        print(f"gain 170: {percent:2.0f}%")
        print(f"gain {percent:2.0f}%: {asi.gain_percent_to_absolute(percent)}")

    def test_gain_absolute(percent):
        print(f"gain {percent:2.0f}%: {asi.gain_percent_to_absolute(percent)}")

        percent = asi.gain_absolute_to_percent(asi.ASI_294MM_DEFAULT_GAIN)
        print("")
        print(f"gain 170: {percent:2.0f}%")
        print(f"gain {percent:2.0f}%: {asi.gain_percent_to_absolute(percent)}")

    test_imager()
