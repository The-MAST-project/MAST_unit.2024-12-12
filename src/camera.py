import datetime
import logging
import os
import socket
import threading
import time
from enum import IntFlag
from logging import Logger
from threading import Lock, Thread
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np
import win32com.client
from astropy.io import fits
from fastapi.routing import APIRouter
from pydantic import BaseModel

from common.activities import CameraActivities
from common.ascom import AscomDispatcher, AscomStatus, ascom_run
from common.camera import CameraBinning, CameraRoi
from common.components import Component, ComponentStatus
from common.config import Config
from common.dlipowerswitch import OutletDomain, PowerStatus, SwitchedOutlet
from common.mast_logging import init_log
from common.paths import PathMaker
from common.utils import (
    BASE_UNIT_PATH,
    CanonicalResponse,
    CanonicalResponse_Ok,
    RepeatTimer,
    function_name,
    time_stamp,
)

logger = logging.getLogger("mast.unit." + __name__)
init_log(logger)


class Visualizer:
    def __init__(self, name: str, func: Callable):
        self.name = name
        self.func = func


class AscomCameraState(IntFlag):
    """
    Camera states as per https://ascom-standards.org/Help/Developer/html/T_ASCOM_DeviceInterface_CameraStates.htm
    """

    Idle = 0
    Waiting = 1
    Exposing = 2
    Reading = 3
    Download = 4
    Error = 5


class CameraSettings:
    """

    Multipurpose camera exposure context

    Callers to start_exposure() fill in:
    - seconds - duration in seconds
    - base_folder - [optional] supplied folder under which the new folder/file will reside
    - gain - to be applied to the camera by start_exposure()
    - binning - ditto
    - roi - ditto
    - tags - a flat dictionary of tags, will be added to the file name as ',name=value' or
       just ',name' if the value is None
    - save - whether to save to file or just keep in memory
    - fits_cards - to be added to the default ones

    After start_exposure() is called:
    - image_path - contains the full path to the saved file, with a standard combination of the context elements
               <folder>/seq=<sequence>,tags=<tag1=value1,tag2,tag3=value3>,binning=<binning>,gain=<gain>,roi=<roi>
    - start - contains the exposure start time

    Note:
     start_exposure() will copy the context to camera.latest_settings thus making it available for further use

    """

    def __init__(
        self,
        seconds: float,
        gain: Optional[float] = None,
        binning: Optional[CameraBinning] = None,
        roi: Optional[CameraRoi] = None,
        tags: Optional[Dict] = None,
        save: bool = True,
        fits_cards: Optional[Dict[str, Tuple]] = None,
        base_folder: Optional[str] = None,
        image_path: Optional[str] = None,
    ):

        self.seconds: float = seconds
        self.base_folder: str | None = base_folder
        self.image_path: str | None = image_path
        self.binning: CameraBinning | None = binning
        self.gain: float | None = gain
        self.roi: CameraRoi | None = roi
        self.tags: dict | None = tags if tags else {}
        self.save: bool = save
        self.fits_cards: Optional[Dict[str, Tuple]] = fits_cards if fits_cards else {}
        self.start: datetime.datetime = datetime.datetime.now()
        self.file_name_parts: List[str] = []

        if self.save:
            if self.image_path is not None:
                os.makedirs(os.path.dirname(self.image_path), exist_ok=True)
            elif self.base_folder is not None:
                self.folder = self.base_folder
                os.makedirs(self.folder, exist_ok=True)
                self.make_file_name()
            else:
                raise Exception(
                    f"CameraSettings:__init__(): either 'image_path' or 'base_folder' MUST be supplied"
                )

    def make_file_name(self, additional_tags: dict | None = None):
        """
        Makes the file part of the image path.  This will:
        - generate current seq= and time= file name parts
        - prepend optional additional_tags to those passed to the constructor

        :param additional_tags: tags specific to THIS making of the file name
        :return:
        """
        self.file_name_parts = []
        self.file_name_parts.append(
            f"seq={PathMaker().make_seq(self.folder, start_with=-1)}"
        )
        self.file_name_parts.append(f"time={PathMaker().current_utc()}")

        tags = {}
        if additional_tags:
            tags = additional_tags
        if self.tags:
            tags.update(self.tags)
        for k, v in tags.items():
            self.file_name_parts.append(f"{k}" if v is None else f"{k}={v}")

        self.file_name_parts.append(f"seconds={self.seconds}")
        self.file_name_parts.append(f"binning={self.binning}")
        self.file_name_parts.append(f"gain={self.gain}")
        self.file_name_parts.append(f"roi={self.roi}")

        self.image_path = os.path.join(
            self.folder, ",".join(self.file_name_parts) + ".fits"
        )


class ExposureModel(BaseModel):
    file: Optional[str] = None
    seconds: Optional[float] = None
    date: Optional[datetime.datetime] = None


class CameraStatus(PowerStatus, AscomStatus, ComponentStatus):
    errors: Optional[List[str]] = None
    set_point: Optional[float] = None
    temperature: Optional[float] = None
    cooler: bool = False
    cooler_power: Optional[float] = None
    latest_exposure: Optional[ExposureModel] = None
    date: Optional[str] = None


class Camera(Component, SwitchedOutlet, AscomDispatcher):
    _instance = None
    _initialized = False

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(Camera, cls).__new__(cls)
        return cls._instance

    @property
    def full_frame_roi(self) -> CameraRoi:
        return CameraRoi(0, 0, self.cameraXSize, self.cameraYSize)

    @property
    def logger(self) -> Logger:
        return logger

    @property
    def ascom(self) -> win32com.client.Dispatch:
        return self._ascom

    def __init__(self, unit: "Unit"):  # type: ignore[name]
        #
        # The Camera() is a Singleton but the initiator is called twice (for the same object ID):
        # - once from this file, with unit as None
        # - one from the unit, with unit as the main Unit object
        #
        unit_id = None
        if unit:
            unit_id = f"0x{id(unit):X}"
        logger.info(f"camera.id: 0x{id(self):X}, unit: {unit_id}")

        self.unit: "Unit" = unit  # type: ignore[name]

        if self._initialized:
            return

        self.defaults = {
            "temp_check_interval": 15,
        }

        self.unit_conf = Config().get_unit()
        self.conf = self.unit_conf["camera"]
        Component.__init__(self)
        SwitchedOutlet.__init__(self, OutletDomain.Unit, outlet_name="Camera")

        try:
            if not self.is_on():
                self.power_on()

            self._ascom = win32com.client.Dispatch(self.conf["ascom_driver"])
        except Exception as ex:
            logger.exception(ex)
            raise ex

        self.latest_settings: None | CameraSettings = None
        self.latest_temperature_check: datetime.datetime | None = None
        self.temp_check_interval = (
            self.conf["temp_check_interval"]
            if "temp_check_interval" in self.conf
            else self.defaults["temp_check_interval"]
        )

        self._is_exposing: bool = False
        self.operational_set_point: float = -25
        self.warm_set_point: float = (
            5  # temperature at which the camera is considered warm
        )
        self._image_width: int | None = None
        self._image_height: int | None = None
        self.PixelSizeX: int | None = None
        self.PixelSizeY: int | None = None
        self.maxBinX: int | None = None
        self.maxBinY: int | None = None
        self.cameraXSize: int | None = None
        self.cameraYSize: int | None = None
        self.GainMin: float | None = None
        self.GainMax: float | None = None
        self.image: np.ndarray | None = None
        self.last_state: AscomCameraState = AscomCameraState.Idle
        self.errors: List[str] = []
        self.expected_mid_exposure: datetime.datetime | None = None
        self.ccd_temp_at_mid_exposure: float | None = None
        self._binning: CameraBinning = CameraBinning(1, 1)
        self._roi: CameraRoi | None = None
        self._gain: int | None = None

        self._was_shut_down: bool = False

        self.timer: RepeatTimer = RepeatTimer(1, function=self.ontimer)
        self.timer.name = "camera-timer-thread"
        self.timer.start()

        self._detected = False
        self.image_lock: Lock = Lock()
        self.image_was_read: bool = False
        self.image_was_saved: bool = False

        self.visualizers: List[Visualizer] = []

        self.image_ready_event: threading.Event = threading.Event()
        self.image_saved_event: threading.Event = threading.Event()

        self.guiding_roi_width: int | None = None
        self.guiding_roi_height: int | None = None

        self._initialized = True
        # logger.info('initialized')

    @property
    def binning(self):
        return self._binning

    @binning.setter
    def binning(self, value: CameraBinning):
        if 1 > value.x > self.maxBinX:
            raise Exception(f"bad {value.x=}, must be > 1 and < {self.maxBinX=}")
        if 1 > value.y > self.maxBinY:
            raise Exception(f"bad {value.y=}, must be > 1 and < {self.maxBinY=}")

        current_binning = self._binning
        response_x = ascom_run(self, f"BinX = {value.x}")
        response_y = ascom_run(self, f"BinY = {value.y}")
        if response_x.failed or response_y.failed:
            ascom_run(self, f"BinX = {current_binning.x}")
            ascom_run(self, f"BinY = {current_binning.y}")
            raise Exception(f"failures: {response_x.failure=}, {response_y.failure=}")
        self._binning = value

    @property
    def roi(self) -> CameraRoi:
        return self._roi

    @roi.setter
    def roi(self, value: CameraRoi):
        if 0 > value.startX > self.cameraXSize:
            raise Exception(
                f"bad {value.startX=}, must be 0 > startX > {self.cameraXSize=}"
            )
        if 0 > value.startY > self.cameraYSize:
            raise Exception(
                f"bad {value.startY=}, must be 0 > startY > {self.cameraYSize=}"
            )
        if 0 > value.numX > self.cameraXSize:
            raise Exception(
                f"bad {value.numX=}, must be 0 > width > {self.cameraXSize=}"
            )
        if 0 > value.numY > self.cameraYSize:
            raise Exception(
                f"bad {value.numY=}, must be 0 > height > {self.cameraYSize=}"
            )
        if value.startX + value.numX > self.cameraXSize:
            raise Exception(
                f"{value.startX=} + {value.numX=} exceeds {self.cameraXSize=}"
            )
        if value.startY + value.numY > self.cameraYSize:
            raise Exception(
                f"{value.startY=} + {value.numY=} exceeds {self.cameraYSize=}"
            )

        response_x = ascom_run(self, f"StartX ={value.startX}")
        response_y = ascom_run(self, f"StartY = {value.startY}")
        response_width = ascom_run(self, f"NumX = {value.numX}")
        response_height = ascom_run(self, f"NumY = {value.numY}")

        if (
            response_x.failed
            or response_y.failed
            or response_height.failed
            or response_width.failed
        ):
            ascom_run(self, f"StartX = {self._roi.startX}")
            ascom_run(self, f"StartY = {self._roi.startY}")
            ascom_run(self, f"NumX = {self._roi.numX}")
            ascom_run(self, f"NumY = {self._roi.numY}")

            raise Exception(
                f"errors: {response_x.failure=}, {response_y.failure=}, "
                + f"{response_width.failure=}, {response_height.failure=}"
            )
        else:
            self._roi = CameraRoi(value.startX, value.startY, value.numX, value.numY)

    @property
    def connected(self) -> bool:
        if not self.is_on() or not self._ascom:
            return False
        response = ascom_run(self, "Connected", no_entry_log=True)
        return response.value if response.succeeded else False

    @connected.setter
    def connected(self, value: bool):
        if not self.is_on() or not self._ascom:
            return

        response = ascom_run(self, f"Connected = {value}")
        if response.succeeded:
            if value:
                response = ascom_run(self, "PixelSizeX")
                if response.succeeded:
                    self.PixelSizeX = response.value
                    self._detected = True

                response = ascom_run(self, "PixelSizeY")
                if response.succeeded:
                    self.PixelSizeY = response.value

                response = ascom_run(self, "MaxBinX")
                if response.succeeded:
                    self.maxBinX = int(response.value)

                response = ascom_run(self, "MaxBinY")
                if response.succeeded:
                    self.maxBinY = int(response.value)

                response = ascom_run(self, "CameraXSize")
                if response.succeeded:
                    self.cameraXSize = response.value

                response = ascom_run(self, "CameraYSize")
                if response.succeeded:
                    self.cameraYSize = response.value

                response = ascom_run(self, "GainMin")
                if response.succeeded:
                    self.GainMin = response.value

                response = ascom_run(self, "GainMax")
                if response.succeeded:
                    self.GainMax = response.value

                a = self.ascom_status()
                logger.info(
                    f"Camera: {a.ascom.name}, {a.ascom.description}, "
                    + f"{self.cameraXSize}x{self.cameraYSize}"
                    + f" driver: '{self.conf['ascom_driver']}'"
                )

                self.guiding_roi_width = int((self.cameraXSize / 100) * 90)
                self.guiding_roi_height = int((self.cameraYSize / 100) * 80)
        else:
            logger.info(f"failed connected = {value} (failure='{response.failure}')")
        self._detected = value

    @property
    def gain(self) -> int | None:
        response = ascom_run(self, "Gain")
        if response.succeeded:
            self._gain = response.value
            return self._gain
        else:
            return None

    @gain.setter
    def gain(self, value: int):
        if not self.connected:
            raise Exception(f"cannot set gain, not connected")

        if self.GainMin is not None and self.GainMax is not None:
            if self.GainMin > value > self.GainMax:
                logger.error(
                    f"Exception({value=} out of bounds [{self.GainMin=}, {self.GainMax=}]"
                )
                return

        response = ascom_run(self, f"Gain = {value}")
        if response.failed:
            logger.error(
                f"Exception(failed to set Gain to {value}, error(s): {response.failure}"
            )
            return
        self._gain = value

    def connect(self):
        """
        Connects to the **MAST** camera

        :mastapi:
        Returns
        -------

        """
        self.connected = True
        return CanonicalResponse_Ok

    def disconnect(self):
        """
        Disconnects from the **MAST* camera

        :mastapi:
        """
        self.connected = False
        return CanonicalResponse_Ok

    def endpoint_start_exposure(
        self,
        seconds: Optional[float] = 5,
        gain: Optional[float] = 170,
        binning: Optional[int] = 1,
        center_x: Optional[int] = None,
        center_y: Optional[int] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
    ):

        # if isinstance(binning, str):
        #     binning = int(binning)
        if all([center_x, center_y, width, height]):
            roi = CameraRoi(center_x, center_y, width, height)
        else:
            roi = CameraRoi(0, 0, self.cameraXSize, self.cameraYSize)
        context = CameraSettings(
            seconds=float(seconds) if isinstance(seconds, str) else seconds,
            base_folder=PathMaker().make_exposures_folder(),
            gain=int(gain) if isinstance(gain, str) else gain,
            binning=CameraBinning(binning, binning),
            roi=roi,
            tags=None,
            save=True,
        )

        # self.do_start_exposure(purpose=ExposurePurpose.Exposure, tags=None, seconds=seconds, gain=gain,
        #   binning=binning, save=True)
        self.do_start_exposure(context)

    def do_start_exposure(self, settings: CameraSettings) -> CanonicalResponse:
        """
        Starts a *MAST* camera exposure

        Parameters
        ----------
        settings

        :mastapi:
        """
        op = function_name()
        self.errors = []

        if not self._ascom:
            self.errors.append(f"{op}: no ASCOM handle")

        if not self.connected:
            self.errors.append(f"{op}: not connected")

        if len(self.errors) > 0:
            return CanonicalResponse(errors=self.errors)

        if self.is_active(CameraActivities.Exposing):
            logger.info(f"{op}: already exposing")
            return CanonicalResponse(errors=[f"already exposing"])

        self.errors = []

        try:
            if settings.gain:
                self.gain = settings.gain

            if settings.binning:
                self.binning = settings.binning

            if settings.roi:
                self.roi = settings.roi

        except Exception as e:
            self.errors.append(f"{e}")

        if len(self.errors) > 0:
            logger.error(f"{op}: {self.errors=}")
            return CanonicalResponse(errors=self.errors)

        # folder = None
        # if settings.save:
        #     # this is the tricky part: a general purpose file name maker
        #     if settings.purpose == ExposurePurpose.Acquisition:
        #         folder = PathMaker().make_acquisition_folder()
        #     elif settings.purpose == ExposurePurpose.Exposure:
        #         folder = PathMaker().make_exposure_file_name()
        #     elif settings.purpose == ExposurePurpose.Guiding:
        #         if settings.base_folder is None:
        #             raise Exception(f"for {settings.purpose=} context.folder cannot be None")
        #         folder = os.path.join(settings.base_folder, 'guiding')
        #     os.makedirs(folder, exist_ok=True)
        #
        #     file = f"seq={PathMaker().make_seq(folder)}," + settings.make_filename()
        #
        #     settings.image_path = os.path.join(folder, file)
        #     logger.info(f"{op}: {settings.image_path=}")
        # else:
        #     logger.info('{op}: image will not be saved to a file')

        response = ascom_run(self, f"StartExposure({settings.seconds}, True)")
        if response.value is None:
            self.start_activity(CameraActivities.Exposing)
            self.expected_mid_exposure = datetime.datetime.now() + datetime.timedelta(
                seconds=settings.seconds / 2
            )
            self.image = None
            self.image_was_read = False
            self.image_was_saved = False
            self.latest_settings = settings
            self.latest_settings.fits_cards["UT-START"] = (
                datetime.datetime.now(datetime.UTC).isoformat(),
                "UT start of exposure",
            )

            if settings.save:
                self.image_saved_event.wait()
                self.image_saved_event.clear()
            else:
                self.image_ready_event.wait()
                self.image_ready_event.clear()

            self.end_activity(CameraActivities.Exposing)
        else:
            if response.is_exception:
                self.errors.append(response.exception)
            if response.is_error:
                self.errors.append(response.errors)

        return (
            CanonicalResponse(errors=self.errors)
            if self.errors
            else CanonicalResponse_Ok
        )

    def abort_exposure(self):
        """
        Aborts the current **MAST** camera exposure. No image readout.

        :mastapi:
        """
        self.errors = []
        if not self.connected:
            self.errors.append("not connected")
            return
        if not self.is_active(CameraActivities.Exposing):
            self.errors.append("not exposing")

        response = ascom_run(self, "CanAbortExposure")
        if response.succeeded and response.value:
            response = ascom_run(self, "AbortExposure()")
            if response.failed:
                self.errors.append(f"failed to abort (failure='{response.failure}')")
        self.end_activity(CameraActivities.Exposing)
        return (
            CanonicalResponse(errors=self.errors)
            if self.errors
            else CanonicalResponse_Ok
        )

    def stop_exposure(self):
        """
        Stops the current **MAST** camera exposure.  An image readout is initiated

        :mastapi:
        """
        self.errors = []
        if not self.connected:
            self.errors.append("not connected")
            return CanonicalResponse(errors=["not connected"])

        if not self.is_active(CameraActivities.Exposing):
            self.errors.append("not exposing")
            return CanonicalResponse(errors=["not connected"])

        response = ascom_run(self, "StopExposure()")  # the timer will read the image
        if response.failed:
            self.errors.append(
                f"could not StopExposure(), (failure='{response.failure}')"
            )
        self.end_activity(CameraActivities.Exposing)

        return (
            CanonicalResponse(errors=self.errors)
            if self.errors
            else CanonicalResponse_Ok
        )

    def status(self) -> CameraStatus:
        """
        Gets the **MAST** camera status

        :mastapi:
        Returns
        -------

        """

        return CameraStatus(
            **self.power_status().dict(),
            **self.ascom_status().dict(),
            **self.component_status().dict(),
            set_point=self.operational_set_point,
            temperature=self._ascom.CCDTemperature,
            cooler=self._ascom.CoolerOn,
            cooler_power=self._ascom.CoolerPower,
            latest_exposure=(
                ExposureModel(
                    file=self.latest_settings.base_folder,
                    seconds=self.latest_settings.seconds,
                    date=self.latest_settings.start,
                )
                if self.latest_settings
                else None
            ),
            date=time_stamp(),
        )

    def startup(self):
        """
        Starts the **MAST** camera up (cooling down , if needed)

        :mastapi:

        """
        # self.start_activity(CameraActivities.StartingUp)
        self.errors = []
        self.power_on()
        self.connect()
        self.cooler_on()
        return (
            CanonicalResponse(errors=self.errors)
            if self.errors
            else CanonicalResponse_Ok
        )

    def cooldown(self):
        if not self.connected:
            return

        response = ascom_run(self, "CanSetCCDTemperature")
        if response.succeeded and response.value:
            self.start_activity(CameraActivities.CoolingDown)
            response = ascom_run(self, "CanSetCCDTemperature")
            if response.succeeded:
                logger.info(
                    f"cool-down: setting set-point to {self.operational_set_point:.1f}"
                )
                response = ascom_run(
                    self, f"SetCCDTemperature = {self.operational_set_point}"
                )
                if response.failed:
                    logger.error(
                        f"failed to set set-point (failure='{response.failure}')"
                    )

            response = ascom_run(self, "CoolerOn")
            if response.succeeded and not response.value:
                return self.cooler_on()
        return CanonicalResponse_Ok

    def shutdown(self):
        """
        Shuts the **MAST** camera down (warms up, if needed)

        :mastapi:
        """
        # if self.connected:
        #     self.start_activity(CameraActivities.ShuttingDown)
        #     if abs(ascom_run(self, 'CCDTemperature') - self.warm_set_point) > 0.5:
        #         self.warmup()
        # else:
        #     self.power_off()
        self.errors = []
        if self.connected:
            response = ascom_run(self, "CoolerOn = False")
            if response.failed:
                self.errors.append(
                    f"could not set CoolerOn to False (failure='{response.failure}'"
                )
            else:
                time.sleep(2)
        self.power_off()
        self._was_shut_down = True
        return (
            CanonicalResponse(errors=self.errors)
            if self.errors
            else CanonicalResponse_Ok
        )

    def warmup(self):
        """
        Warms the **MAST** camera up, to prevent temperature shock
        """
        if not self.connected:
            return

        response = ascom_run(self, "CanSetCCDTemperature")
        if response.succeeded and response.value:
            self.start_activity(CameraActivities.WarmingUp)
            response = ascom_run(self, "CCDTemperature")
            temp = None
            if response.succeeded:
                temp = response.value

            response = ascom_run(self, f"SetCCDTemperature({self.warm_set_point})")
            if response.succeeded:
                message = "warm-up started:"
                if temp:
                    message = message + f" current temp: {temp:.1f},"
                logger.info(f"{message} setting set-point to {self.warm_set_point:.1f}")
            else:
                logger.error(f"could not set warm point (failure='{response.failure}')")

    def abort(self):
        """
        :mastapi:
        Returns
        -------

        """
        self.abort_exposure()
        return CanonicalResponse_Ok

    def ontimer(self):
        """
        Called by timer, checks if any ongoing activities have changed state
        """
        if not self.connected:
            return

        response = ascom_run(self, "CameraState")
        if response.succeeded:
            current_state = response.value
        else:
            return

        now = datetime.datetime.now()
        # previous_state = self.last_state
        if self.last_state is None:
            self.last_state = current_state
            logger.info(
                f"state changed from None to {AscomCameraState(self.last_state).__repr__()}"
            )
        else:
            if not current_state == self.last_state:
                # percent = ''
                # if (current_state == AscomCameraState.Exposing or current_state == AscomCameraState.Waiting or
                #         current_state == AscomCameraState.Reading or current_state == AscomCameraState.Download):
                #     response = ascom_run(self, 'PercentCompleted')
                #     percent = f"{response.value} %" if response.succeeded else ''
                logger.info(
                    f"state changed from {AscomCameraState(self.last_state).__repr__()} to "
                    + f"{AscomCameraState(current_state).__repr__()}"
                )
                self.last_state = current_state

        if (
            current_state == AscomCameraState.Exposing
            and self.expected_mid_exposure is not None
            and now >= self.expected_mid_exposure
        ):
            response = ascom_run(self, "CCDTemperature")
            if response.succeeded:
                self.ccd_temp_at_mid_exposure = response.value
                self.expected_mid_exposure = None

        # logger.info(f"is_active(CameraActivities.Exposing)={self.is_active(CameraActivities.Exposing)}, {current_state=}")
        if (
            self.is_active(CameraActivities.Exposing)
            and current_state == AscomCameraState.Idle
        ):
            if (
                not self.image_lock.locked()
            ):  # it could be already locked by a previous occurrence of onTimer()
                with self.image_lock:
                    #
                    # The lock is held in order to prevent subsequent instances of onTimer() to act upon ImageReady
                    #  and possibly attempt to read the ImageArray.
                    #
                    # While the lock is held:
                    # - We check if ImageReady == True
                    # - If ImageReady == True:
                    #   - We read the image from the camara into self.image (CameraActivities.ReadingOut)
                    #   - We inform others that the image is available (in memory) by setting the image_ready_event
                    # - Optionally, in a separate thread (iff self.latest_exposure.file is not None):
                    #   - We save the image (CameraActivities.Saving)
                    #   - We inform others that the image is available (in memory) by setting the image_saved_event
                    #
                    if self.image is None and not self.is_active(
                        CameraActivities.ReadingOut
                    ):
                        #
                        # The timer may hit more than once while the image is being read.
                        #  self.image becomes not None only after ALL the data was downloaded from the camera
                        #
                        response = ascom_run(self, "ImageReady")
                        if response.succeeded and response.value:
                            self.start_activity(CameraActivities.ReadingOut)
                            # download the image from the camera
                            response = ascom_run(self, "ImageArray")
                            self.image = (
                                np.array(response.value) if response.succeeded else None
                            )
                            self.end_activity(CameraActivities.ReadingOut)
                            self.image_was_read = True
                            self.latest_settings.fits_cards["UT-END"] = (
                                datetime.datetime.now(datetime.UTC).isoformat(),
                                "UT end of exposure",
                            )
                            self.image_ready_event.set()  # tell everybody the image is available (in memory)

                            for visualizer in self.visualizers:
                                Thread(
                                    target=visualizer.func,
                                    name=f"{visualizer.name}",
                                    args=[self.image],
                                ).start()

                            self.save_to_file()  # in a separate thread, also informs everybody the file was saved

        if self.latest_temperature_check and (
            now - self.latest_temperature_check
        ) >= datetime.timedelta(seconds=self.temp_check_interval):
            response = ascom_run(self, "CCDTemperature")
            if response.succeeded:
                ccd_temp = response.value
                self.latest_temperature_check = now
                response = ascom_run(self, "CoolerPower")
                if response.succeeded:
                    cooler_power = response.value
                    logger.debug(f"{ccd_temp=}, {cooler_power=}")

        # if self.is_active(CameraActivities.CoolingDown):
        #     ccd_temp = ascom_run(self, 'CCDTemperature')
        #     # ambient_temp = ascom_run(self, 'HeatSinkTemperature')
        #     # logger.debug(f"{ambient_temp=}, {ccd_temp=}")
        #     if ccd_temp <= self.operational_set_point:
        #         self.end_activity(CameraActivities.CoolingDown)
        #         self.end_activity(CameraActivities.StartingUp)
        #         logger.info(f'cool-down: done ' +
        #           f'(temperature={ccd_temp:.1f}, set-point={self.operational_set_point})')

        # if self.is_active(CameraActivities.WarmingUp):
        #     ccd_temp = ascom_run(self, 'CCDTemperature')
        #     if ccd_temp >= self.warm_set_point:
        #         ascom_run(self, 'CoolerOn = False')
        #         logger.info('turned cooler OFF')
        #         self.end_activity(CameraActivities.WarmingUp)
        #         self.end_activity(CameraActivities.ShuttingDown)
        #         logger.info(f'warm-up done (temperature={ccd_temp:.1f}, set-point={self.warm_set_point})')
        #         self.power_off()

    @property
    def operational(self) -> bool:
        response = ascom_run(self, "CoolerOn")

        return all(
            [
                self.power_switch.detected,
                self.is_on(),
                self.detected,
                self._ascom,
                self._ascom.connected,
                response.succeeded,
                response.value,
            ]
        )

    @property
    def why_not_operational(self) -> List[str]:
        label = f"{self.name}"
        response = ascom_run(self, "CoolerOn")

        ret = []
        if not self.power_switch.detected:
            ret.append(
                f"{label}: power switch '{self.power_switch.hostname}' "
                + f"(at '{self.power_switch.ipaddr}') not detected"
            )
        elif not self.is_on():
            ret.append(f"{label}: not powered")
        elif not self.detected:
            ret.append(f"{label}: not detected")
        elif not self._ascom:
            ret.append(f"{label}: (ASCOM) - no handle")
        elif not self._ascom.connected:
            ret.append(f"{label}: (ASCOM) - not connected")
        elif not (response.succeeded and response.value):
            ret.append(f"{label}: (ASCOM) - cooler not ON")

        return ret

    @property
    def name(self) -> str:
        return "camera"

    @property
    def detected(self) -> bool:
        return self._detected

    @property
    def was_shut_down(self) -> bool:
        return self._was_shut_down

    def cooler_on(self):
        if not self.connected:
            self.errors.append("cooler_on: not connected")
            logger.error("cooler_on: not connected")
            return

        response = ascom_run(self, "CoolerOn = True")
        if response.succeeded:
            logger.info(f"cooler turned ON")
        else:
            logger.error(f"cooler ON failed ({response.failure})")
        return response

    def cooler_off(self):
        if not self.connected:
            self.errors.append("cooler_off: not connected")
            logger.error("cooler_off: not connected")
            return

        response = ascom_run(self, "CoolerOn = False")
        if response.succeeded:
            logger.info(f"cooler turned OFF")
        else:
            logger.error(f"cooler OFF failed ({response.failure})")
        return response

    def save_to_file(self):
        Thread(name="image-saver-thread", target=self.do_save_to_file).start()

    def do_save_to_file(self):
        op = function_name()

        if self.image is None:
            logger.error(f"{op}: image is None")
            return

        self.start_activity(CameraActivities.Saving)

        header = fits.Header()
        header["SIMPLE"] = (True, "file conforms to FITS standard")
        header["BITPIX"] = (32, "array data type")
        header["NAXIS"] = (2, "number of array dimensions")
        header["NAXIS1"] = (self.image.shape[0], "length of data axis 1")
        header["NAXIS2"] = (self.image.shape[1], "length of data axis 2")
        header["EXTEND"] = (True, "FITS data sets may contain extensions")
        header["DATE-OBS"] = (
            datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "Observation datetime",
        )
        header["XBINNING"] = (self.binning.x, "horizontal binning")
        header["YBINNING"] = (self.binning.y, "vertical binning")
        header["EXPTIME"] = (self.latest_settings.seconds, "exposure time in seconds")
        header["INSTRUME"] = (socket.gethostname(), "the instrument")
        if self.ccd_temp_at_mid_exposure:
            header["CCDTEMP"] = (
                self.ccd_temp_at_mid_exposure,
                "ccd temp. at mid exposure",
            )
            self.ccd_temp_at_mid_exposure = None

        if self.unit:
            header["FOCUSPOS"] = self.unit.focuser.position
            header.comments["FOCUSPOS"] = "focuser position"
            header["STAGEPOS"] = self.unit.stage.position
            header.comments["STAGEPOS"] = "FIFA stage position"

        if self.latest_settings.fits_cards:
            for k, v in self.latest_settings.fits_cards.items():
                header[k] = v

        hdu = fits.PrimaryHDU(data=np.transpose(self.image), header=fits.Header(header))
        hdu_list = fits.HDUList([hdu])
        logger.info(f"{op}: saving image to {self.latest_settings.image_path} ...")
        hdu_list.writeto(self.latest_settings.image_path, checksum=True, overwrite=True)

        self.image_was_saved = True
        self.image_saved_event.set()
        self.end_activity(CameraActivities.Saving)

    def register_visualizer(self, name: str, visualizer: Callable):
        self.visualizers.append(Visualizer(name=name, func=visualizer))

    def wait_for_image_saved(self):
        op = function_name()
        if not self.image_was_saved:
            # logger.info(f"{op}: image was not saved, waiting for image_saved_event ...")
            self.image_saved_event.wait()
            logger.info(f"{op}: got image_saved_event")
            self.image_saved_event.clear()
        # else:
        #     logger.info(f"{op}: image was saved, not waiting for image_saved_event.")

    def wait_for_image_ready(self):
        op = function_name()
        if not self.image_was_read:
            # logger.info(f"{op}: image was not read, waiting for image_ready_event ...")
            self.image_ready_event.wait()
            logger.info(f"{op}: image was not read, got image_ready_event")
            self.image_ready_event.clear()
        # else:
        #     logger.info(f"{op}: image was read, not waiting for image_ready_event.")


base_camera_path = BASE_UNIT_PATH + "/camera"
tag = "Camera"

camera = Camera(unit=None)

router = APIRouter()
router.add_api_route(base_camera_path + "/startup", tags=[tag], endpoint=camera.startup)
router.add_api_route(
    base_camera_path + "/shutdown", tags=[tag], endpoint=camera.shutdown
)
router.add_api_route(base_camera_path + "/abort", tags=[tag], endpoint=camera.abort)
router.add_api_route(base_camera_path + "/status", tags=[tag], endpoint=camera.status)
router.add_api_route(base_camera_path + "/connect", tags=[tag], endpoint=camera.connect)
router.add_api_route(
    base_camera_path + "/disconnect", tags=[tag], endpoint=camera.disconnect
)
router.add_api_route(
    base_camera_path + "/start_exposure",
    tags=[tag],
    endpoint=camera.endpoint_start_exposure,
)
router.add_api_route(
    base_camera_path + "/stop_exposure", tags=[tag], endpoint=camera.stop_exposure
)
router.add_api_route(
    base_camera_path + "/abort_exposure", tags=[tag], endpoint=camera.abort_exposure
)
router.add_api_route(
    base_camera_path + "/cooler_on", tags=[tag], endpoint=camera.cooler_on
)
router.add_api_route(
    base_camera_path + "/cooler_off", tags=[tag], endpoint=camera.cooler_off
)
