import datetime
import json
import logging
import socket
from enum import IntEnum, auto
from threading import Event, Lock, Thread
from typing import Any

import numpy as np
import pyzwoasi as asi
from astropy.io import fits

import ASI
from common.activities import ImagerActivities

# from common.config import Config
from common.dlipowerswitch import OutletDomain, SwitchedOutlet
from common.interfaces.components import Component
from common.interfaces.imager import (
    ImagerBinning,
    ImagerExposure,
    ImagerExposureSeries,
    ImagerInterface,
    ImagerRoi,
    ImagerSettings,
    ImagerStatus,
)
from common.mast_logging import init_log
from common.utils import RepeatTimer, function_name, time_stamp
from imagers import Imager

logger = logging.Logger("mast-unit-imager-zwo")
init_log(logger)

# if TYPE_CHECKING:
#     from unit import Unit


class ZWOImager(ImagerInterface, SwitchedOutlet):
    """
    ZWOImager is a class that implements the ImagerInterface for ZWO cameras.
    It provides methods to interact with ZWO imaging software (ASI SDK).
    """

    _instance = None
    _initialized = False

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(
        self,
        unit,
        imager_params: dict[str, Any] | None = None,
        _from_imager: bool = False,
    ):

        Component.__init__(self)
        if not _from_imager:
            SwitchedOutlet.group(
                domain=OutletDomain.UnitOutlets,
                group_name="Camera",
                outlet_names=["Camera", "CameraUSB"],
            ).populate(self)
        self.unit = unit
        self.imager_params = imager_params or {}

        self.errors: list[str] = []
        self.latest_settings: ImagerSettings | None = None
        self.latest_exposure: ImagerExposure | None = None
        # self.default_settings: ImagerSettings = ImagerSettings(seconds=0, base_folder='c:/temp')
        self._image_array: np.ndarray | None = None
        self.image_read_event: Event = Event()
        self.image_saved_event: Event = Event()
        self.ccd_temp_at_mid_exposure: float | None = None

        if not self.is_on():
            self.power_on()

        n_cameras = asi.getNumOfConnectedCameras()
        logger.info(f"found {n_cameras} ASI camera(s), SDK={asi.getSDKVersion()}")
        self.cam_id = None
        self._connected: bool = False

        self.image_lock = Lock()

        if n_cameras > 0:
            self.cam_id = 0
            self.connected = True
            if self.connected:  # did we succeed to connect?
                self.timer = RepeatTimer(interval=1, function=self.on_timer)
                self.timer.start()

        self._initialized = True

    def __del__(self):
        asi.closeCamera(self.cam_id)

    def save_in_thread(self):
        from imagers.ascom import save_to_fits_file

        save_to_fits_file(self)
        self.image_was_saved = True
        self.image_read_event.set()

        del self._image_array

    def on_timer(self):
        op = function_name()

        if self.is_active(ImagerActivities.Exposing):
            assert self.latest_exposure is not None
            assert self.latest_settings is not None
            if (
                datetime.datetime.now() - self.latest_exposure.start
            ) >= datetime.timedelta(seconds=self.latest_settings.seconds / 2):
                self.ccd_temp_at_mid_exposure, _ = asi.getControlValue(
                    self.cam_id, ASI.Control.Temperature
                )
                self.ccd_temp_at_mid_exposure /= 10

            try:
                exposure_status = asi.getExpStatus(self.cam_id)
                # logger.info(f"{op}: {exposure_status=} ({ASIExposureStatus(exposure_status).name})")
                if exposure_status == ASI.ExposureStatus.ASI_EXP_FAILED:
                    asi.stopExposure(self.cam_id)
                    self.end_activity(ImagerActivities.Exposing)
                    logger.error(
                        f"exposure failed with exposure_status={ASI.ExposureStatus(exposure_status).name}"
                    )
                elif exposure_status == ASI.ExposureStatus.ASI_EXP_SUCCESS:
                    buffer_size = self.width * self.height
                    if self.output_format == ASI.OutputFormat.RAW16:
                        buffer_size *= 2

                    self.start_activity(ImagerActivities.ReadingOut)
                    buffer = asi.getDataAfterExp(self.cam_id, bufferSize=buffer_size)
                    assert self.latest_settings and self.latest_settings.roi
                    self.image_array = np.ndarray(
                        buffer=buffer,
                        shape=(
                            self.latest_settings.roi.height,
                            self.latest_settings.roi.width,
                        ),
                        dtype=(
                            np.uint16
                            if self.output_format == ASI.OutputFormat.RAW8
                            else np.uint8
                        ),
                    )
                    self.end_activity(ImagerActivities.ReadingOut)
                    self.image_read_event.set()

                    if self.latest_settings and self.latest_settings.save:
                        Thread(
                            name="zwo-image-saver", target=self.save_in_thread
                        ).start()
                    else:
                        self.end_activity(ImagerActivities.Exposing)

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
        if self.image_saved_event is not None:
            self.image_saved_event.wait()
            self.image_saved_event.clear()

    @property
    def temperature(self) -> float:
        self.errors = []
        try:
            val, _ = asi.getControlValue(self.cam_id, ASI.Control.Temperature)
        except Exception as ex:
            self.errors.append(
                f"failed to get control AsiControl.Temperature ({ASI.Control.Temperature}), {ex=}"
            )
        return val / 10.0

    def cooler(self, onoff: bool):
        self.errors = []
        try:
            asi.setControlValue(self.cam_id, ASI.Control.CoolerOn, onoff, 0)
        except Exception as ex:
            self.errors.append(
                f"failed to set control AsiControl.ASI_COOLER_ON ({ASI.Control.CoolerOn}), "
                + f"value={onoff}, {ex=}"
            )

    @property
    def cooler_power(self) -> float:
        self.errors = []
        try:
            val, _ = asi.getControlValue(self.cam_id, ASI.Control.CoolPowerPerc)
        except Exception as ex:
            self.errors.append(
                "failed to get control AsiControl.ASI_COOLER_POWER_PERC "
                + f"({ASI.Control.CoolPowerPerc}), {ex=}"
            )
        return val / 10.0

    def startup(self):
        # self.set_control(AsiControl.ASI_HIGH_SPEED_MODE, 1)
        self.set_control(ASI.Control.TargetTemp, -5)
        self.set_control(ASI.Control.CoolerOn, True)
        return super().startup()

    def shutdown(self):
        self.set_control(ASI.Control.TargetTemp, 10)
        self.set_control(ASI.Control.CoolerOn, False)
        del self._image_array
        return super().shutdown()

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
                info = asi.getCameraProperty(self.cam_id)
                self.width = info.MaxWidth
                self.height = info.MaxHeight
                self.model = info.Name.decode()
                self.depth = info.BitDepth

                asi.openCamera(self.cam_id)
                asi.initCamera(self.cam_id)
                self._connected = True
                self.serial = asi.getSerialNumber(self.cam_id)
                self.pixel_size = info.PixelSize
                if "output_format" in self.imager_params:
                    if int(self.imager_params["output_format"]) not in [
                        ASI.OutputFormat.RAW8,
                        ASI.OutputFormat.RAW16,
                    ]:
                        self.log_and_append(
                            f"invalid {self.imager_params['output_format']=}, must be one of: "
                            + f"{[ASI.OutputFormat.RAW8, ASI.OutputFormat.RAW16]}"
                        )
                    else:
                        self.output_format: ASI.OutputFormat = self.imager_params[
                            "output_format"
                        ]
                else:
                    self.output_format = ASI.OutputFormat.RAW16

                # imager_conf = Config().get_unit().imager if not self.unit else self.unit.unit_conf.imager
                # The imager configuration in MongoDB is a bit muddy :-()

                # self.default_settings = ImagerSettings(
                #     seconds=0,
                #     roi=ImagerRoi(x=0, y=0, width=self.width, height=self.height),
                #     binning=ImagerBinning(x=1, y=1),
                #     gain=85,
                #     base_folder='c:/temp/zwo-images'
                # )

                logger.info(
                    f"ZWO ASI ID={self.cam_id}, SN='{self.serial}', "
                    + f"model='{self.model}', "
                    + f"size={self.width}x{self.height}, "
                    + f"depth={self.depth} bits, pixel-size={self.pixel_size} micron, "
                    + f"output_format={self.output_format.name}"
                )
            except Exception as ex:
                self.log_and_append(f"could not connect to {self.cam_id=}, {ex=}")
        else:
            try:
                asi.closeCamera(self.cam_id)
                self._connected = False
            except Exception as ex:
                self.log_and_append(f"could not closeCamera {self.cam_id=}, {ex=}")

    @property
    def default_settings(self) -> ImagerSettings:
        """
        Produces default ImagerSettings for the "zwo" imager.

        TODO: Should somehow use values from the Config()
        """
        return ImagerSettings(
            seconds=0,
            roi=ImagerRoi(x=0, y=0, width=self.width, height=self.height),
            binning=ImagerBinning(x=1, y=1),
            gain=85,
            base_folder="c:/temp/zwo-images",
        )

    def abort(self):
        if self.is_active(ImagerActivities.Exposing):
            asi.stopExposure(self.cam_id)

    def status(self) -> ImagerStatus:
        """
        Gets the **MAST** imager status
        """

        target_temp, _ = asi.getControlValue(self.cam_id, ASI.Control.TargetTemp)
        return ImagerStatus(
            **self.power_status().model_dump(),
            **self.component_status().model_dump(),
            set_point=target_temp,
            temperature=self.temperature,
            cooler=self.cooler_on,
            cooler_power=self.cooler_power,
            latest_exposure=self.latest_exposure,
            date=time_stamp(),
        )

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
        if settings.binning:
            binning = settings.binning
            if binning.x != binning.y:
                raise ValueError(
                    f"bad binning={binning.x}x{binning.y}, horizontal and vertical must be equal"
                )
        binning = settings.binning.x if settings.binning else 1
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
            format: ASI.OutputFormat = self.output_format
            if width % 8 != 0:
                logger.warning(
                    f"aligning roi width to 8 (subtracting {width % 8} bits)"
                )
                width -= width % 8
            if height % 2 != 0:
                logger.warning(
                    f"aligning roi height to 2 (subtracting {height % 2} bits)"
                )
                height -= height % 8
            asi.setROIFormat(
                self.cam_id,
                width=width,
                height=height,
                binning=binning,
                imgType=format.value,
            )
            asi.setStartPos(self.cam_id, x, y)
            logger.info(
                f"set_format(roi=({x=}, {y=}, {width=}, {height=}), {binning=}, output_format={format.name})"
            )
        except Exception as ex:
            self.log_and_append(
                f"failed to set format to {x=},{y=},{width=},{height=},{binning=},{format.name=}: {ex=}"
            )

    def start_exposure(self, settings: ImagerSettings):
        self.errors = []
        self.set_control(
            ASI.Control.Exposure, int(settings.seconds * 1000000)
        )  # micro seconds

        assert self.default_settings is not None

        if settings.base_folder is None:
            settings.base_folder = self.default_settings.base_folder

        if settings.gain is None:
            settings.gain = self.default_settings.gain
        assert settings.gain is not None
        self.set_control(ASI.Control.Gain, settings.gain)

        if settings.roi is None:
            settings.roi = self.default_settings.roi
        self.set_format(settings)

        self.latest_settings = settings.model_copy()

        try:
            self.latest_exposure = ImagerExposure(
                file=settings.image_path,
                seconds=settings.seconds,
                date=datetime.datetime.now(datetime.UTC).isoformat(),
            )
            self.start_activity(ImagerActivities.Exposing)
            asi.startExposure(self.cam_id, isDark=False)
            logger.info(f"started a {settings.seconds} seconds exposure")
        except Exception as ex:
            self.log_and_append(f"failed to start exposure, {ex=}")

    def set_control(self, control: ASI.Control, value: int):
        try:
            asi.setControlValue(self.cam_id, controlType=control, value=value, auto=0)
            logger.info(f"set_control('{control.name}', value={value})")
        except Exception as ex:
            self.log_and_append(
                f"failed to set_control('{control.name}' ({control.value}), value={value}), {ex=}"
            )
            raise

    def log_and_append(self, err: str):
        self.errors.append(err)
        logger.error(err)

    def stop_exposure(self):
        asi.stopExposure(self.cam_id)

    def abort_exposure(self):
        asi.stopExposure(self.cam_id)

    def wait_for_image_ready(self):
        self.image_read_event.wait()
        self.image_read_event.clear()

    @property
    def cooler_on(self) -> bool:
        self.errors = []
        try:
            val, _ = asi.getControlValue(self.cam_id, ASI.Control.CoolerOn)
        except Exception as ex:
            self.errors.append(
                f"failed to get control AsiControl.CoolerOn ({ASI.Control.CoolerOn}), {ex=}"
            )
        return bool(val)

    @cooler_on.setter
    def cooler_on(self, onoff: bool):
        self.errors = []
        try:
            asi.setControlValue(self.cam_id, ASI.Control.CoolerOn, int(onoff), 0)
        except Exception as ex:
            self.errors.append(
                f"failed to set control AsiControl.CoolerOn ({ASI.Control.CoolerOn}, {int(onoff)}), {ex=}"
            )

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
        imager = Imager(
            unit=None,
            imager_type="zwo",
            params={"output_format": ASI.OutputFormat.RAW16},
        )
        imager.startup()
        series = imager.start_exposure_series(purpose="testing zwo imager")
        imager.start_exposure(
            ImagerSettings.model_validate({"seconds": 5}, context={"imager": imager})
        )
        print(json.dumps(imager.status().model_dump(), indent=2))
        if imager.can_send_image_ready_event:
            imager.wait_for_image_ready()
            logger.info("got image ready event")

        if imager.can_send_image_saved_event:
            imager.wait_for_image_saved()
            logger.info("got image saved event")
        imager.end_exposure_series(series)
        exit(0)

    def test_gain_percent():
        percent = ASI.gain_absolute_to_percent(170)
        print("")
        print(f"gain 170: {percent:2.0f}%")
        print(f"gain {percent:2.0f}%: {ASI.gain_percent_to_absolute(percent)}")

        percent = ASI.gain_absolute_to_percent(170)
        print("")
        print(f"gain 170: {percent:2.0f}%")
        print(f"gain {percent:2.0f}%: {ASI.gain_percent_to_absolute(percent)}")

    test_gain_percent()
