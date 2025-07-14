import datetime
import json
import logging
import socket
from enum import IntEnum, auto
from threading import Event, Thread
from typing import Any

import numpy as np
import pyzwoasi as asi
from astropy.io import fits

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

logger = logging.Logger("mast-unit-imager-zwo")
init_log(logger)

# if TYPE_CHECKING:
#     from unit import Unit


#
# Extracted at runtime from the ZWO ASI SDK for camera model: ZWO ASI294MM Pro
#
class AsiControl(IntEnum):
    Gain = 0  # Gain,
    Exposure = 1  # Exposure Time(us),
    Offset = 5  # offset,
    BandWidth = 6  # The total data transfer rate percentage,
    Flip = 9  # Flip: 0->None 1->Horiz 2->Vert 3->Both,
    AutoExpMaxGain = 10  # Auto exposure maximum gain value,
    AutoExpMaxExpMS = 11  # Auto exposure maximum exposure value(unit ms),
    AutoExpTargetBrightness = 12  # Auto exposure target brightness value,
    HighSpeedMode = 14  # Is high speed mode:0->No 1->Yes,
    Temperature = 8  # Sensor temperature(degrees Celsius),
    CoolPowerPerc = 15  # Cooler power percent,
    TargetTemp = 16  # Target temperature(cool camera only),
    CoolerOn = 17  # turn on/off cooler(cool camera only),


AsiControlDict: dict[AsiControl, dict] = {
    AsiControl.Gain: {
        "description": "Gain",
        "min_value": 0,
        "max_value": 570,
        "default": 200,
        "is_writable": 1,
        "is_auto_supported": 1,
        "control_type": 0,
        "auto": False,
    },
    AsiControl.Exposure: {
        "description": "Exposure Time(us)",
        "min_value": 32,
        "max_value": 2000000000,
        "default": 10000,
        "is_writable": 1,
        "is_auto_supported": 1,
        "control_type": 1,
        "auto": False,
    },
    AsiControl.Offset: {
        "description": "offset",
        "min_value": 0,
        "max_value": 80,
        "default": 8,
        "is_writable": 1,
        "is_auto_supported": 0,
        "control_type": 5,
        "auto": False,
    },
    AsiControl.BandWidth: {
        "description": "The total data transfer rate percentage",
        "min_value": 40,
        "max_value": 100,
        "default": 50,
        "is_writable": 1,
        "is_auto_supported": 1,
        "control_type": 6,
        "auto": True,
    },
    AsiControl.Flip: {
        "description": "Flip: 0->None 1->Horiz 2->Vert 3->Both",
        "min_value": 0,
        "max_value": 3,
        "default": 0,
        "is_writable": 1,
        "is_auto_supported": 0,
        "control_type": 9,
        "auto": False,
    },
    AsiControl.AutoExpMaxGain: {
        "description": "Auto exposure maximum gain value",
        "min_value": 0,
        "max_value": 570,
        "default": 285,
        "is_writable": 1,
        "is_auto_supported": 0,
        "control_type": 10,
        "auto": False,
    },
    AsiControl.AutoExpMaxExpMS: {
        "description": "Auto exposure maximum exposure value(unit ms)",
        "min_value": 1,
        "max_value": 60000,
        "default": 100,
        "is_writable": 1,
        "is_auto_supported": 0,
        "control_type": 11,
        "auto": False,
    },
    AsiControl.AutoExpTargetBrightness: {
        "description": "Auto exposure target brightness value",
        "min_value": 50,
        "max_value": 160,
        "default": 100,
        "is_writable": 1,
        "is_auto_supported": 0,
        "control_type": 12,
        "auto": False,
    },
    AsiControl.HighSpeedMode: {
        "description": "Is high speed mode:0->No 1->Yes",
        "min_value": 0,
        "max_value": 1,
        "default": 0,
        "is_writable": 1,
        "is_auto_supported": 0,
        "control_type": 14,
        "auto": False,
    },
    AsiControl.Temperature: {
        "description": "Sensor temperature(degrees Celsius)",
        "min_value": -500,
        "max_value": 1000,
        "default": 20,
        "is_writable": 0,
        "is_auto_supported": 0,
        "control_type": 8,
        "auto": False,
    },
    AsiControl.CoolPowerPerc: {
        "description": "Cooler power percent",
        "min_value": 0,
        "max_value": 100,
        "default": 0,
        "is_writable": 0,
        "is_auto_supported": 0,
        "control_type": 15,
        "auto": False,
    },
    AsiControl.TargetTemp: {
        "description": "Target temperature(cool camera only)",
        "min_value": -40,
        "max_value": 30,
        "default": 0,
        "is_writable": 1,
        "is_auto_supported": 0,
        "control_type": 16,
        "auto": False,
    },
    AsiControl.CoolerOn: {
        "description": "turn on/off cooler(cool camera only)",
        "min_value": 0,
        "max_value": 1,
        "default": 0,
        "is_writable": 1,
        "is_auto_supported": 0,
        "control_type": 17,
        "auto": False,
    },
}

#
# From:
#   ASICamera2 Software Development Kit manual
# Section 2.9:
#   typedef enum ASI_CONTROL_TYPE
#
# ASI_GAIN = 0,//gain
# ASI_EXPOSURE,//exposure time (microsecond)
# ASI_GAMMA,//gamma with range 1 to 100 (nominally 50)
# ASI_WB_R,//red component of white balance
# ASI_WB_B,// blue component of white balance
# ASI_BRIGHTNESS,//pixel value offset (a bias, not a scale factor)
# ASI_BANDWIDTHOVERLOAD,//The total data transfer rate percentage
# ASI_OVERCLOCK,//over clock
# ASI_TEMPERATURE,// sensor temperature，10 times the actual temperature
# ASI_FLIP,//image flip
# ASI_AUTO_MAX_GAIN,//maximum gain when auto adjust
# ASI_AUTO_MAX_EXP,//maximum exposure time when auto adjust，unit is micro seconds
# ASI_AUTO_MAX_BRIGHTNESS,//target brightness when auto adjust
# ASI_HARDWARE_BIN,//hardware binning of pixels
# ASI_HIGH_SPEED_MODE,//high speed mode
# ASI_COOLER_POWER_PERC,//cooler power percent(only cool camera)
# ASI_TARGET_TEMP,//sensor's target temperature(only cool camera)，don't multiply by 10
# ASI_COOLER_ON//open cooler (only cool camera)
# ASI_MONO_BIN,//lead to a smaller grid at software bin mode for color camera
# ASI_FAN_ON,//only cooled camera has fan
# ASI_PATTERN_ADJUST.//currently only supported by 1600 mono camera
# ASI_ANTI_DEW_HEATER
# } ASI_CONTROL_TYPE;

# class AsiControl(IntFlag):
#     ASI_GAIN = 0   # gain
#     ASI_EXPOSURE = auto()               # exposure time (microsecond)
#     ASI_GAMMA = auto()                  # gamma with range 1 to 100 (nominally 50)
#     ASI_WB_R = auto()                   # red component of white balance
#     ASI_WB_B = auto()                   # blue component of white balance
#     ASI_BRIGHTNESS = auto()             # pixel value offset (a bias, not a scale factor)
#     ASI_BANDWIDTHOVERLOAD = auto()      # The total data transfer rate percentage
#     ASI_OVERCLOCK = auto()              # over clock
#     ASI_TEMPERATURE = auto()            # sensor temperature，10 times the actual temperature
#     ASI_FLIP = auto()                   # image flip
#     ASI_AUTO_MAX_GAIN = auto()          # maximum gain when auto adjust
#     ASI_AUTO_MAX_EXP = auto()           # maximum exposure time when auto adjust，unit is micro seconds
#     ASI_AUTO_MAX_BRIGHTNESS = auto()    # target brightness when auto adjust
#     ASI_HARDWARE_BIN = auto()           # hardware binning of pixels
#     ASI_HIGH_SPEED_MODE = auto()        # high speed mode
#     ASI_COOLER_POWER_PERC = auto()      # cooler power percent(only cool camera)
#     ASI_TARGET_TEMP = auto()            # sensor's target temperature(only cool camera)，don't multiply by 10
#     ASI_COOLER_ON = auto()              # open cooler (only cool camera)
#     ASI_MONO_BIN = auto()               # lead to a smaller grid at software bin mode for color camera
#     ASI_FAN_ON = auto()                 # only cooled camera has fan
#     ASI_PATTERN_ADJUST = auto()         # currently only supported by 1600 mono camera
#     ASI_ANTI_DEW_HEATER = auto()


class AsiOutputFormat(IntEnum):
    RAW8 = 0
    RGB24 = 1
    RAW16 = 2
    Y8 = 3


class ASIExposureStatus(IntEnum):
    """
    From:
        ASICamera2 Software Development Kit, v1.37

    Section 2.11
        typedef enum ASI_EXPOSURE_STATUS {
            ASI_EXP_IDLE = 0,   // idle, ready to start exposure
            ASI_EXP_WORKING,    // exposure in progress
            ASI_EXP_SUCCESS,    // exposure completed successfully, image can be read out
            ASI_EXP_FAILED,     // exposure failure, need to restart exposure
        } ASI_EXPOSURE_STATUS;
    """

    ASI_EXP_IDLE = 0  # idle, ready to start exposure
    ASI_EXP_WORKING = auto()  # exposure in progress
    ASI_EXP_SUCCESS = auto()  # exposure completed successfully, image can be read out
    ASI_EXP_FAILED = auto()  #  exposure failure, need to restart exposure


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

    def __init__(self, unit, imager_params: dict[str, Any] | None = None, _from_imager: bool = False):

        Component.__init__(self)
        if not _from_imager:
            SwitchedOutlet.group(
                domain=OutletDomain.Unit,
                group_name="Camera",
                outlet_names=["Camera", "CameraUSB"]).populate(self)
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

        if not self.is_on:
            self.power_on()

        n_cameras = asi.getNumOfConnectedCameras()
        logger.info(f"found {n_cameras} ASI camera(s), SDK={asi.getSDKVersion()}")
        self.cam_id = None
        self._connected: bool = False

        if n_cameras > 0:
            self.cam_id = 0
            self.connected = True
            if self.connected:  # did we succeed to connect?
                self.timer = RepeatTimer(interval=1, function=self.on_timer)
                self.timer.start()

        self._initialized = True

    def __del__(self):
        asi.closeCamera(self.cam_id)

    def make_pythonian_classes(self):
        """
        Makes pythonian classes from ASI internals (controls, etc.)
        """
        n_controls = asi.getNumOfControls(self.cam_id)
        enum_lines = ["class AsiControl(IntEnum):"]
        dict_lines = ["AsiControlDict: dict[AsiControl, dict] = {"]

        for control in range(n_controls):
            cap = asi.getControlCaps(self.cam_id, controlIndex=control)
            val, auto = asi.getControlValue(self.cam_id, controlType=cap.ControlType)
            line = f"{cap.Name.decode()} = {cap.ControlType}"
            enum_lines.append(
                f"    {line}{' ' * (30 - len(line))}# {cap.Description.decode()}, "
            )
            dict_lines.append(f"    AsiControl.{cap.Name.decode()}: {{")
            dict_lines.append(f"        'description': '{cap.Description.decode()}',")
            dict_lines.append(f"        'min_value': {cap.MinValue},")
            dict_lines.append(f"        'max_value': {cap.MaxValue},")
            dict_lines.append(f"        'default': {cap.DefaultValue},")
            dict_lines.append(f"        'is_writable': {cap.IsWritable},")
            dict_lines.append(f"        'is_auto_supported': {cap.IsAutoSupported},")
            dict_lines.append(f"        'control_type': {cap.ControlType},")
            dict_lines.append(f"        'auto': {auto},")
            dict_lines.append("    },")
        dict_lines.append("}")

        print(
            f"#\n# Extracted at runtime from the ZWO ASI SDK for camera model: {self.model}\n#"
        )
        for line in enum_lines:
            print(line)
        print()
        for line in dict_lines:
            print(line)
        print()

    def save_in_thread(self):
        self.start_activity(ImagerActivities.Saving)
        assert self.latest_settings is not None and self.latest_settings.roi is not None
        assert self.image_array is not None, "self.image_array is None"

        header = fits.Header()
        header["SIMPLE"] = (True, "file conforms to FITS standard")
        if self.output_format == AsiOutputFormat.RAW16:
            header["BITPIX"] = (16, "array data type")
            header["BZERO"] = 32768
        else:
            header["BITPIX"] = 8
            header["BZERO"] = 0
        header["BSCALE"] = 1
        header["NAXIS"] = (2, "number of array dimensions")
        header["NAXIS1"] = (self.image_array.shape[0], "length of data axis 1")
        header["NAXIS2"] = (self.image_array.shape[1], "length of data axis 2")
        header["EXTEND"] = (True, "FITS data sets may contain extensions")
        header["DATE-OBS"] = (
            datetime.datetime.now(datetime.UTC).isoformat(),
            "Observation datetime",
        )
        if self.latest_settings.binning:
            header["XBINNING"] = (self.latest_settings.binning.x, "horizontal binning")
            header["YBINNING"] = (self.latest_settings.binning.y, "vertical binning")
        header["EXPTIME"] = (self.latest_settings.seconds, "exposure time in seconds")
        header["INSTRUME"] = (f"{socket.gethostname()}:{self.model}", "the instrument")
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

        # header["NAXIS"] = 2
        # header["NAXIS1"] = self.latest_settings.roi.width
        # header["NAXIS2"] = self.latest_settings.roi.height
        # header['BITPIX'] = 16 if self.output_format == AsiOutputFormat.RAW16 else 8 # 16-bit unsigned integer (for RAW16)
        # header['BZERO'] = 0
        # header['BSCALE'] = 1
        # header['IMAGETYP'] = 'LIGHT'
        # header['INSTRUME'] = self.model
        # header['EXPTIME'] = self.latest_settings.seconds
        # if self.latest_settings.binning:
        #     header['XBINNING'] = self.latest_settings.binning.x
        #     header['YBINNING'] = self.latest_settings.binning.y

        try:
            hdu = fits.PrimaryHDU(data=self._image_array, header=header)
            hdu_list = fits.HDUList([hdu])
            hdu_list.writeto(
                self.latest_settings.image_path, checksum=True, overwrite=True
            )
            self.image_saved_event.set()
            logger.info(f"image saved to '{self.latest_settings.image_path}'")
        except Exception as ex:
            logger.error(
                f"failed to save fits file '{self.latest_settings.image_path}', {ex=}"
            )
        finally:
            self.end_activity(ImagerActivities.Saving)
            self.end_activity(ImagerActivities.Exposing)

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
                    self.cam_id, AsiControl.Temperature
                )

            try:
                exposure_status = asi.getExpStatus(self.cam_id)
                # logger.info(f"{op}: {exposure_status=} ({ASIExposureStatus(exposure_status).name})")
                if exposure_status == ASIExposureStatus.ASI_EXP_FAILED:
                    asi.stopExposure(self.cam_id)
                    self.end_activity(ImagerActivities.Exposing)
                    logger.error(
                        f"exposure failed with exposure_status={ASIExposureStatus(exposure_status).name}"
                    )
                elif exposure_status == ASIExposureStatus.ASI_EXP_SUCCESS:
                    buffer_size = self.width * self.height
                    if self.output_format == AsiOutputFormat.RAW16:
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
                            if self.output_format == AsiOutputFormat.RAW8
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
            val, _ = asi.getControlValue(self.cam_id, AsiControl.Temperature)
        except Exception as ex:
            self.errors.append(
                f"failed to get control AsiControl.Temperature ({AsiControl.Temperature}), {ex=}"
            )
        return val / 10.0

    def cooler(self, onoff: bool):
        self.errors = []
        try:
            asi.setControlValue(self.cam_id, AsiControl.CoolerOn, onoff, 0)
        except Exception as ex:
            self.errors.append(
                f"failed to set control AsiControl.ASI_COOLER_ON ({AsiControl.CoolerOn}), "
                + f"value={onoff}, {ex=}"
            )

    @property
    def cooler_power(self) -> float:
        self.errors = []
        try:
            val, _ = asi.getControlValue(self.cam_id, AsiControl.CoolPowerPerc)
        except Exception as ex:
            self.errors.append(
                "failed to get control AsiControl.ASI_COOLER_POWER_PERC "
                + f"({AsiControl.CoolPowerPerc}), {ex=}"
            )
        return val / 10.0

    def startup(self):
        # self.set_control(AsiControl.ASI_HIGH_SPEED_MODE, 1)
        self.set_control(AsiControl.TargetTemp, -5)
        self.set_control(AsiControl.CoolerOn, True)
        return super().startup()

    def shutdown(self):
        self.set_control(AsiControl.TargetTemp, 10)
        self.set_control(AsiControl.CoolerOn, False)
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
                        AsiOutputFormat.RAW8,
                        AsiOutputFormat.RAW16,
                    ]:
                        self.log_and_append(
                            f"invalid {self.imager_params['output_format']=}, must be one of: "
                            + f"{[AsiOutputFormat.RAW8, AsiOutputFormat.RAW16]}"
                        )
                    else:
                        self.output_format: AsiOutputFormat = self.imager_params[
                            "output_format"
                        ]
                else:
                    self.output_format = AsiOutputFormat.RAW16

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

        target_temp, _ = asi.getControlValue(self.cam_id, AsiControl.TargetTemp)
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
            format: AsiOutputFormat = self.output_format
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
            AsiControl.Exposure, int(settings.seconds * 1000000)
        )  # micro seconds

        assert self.default_settings is not None

        if settings.base_folder is None:
            settings.base_folder = self.default_settings.base_folder

        if settings.gain is None:
            settings.gain = self.default_settings.gain
        assert settings.gain is not None
        self.set_control(AsiControl.Gain, settings.gain)

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

    def set_control(self, control: AsiControl, value: int):
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
            val, _ = asi.getControlValue(self.cam_id, AsiControl.CoolerOn)
        except Exception as ex:
            self.errors.append(
                f"failed to get control AsiControl.CoolerOn ({AsiControl.CoolerOn}), {ex=}"
            )
        return bool(val)

    @cooler_on.setter
    def cooler_on(self, onoff: bool):
        self.errors = []
        try:
            asi.setControlValue(self.cam_id, AsiControl.CoolerOn, int(onoff), 0)
        except Exception as ex:
            self.errors.append(
                f"failed to set control AsiControl.CoolerOn ({AsiControl.CoolerOn}, {int(onoff)}), {ex=}"
            )

    @property
    def image_array(self):
        return self._image_array

    @image_array.setter
    def image_array(self, value):
        self._image_array = value

    def start_exposure_series(self, purpose: str | None = None):
        return super().start_exposure_series(purpose=purpose)

    def end_exposure_series(self, series):
        super().end_exposure_series(series)


if __name__ == "__main__":
    cam = ZWOImager(unit=None)
    # cam.make_pythonian_classes()
    cam.startup()
    series = cam.start_exposure_series(purpose="testing")
    cam.start_exposure(
        ImagerSettings.model_validate({"seconds": 5}, context={"imager": cam})
    )
    print(json.dumps(cam.status().model_dump(), indent=2))
    if cam.can_send_image_ready_event:
        cam.wait_for_image_ready()
        logger.info("got image ready event")

    if cam.can_send_image_saved_event:
        cam.wait_for_image_saved()
        logger.info("got image saved event")
    cam.end_exposure_series(series)
    exit(0)
