import datetime
import logging
import socket
import threading
import time
from collections.abc import Callable
from enum import IntFlag
from logging import Logger
from threading import Lock, Thread
from typing import TYPE_CHECKING

import numpy as np
import win32com.client
from astropy.io import fits

from common.activities import ImagerActivities
from common.ascom import AscomDispatcher, ascom_run
from common.canonical import CanonicalResponse, CanonicalResponse_Ok
from common.components import Component
from common.config import Config
from common.dlipowerswitch import OutletDomain, SwitchedOutlet
from common.mast_logging import init_log
from common.paths import PathMaker
from common.utils import RepeatTimer, function_name, time_stamp

from . import ImagerBinning, ImagerExposure, ImagerInterface, ImagerRoi, ImagerSettings, ImagerStatus

if TYPE_CHECKING:
    from unit import Unit

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


class ASCOMImager(ImagerInterface, SwitchedOutlet, AscomDispatcher):
    _instance = None
    _initialized = False

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    @property
    def can_image_to_memory(self) -> bool:
        return True

    @property
    def full_frame_roi(self) -> ImagerRoi:
        width = self.cameraXSize if self.cameraXSize else 1000
        height = self.cameraYSize if self.cameraYSize else 1000
        return ImagerRoi(x=0, y=0, width=width, height=height)

    @property
    def camera_x_size(self) -> int | None:
        return self.cameraXSize

    @property
    def camera_y_size(self) -> int | None:
        return self.cameraYSize

    @property
    def logger(self) -> Logger:
        return logger

    @property
    def ascom(self) -> win32com.client.Dispatch: # type: ignore
        return self._ascom

    def __init__(self, unit: "Unit", prog_id: str | None = None):
        #
        # The Camera() is a Singleton but the initiator is called twice (for the same object ID):
        # - once from this file, with unit as None
        # - one from the unit, with unit as the main Unit object
        #


        self.unit = unit
        self.prog_id = prog_id

        if self._initialized:
            return

        self.defaults = {
            "temp_check_interval": 15,
        }

        self.conf = Config().get_unit().imager
        Component.__init__(self)
        SwitchedOutlet.__init__(self, OutletDomain.Unit, outlet_name="Camera")

        if not prog_id:
            prog_id = Config().get_unit().imager.imager_type.replace("ascom:", "")
        if not prog_id:
            raise Exception(
                "ASCOMImager: no ASCOM driver specified either as parameter or in the configuration"
            )
        self.prog_id = prog_id

        try:
            if not self.is_on():
                self.power_on()

            self._ascom = win32com.client.Dispatch(self.prog_id)
        except Exception as ex:
            logger.exception(ex)
            raise ex

        self.latest_settings: ImagerSettings | None = None
        self.latest_temperature_check: datetime.datetime | None = None
        self.temp_check_interval = self.conf.temp_check_interval

        self._is_exposing: bool = False
        self.operational_set_point: float = -25
        self.warm_set_point: float = 5  # temperature at which the camera is considered warm
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
        self.errors: list[str] = []
        self.expected_mid_exposure: datetime.datetime | None = None
        self.ccd_temp_at_mid_exposure: float | None = None
        self._binning: ImagerBinning = ImagerBinning(x=1, y=1)
        self._roi: ImagerRoi | None = None
        self._gain: int | None = None

        self._was_shut_down: bool = False

        self.timer: RepeatTimer = RepeatTimer(1, function=self.ontimer)
        self.timer.name = "camera-timer-thread"
        self.timer.start()

        self._detected = False
        self.image_lock: Lock = Lock()
        self.image_was_read: bool = False
        self.image_was_saved: bool = False

        self.visualizers: list[Visualizer] = []

        self.image_ready_event: threading.Event = threading.Event()
        self.image_saved_event: threading.Event = threading.Event()

        self.guiding_roi_width: int | None = None
        self.guiding_roi_height: int | None = None

        self._initialized = True
        # logger.info('initialized')

    @property
    def image_array(self) -> np.ndarray | None:
        """
        Returns the image array, if available.  If the image is not available, returns None.
        """
        if self.image_was_read and self.image is not None:
            return self.image
        return None

    @property
    def binning(self):
        return self._binning

    @binning.setter
    def binning(self, value: ImagerBinning):
        if self.maxBinX and (1 > value.x > self.maxBinX):
            raise Exception(f"bad {value.x=}, must be > 1 and < {self.maxBinX=}")
        if self.maxBinY and(1 > value.y > self.maxBinY):
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
    def roi(self) -> ImagerRoi | None:
        return self._roi

    @roi.setter
    def roi(self, value: ImagerRoi):
        if self.cameraXSize and (0 > value.x > self.cameraXSize):
            raise Exception(
                f"bad {value.x=}, must be 0 > x > {self.cameraXSize=}"
            )
        if self.cameraYSize and (0 > value.y > self.cameraYSize):
            raise Exception(
                f"bad {value.y=}, must be 0 > y > {self.cameraYSize=}"
            )
        if self.cameraXSize and (0 > value.width > self.cameraXSize):
            raise Exception(
                f"bad {value.width=}, must be 0 > width > {self.cameraXSize=}"
            )
        if self.cameraYSize and (0 > value.height > self.cameraYSize):
            raise Exception(
                f"bad {value.height=}, must be 0 > height > {self.cameraYSize=}"
            )
        if self.cameraXSize and (value.x + value.width > self.cameraXSize):
            raise Exception(
                f"{value.x=} + {value.width=} exceeds {self.cameraXSize=}"
            )
        if self.cameraYSize and (value.y + value.height > self.cameraYSize):
            raise Exception(
                f"{value.y=} + {value.height=} exceeds {self.cameraYSize=}"
            )

        response_x = ascom_run(self, f"StartX ={value.x}")
        response_y = ascom_run(self, f"StartY = {value.y}")
        response_width = ascom_run(self, f"NumX = {value.width}")
        response_height = ascom_run(self, f"NumY = {value.height}")

        if (
            response_x.failed
            or response_y.failed
            or response_height.failed
            or response_width.failed
        ):
            if self._roi:
                ascom_run(self, f"StartX = {self._roi.x}")
                ascom_run(self, f"StartY = {self._roi.y}")
                ascom_run(self, f"NumX = {self._roi.width}")
                ascom_run(self, f"NumY = {self._roi.height}")

            raise Exception(
                f"errors: {response_x.failure=}, {response_y.failure=}, "
                + f"{response_width.failure=}, {response_height.failure=}"
            )
        else:
            self._roi = ImagerRoi(x=value.x, y=value.y, width=value.width, height=value.height)

    @property
    def connected(self) -> bool:
        if not self.is_on() or not self._ascom:
            return False
        response = ascom_run(self, "Connected", no_entry_log=True)
        return response.value if response.succeeded else False # type: ignore

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
                if response.succeeded and isinstance(response.value, int):
                    self.maxBinX = int(response.value)

                response = ascom_run(self, "MaxBinY")
                if response.succeeded and isinstance(response.value, int):
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
                    + f" driver: '{self.conf.imager_type}'"
                )

                if self.cameraXSize and self.cameraYSize:
                    self.guiding_roi_width = int((self.cameraXSize / 100) * 90)
                    self.guiding_roi_height = int((self.cameraYSize / 100) * 80)
        else:
            logger.error(f"failed 'connected = {value}' (failure='{response.failure}')")
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
            raise Exception("cannot set gain, not connected")

        if self.GainMin is not None and self.GainMax is not None and self.GainMin > value > self.GainMax:
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
        seconds: float | None = 5,
        gain: int | None = 170,
        binning: int | None = 1,
        center_x: int | None = None,
        center_y: int | None = None,
        width: int | None = None,
        height: int | None = None,
    ):

        if self.cameraXSize is None or self.cameraYSize is None:
            return CanonicalResponse(
                errors=["cameraXSize or cameraYSize is not set, cannot start exposure"]
            )
        center_x = center_x if center_x is not None else int(self.cameraXSize / 2)
        center_y = center_y if center_y is not None else int(self.cameraYSize / 2)
        width = width if width is not None else self.cameraXSize
        height = height if height is not None else self.cameraYSize

        roi = ImagerRoi(x=center_x - int(width / 2), y=center_y - int(height / 2), width=width, height=height)
        binning = binning if isinstance(binning, int) else 1

        settings = ImagerSettings(
            seconds=seconds if isinstance(seconds, int | float) else 5,
            base_folder=PathMaker().make_exposures_folder(),
            gain=gain,
            binning=ImagerBinning(x=binning, y=binning),
            roi=roi,
            tags=None,
            save=True,
        )

        self.start_exposure(settings)

    def start_exposure(self, settings: ImagerSettings) -> CanonicalResponse:
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

        if self.is_active(ImagerActivities.Exposing):
            logger.info(f"{op}: already exposing")
            return CanonicalResponse(errors=["already exposing"])

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

        response = ascom_run(self, f"StartExposure({settings.seconds}, True)")
        if response.value is None:
            self.start_activity(ImagerActivities.Exposing)
            self.expected_mid_exposure = datetime.datetime.now() + datetime.timedelta(
                seconds=settings.seconds / 2
            )
            self.image = None
            self.image_was_read = False
            self.image_was_saved = False
            self.latest_settings = settings
            if not self.latest_settings.fits_cards:
                self.latest_settings.fits_cards = {}
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

            self.end_activity(ImagerActivities.Exposing)
        else:
            if response.errors:
                self.errors.extend(response.errors)

        return (
            CanonicalResponse(errors=self.errors)
            if self.errors
            else CanonicalResponse_Ok
        )

    def abort_exposure(self):
        """
        Aborts the current **MAST** camera exposure. No image readout.
        """
        self.errors = []
        if not self.connected:
            self.errors.append("not connected")
            return
        if not self.is_active(ImagerActivities.Exposing):
            self.errors.append("not exposing")

        response = ascom_run(self, "CanAbortExposure")
        if response.succeeded and response.value:
            response = ascom_run(self, "AbortExposure()")
            if response.failed:
                self.errors.append(f"failed to abort (failure='{response.failure}')")
        self.end_activity(ImagerActivities.Exposing)
        return (
            CanonicalResponse(errors=self.errors)
            if self.errors
            else CanonicalResponse_Ok
        )

    def stop_exposure(self):
        """
        Stops the current **MAST** camera exposure.  An image readout is initiated
        """
        self.errors = []
        if not self.connected:
            self.errors.append("not connected")
            return CanonicalResponse(errors=["not connected"])

        if not self.is_active(ImagerActivities.Exposing):
            self.errors.append("not exposing")
            return CanonicalResponse(errors=["not connected"])

        response = ascom_run(self, "StopExposure()")  # the timer will read the image
        if response.failed:
            self.errors.append(
                f"could not StopExposure(), (failure='{response.failure}')"
            )
        self.end_activity(ImagerActivities.Exposing)

        return (
            CanonicalResponse(errors=self.errors)
            if self.errors
            else CanonicalResponse_Ok
        )

    def status(self) -> ImagerStatus:
        """
        Gets the **MAST** imager status
        """

        return ImagerStatus(
            **self.power_status().model_dump(),
            **self.ascom_status().model_dump(),
            **self.component_status().model_dump(),
            set_point=self.operational_set_point,
            temperature=self.temperature,
            cooler=self._ascom.CoolerOn,
            cooler_power=self._ascom.CoolerPower,
            latest_exposure=(
                ImagerExposure(
                    file=self.latest_settings.base_folder,
                    seconds=self.latest_settings.seconds,
                    date=self.latest_settings.start,
                )
                if self.latest_settings
                else None
            ),
            date=time_stamp(),
        )

    @property
    def cooler_on(self) -> bool:
        """
        Returns the **MAST** camera cooler state
        """
        if not self.connected:
            self.errors.append("cooler_on.getter: not connected")
            logger.error("cooler_on.getter: not connected")
            return False

        response = ascom_run(self, "CoolerOn")
        if response.succeeded and response.value is not None:
            return bool(response.value)
        else:
            self.errors.append(f"cooler_on.getter: {response.errors}")
            logger.error(f"cooler_on.getter: {response.errors}")
            return False

    @cooler_on.setter
    def cooler_on(self, value: bool):
        if not self.connected:
            self.errors.append("cooler_on.setter: not connected")
            logger.error("cooler_on.setter: not connected")
            return

        response = ascom_run(self, f"CoolerOn = {"True" if value else "False"}")
        if response.errors:
            self.errors.append(f"cooler_on.setter: {response.errors}")
            logger.error(f"cooler_on.setter: {response.errors}")

    def startup(self):
        """
        Starts the **MAST** camera up (cooling down , if needed)
        """
        # self.start_activity(CameraActivities.StartingUp)
        self.errors = []
        self.power_on()
        self.connect()
        self.cooler_on = True
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
            self.start_activity(ImagerActivities.CoolingDown)
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

            self.cooler_on = True
        return CanonicalResponse_Ok

    def shutdown(self):
        """
        Shuts the **MAST** camera down (warms up, if needed)

        :mastapi:
        """
        # if self.connected:
        #     self.start_activity(CameraActivities.ShuttingDown)
        #     if abs(self.temperature - self.warm_set_point) > 0.5:
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
            self.start_activity(ImagerActivities.WarmingUp)
            current_temp = self.temperature
            if current_temp is None:
                logger.error(
                    f"could not get current CCD temperature (failure='{response.failure}')"
                )

            response = ascom_run(self, f"SetCCDTemperature({self.warm_set_point})")
            if response.succeeded:
                message = "warm-up started:"
                if current_temp:
                    message = message + f" current temp: {current_temp:.1f},"
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

        current_state = None
        response = ascom_run(self, "CameraState")
        if response.succeeded:
            current_state = response.value
        else:
            return

        now = datetime.datetime.now()
        # previous_state = self.last_state
        if self.last_state is None and current_state is not None:
            self.last_state = current_state
            logger.info(
                f"state changed from None to {AscomCameraState(self.last_state).__repr__()}"
            )
        else:
            if current_state is not None and current_state != self.last_state:
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
            ccd_temp = self.temperature
            if ccd_temp is not None:
                self.ccd_temp_at_mid_exposure = ccd_temp
                self.expected_mid_exposure = None

        # logger.info(f"is_active(CameraActivities.Exposing)={self.is_active(CameraActivities.Exposing)}, {current_state=}")
        if (
            self.is_active(ImagerActivities.Exposing)
            and current_state == AscomCameraState.Idle
        ) and (
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
                    ImagerActivities.ReadingOut
                ):
                    #
                    # The timer may hit more than once while the image is being read.
                    #  self.image becomes not None only after ALL the data was downloaded from the camera
                    #
                    response = ascom_run(self, "ImageReady")
                    if response.succeeded and response.value:
                        self.start_activity(ImagerActivities.ReadingOut)
                        # download the image from the camera
                        response = ascom_run(self, "ImageArray")
                        self.image = (
                            np.array(response.value) if response.succeeded else None
                        )
                        self.end_activity(ImagerActivities.ReadingOut)
                        self.image_was_read = True
                        if self.latest_settings and self.latest_settings.fits_cards is None:
                            self.latest_settings.fits_cards = {}
                        if self.latest_settings and self.latest_settings.fits_cards is not None:
                            self.latest_settings.fits_cards["CCD-TEMP"] = (
                                self.ccd_temp_at_mid_exposure,
                                "CCD temperature at mid-exposure",
                            )
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
            ccd_temp = self.temperature
            if ccd_temp is None:
                logger.error(
                    f"failed to get CCDTemperature (failure='{response.failure}')"
                )
            self.latest_temperature_check = now

            # cooler_power = self.cooler_power

        # if self.is_active(CameraActivities.CoolingDown):
        #     ccd_temp = self.temperature
        #     # ambient_temp = ascom_run(self, 'HeatSinkTemperature')
        #     # logger.debug(f"{ambient_temp=}, {ccd_temp=}")
        #     if ccd_temp <= self.operational_set_point:
        #         self.end_activity(CameraActivities.CoolingDown)
        #         self.end_activity(CameraActivities.StartingUp)
        #         logger.info(f'cool-down: done ' +
        #           f'(temperature={ccd_temp:.1f}, set-point={self.operational_set_point})')

        # if self.is_active(CameraActivities.WarmingUp):
        #     ccd_temp = self.temperature
        #     if ccd_temp >= self.warm_set_point:
        #         ascom_run(self, 'CoolerOn = False')
        #         logger.info('turned cooler OFF')
        #         self.end_activity(CameraActivities.WarmingUp)
        #         self.end_activity(CameraActivities.ShuttingDown)
        #         logger.info(f'warm-up done (temperature={ccd_temp:.1f}, set-point={self.warm_set_point})')
        #         self.power_off()

    @property
    def temperature(self) -> float:
        """
        Returns the current camera temperature

        :mastapi:
        """
        if not self.connected:
            return float("nan")

        response = ascom_run(self, "CCDTemperature")
        if response.value is not None and response.succeeded:
            return response.value
        else:
            logger.error(f"failed to get CCDTemperature (failure='{response.failure}')")
            return float("nan")

    @property
    def operational(self) -> bool:
        response = ascom_run(self, "CoolerOn")

        return all(
            [
                self.power_switch and self.power_switch.detected,
                self.is_on(),
                self.detected,
                self._ascom,
                self._ascom.connected,
                response.succeeded,
                response.value,
            ]
        )

    @property
    def why_not_operational(self) -> list[str]:
        label = f"{self.name}"
        cooler_response = ascom_run(self, "CoolerOn")

        ret = []
        if not self.power_switch:
            ret.append(f"{label}: no power switch")
        else:
            if not self.power_switch.detected:
                ret.append(
                    f"{label}: power switch '{self.power_switch.hostname}' "
                    + f"(at '{self.power_switch.ipaddr}') not detected"
                )
            else:
                ret.append(f"{label}: {"powered on" if self.is_on() else "not powered"}")
        ret.append(f"{label}: {"detected" if self._detected else "not detected"}")
        if not self._ascom:
            ret.append(f"{label}: (ASCOM) - no handle")
        else:
            ret.append(f"{label}: (ASCOM) - {"connected" if self._ascom.connected else "not connected"}")

        ret.append(f"{label}: (ASCOM) - cooler {"ON" if cooler_response.succeeded and cooler_response.value else "OFF"}")

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

    @property
    def cooler_power(self) -> float | None:
        response = ascom_run(self, "CoolerPower")
        if response.errors:
            self.errors.append(f"cooler: {response.errors}")
            logger.error(f"cooler: {response.errors}")
            return None
        else:
            return response.value

    def save_to_file(self):
        Thread(name="image-saver-thread", target=self.do_save_to_file).start()

    def do_save_to_file(self):
        op = function_name()

        if self.image is None:
            logger.error(f"{op}: image is None")
            return

        if not self.latest_settings or not self.latest_settings.image_path:
            logger.error(f"{op}: no image_path in latest_settings")
            return

        self.start_activity(ImagerActivities.Saving)

        header = fits.Header()
        header["SIMPLE"] = (True, "file conforms to FITS standard")
        header["BITPIX"] = (32, "array data type")
        header["NAXIS"] = (2, "number of array dimensions")
        header["NAXIS1"] = (self.image.shape[0], "length of data axis 1")
        header["NAXIS2"] = (self.image.shape[1], "length of data axis 2")
        header["EXTEND"] = (True, "FITS data sets may contain extensions")
        header["DATE-OBS"] = (
            datetime.datetime.now(datetime.UTC).isoformat(),
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

        if self.latest_settings and self.latest_settings.fits_cards:
            for k, v in self.latest_settings.fits_cards.items():
                header[k] = v

        hdu = fits.PrimaryHDU(data=np.transpose(self.image), header=fits.Header(header))
        hdu_list = fits.HDUList([hdu])
        logger.info(f"{op}: saving image to {self.latest_settings.image_path} ...")
        hdu_list.writeto(self.latest_settings.image_path, checksum=True, overwrite=True)

        self.image_was_saved = True
        self.image_saved_event.set()
        self.end_activity(ImagerActivities.Saving)

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
