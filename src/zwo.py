import datetime
import logging
import socket
from enum import IntEnum, auto
from threading import Event, Thread
from typing import TYPE_CHECKING, Any

import numpy as np
import pyzwoasi as asi
from astropy.io import fits

from common.activities import ImagerActivities
from common.components import Component

# from common.config import Config
from common.dlipowerswitch import OutletDomain, SwitchedOutlet
from common.mast_logging import init_log
from common.utils import RepeatTimer, function_name, time_stamp
from imagers import ImagerBinning, ImagerExposure, ImagerInterface, ImagerRoi, ImagerSettings, ImagerStatus

logger = logging.Logger('mast-unit-imager-zwo')
init_log(logger)

if TYPE_CHECKING:
    from unit import Unit

class AsiControl(IntEnum):
    Gain = 0                    # Gain, default=200, auto-supported=1, auto=False
    Exposure = 1                # Exposure Time(us), default=10000, auto-supported=1, auto=False
    Offset = 2                  # offset, default=8, auto-supported=0, auto=False
    BandWidth = 3               # The total data transfer rate percentage, default=50, auto-supported=1, auto=True
    Flip = 4                    # Flip: 0->None 1->Horiz 2->Vert 3->Both, default=0, auto-supported=0, auto=False
    AutoExpMaxGain = 5          # Auto exposure maximum gain value, default=285, auto-supported=0, auto=False
    AutoExpMaxExpMS = 6         # Auto exposure maximum exposure value(unit ms), default=100, auto-supported=0, auto=False
    AutoExpTargetBrightness = 7 # Auto exposure target brightness value, default=100, auto-supported=0, auto=False
    HighSpeedMode = 8           # Is high speed mode:0->No 1->Yes, default=0, auto-supported=0, auto=False
    Temperature = 9             # Sensor temperature(degrees Celsius), default=20, writable=0, auto-supported=0, auto=False
    CoolPowerPerc = 10          # Cooler power percent, default=0, writable=0, auto-supported=0, auto=False
    TargetTemp = 11             # Target temperature(cool camera only), default=0, auto-supported=0, auto=False
    CoolerOn = 12               # turn on/off cooler(cool camera only), default=0, auto-supported=0, auto=False

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
    ASI_EXP_IDLE = 0            # idle, ready to start exposure
    ASI_EXP_WORKING = auto()    # exposure in progress
    ASI_EXP_SUCCESS = auto()    # exposure completed successfully, image can be read out
    ASI_EXP_FAILED = auto()     #  exposure failure, need to restart exposure

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

    def __init__(self, unit, imager_params: dict[str, Any] | None=None):
        Component.__init__(self)
        SwitchedOutlet.__init__(self, OutletDomain.Unit, outlet_name="Camera")
        self.unit = unit
        self.imager_params = imager_params or {}

        self.errors: list[str] = []
        self.latest_settings: ImagerSettings | None = None
        self.latest_exposure: ImagerExposure | None = None
        self.default_settings: ImagerSettings = ImagerSettings(seconds=0, base_folder='c:/temp')
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
        print("class AsiControl(IntEnum):")
        for control in range(n_controls):
            cap = asi.getControlCaps(self.cam_id, controlIndex=control)
            val, auto = asi.getControlValue(self.cam_id, controlType=cap.ControlType)
            print(f"\t{cap.Name.decode()} = {control}  # {cap.Description.decode()}, "
                        + f"default={cap.DefaultValue}, "
                        + f"writable={cap.IsWritable}, auto-supported={cap.IsAutoSupported}, auto={auto}")

    def save_in_thread(self):
        self.start_activity(ImagerActivities.Saving)
        assert(self.latest_settings is not None and self.latest_settings.roi is not None)
        assert(self.image_array is not None), "self.image_array is None"

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
        header["DATE-OBS"] = (datetime.datetime.now(datetime.UTC).isoformat(), "Observation datetime",)
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
            hdu_list.writeto(self.latest_settings.image_path, checksum=True, overwrite=True)
            self.image_saved_event.set()
            logger.info(f"image saved to '{self.latest_settings.image_path}'")
        except Exception as ex:
            logger.error(f"failed to save fits file '{self.latest_settings.image_path}', {ex=}")
        finally:
            self.end_activity(ImagerActivities.Saving)

        del self._image_array

    def on_timer(self):
        op = function_name()

        if self.is_active(ImagerActivities.Exposing):
            assert(self.latest_exposure is not None)
            assert(self.latest_settings is not None)
            if (datetime.datetime.now() - self.latest_exposure.start) >= \
                    datetime.timedelta(seconds=self.latest_settings.seconds/2):
                self.ccd_temp_at_mid_exposure, _ = asi.getControlValue(self.cam_id, AsiControl.Temperature)

            try:
                exposure_status = asi.getExpStatus(self.cam_id)
                # logger.info(f"{op}: {exposure_status=} ({ASIExposureStatus(exposure_status).name})")
                if exposure_status == ASIExposureStatus.ASI_EXP_FAILED:
                    asi.stopExposure(self.cam_id)
                    self.end_activity(ImagerActivities.Exposing)
                    logger.error(f"exposure failed with exposure_status={ASIExposureStatus(exposure_status).name}")
                elif exposure_status == ASIExposureStatus.ASI_EXP_SUCCESS:
                    self.end_activity(ImagerActivities.Exposing)
                    buffer_size = self.width * self.height
                    if self.output_format == AsiOutputFormat.RAW16:
                        buffer_size *= 2

                    self.start_activity(ImagerActivities.ReadingOut)
                    buffer = asi.getDataAfterExp(self.cam_id, bufferSize=buffer_size)
                    assert(self.latest_settings and self.latest_settings.roi)
                    self.image_array = np.ndarray(
                        buffer=buffer,
                        shape=(self.latest_settings.roi.height, self.latest_settings.roi.width),
                        dtype=np.uint16 if self.output_format == AsiOutputFormat.RAW8 else np.uint8)
                    self.end_activity(ImagerActivities.ReadingOut)
                    self.image_read_event.set()

                    if self.latest_settings and self.latest_settings.save:
                        Thread(name="zwo-image-saver", target=self.save_in_thread).start()

            except Exception as ex:
                logger.error(f"{op}: could not get exposure status, {ex=}")

    @property
    def can_image_to_memory(self) -> bool:
        return True

    def capture(self):
        # Implement capture logic for ZWO
        pass

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
            self.errors.append(f"failed to get control AsiControl.Temperature ({AsiControl.Temperature}), {ex=}")
        return val

    def cooler(self, onoff: bool):
        self.errors = []
        try:
            asi.setControlValue(self.cam_id, AsiControl.CoolerOn, onoff, 0)
        except Exception as ex:
            self.errors.append(f"failed to set control AsiControl.ASI_COOLER_ON ({AsiControl.CoolerOn}), "
                               + f"value={onoff}, {ex=}")

    @property
    def cooler_power(self) -> float:
        self.errors = []
        try:
            val, _ = asi.getControlValue(self.cam_id, AsiControl.CoolPowerPerc)
        except Exception as ex:
            self.errors.append("failed to get control AsiControl.ASI_COOLER_POWER_PERC "
                               + f"({AsiControl.CoolPowerPerc}), {ex=}")
        return val

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
                    if int(self.imager_params["output_format"]) \
                        not in [AsiOutputFormat.RAW8, AsiOutputFormat.RAW16]:
                        self.log_and_append(f"invalid {self.imager_params['output_format']=}, must be one of: "
                                        + f"{[AsiOutputFormat.RAW8, AsiOutputFormat.RAW16]}")
                    else:
                        self.output_format: AsiOutputFormat = self.imager_params['output_format']
                else:
                    self.output_format = AsiOutputFormat.RAW16

                # imager_conf = Config().get_unit().imager if not self.unit else self.unit.unit_conf.imager
                # The imager configuration in MongoDB is a bit muddy :-()

                self.default_settings = ImagerSettings(
                    seconds=0,
                    roi=ImagerRoi(x=0, y=0, width=self.width, height=self.height),
                    binning=ImagerBinning(x=1, y=1),
                    gain=85,
                    base_folder='c:/temp/zwo-images'
                )

                logger.info(f"ZWO ASI ID={self.cam_id}, SN={self.serial}, "
                            + f"model='{self.model}', "
                            + f"size={self.width}x{self.height}, "
                            + f"depth={self.depth} bits, pixel-size={self.pixel_size} micron, "
                            + f"output_format={self.output_format.name}")
            except Exception as ex:
                self.log_and_append(f"could not connect to {self.cam_id=}, {ex=}")
        else:
            try:
                asi.closeCamera(self.cam_id)
                self._connected = False
            except Exception as ex:
                self.log_and_append(f"could not closeCamera {self.cam_id=}, {ex=}")

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
                raise ValueError(f"bad binning={binning.x}x{binning.y}, horizontal and vertical must be equal")
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
                logger.warning(f"aligning roi width to 8 (subtracting {width % 8} bits)")
                width -= width % 8
            if height % 2 != 0:
                logger.warning(f"aligning roi height to 2 (subtracting {height % 2} bits)")
                height -= height % 8
            asi.setROIFormat(self.cam_id, width=width, height=height, binning=binning, imgType=format.value)
            asi.setStartPos(self.cam_id, x, y)
            logger.info(f"set_format(roi=({x=}, {y=}, {width=}, {height=}), {binning=}, output_format={format.name})")
        except Exception as ex:
            self.log_and_append(f"failed to set format to {x=},{y=},{width=},{height=},{binning=},{format.name=}: {ex=}")

    def start_exposure(self, settings: ImagerSettings):
        self.errors = []
        self.set_control(AsiControl.Exposure, int(settings.seconds * 1000000))  # micro seconds

        assert(self.default_settings is not None)

        if settings.base_folder is None:
            settings.base_folder = self.default_settings.base_folder

        if settings.gain is None:
            settings.gain = self.default_settings.gain
        assert(settings.gain is not None)
        self.set_control(AsiControl.Gain, settings.gain)

        if settings.roi is None:
            settings.roi = self.default_settings.roi
        self.set_format(settings)

        self.latest_settings = settings.model_copy()

        try:
            self.start_activity(ImagerActivities.Exposing)
            asi.startExposure(self.cam_id, isDark=False)
            self.latest_exposure = ImagerExposure(
                file=settings.image_path,
                seconds=settings.seconds,
                date=datetime.datetime.now(datetime.UTC).isoformat())
            logger.info(f"started a {settings.seconds} seconds exposure")
        except Exception as ex:
            self.log_and_append(f"failed to start exposure, {ex=}")

    def set_control(self, control: AsiControl, value: int):
        try:
            asi.setControlValue(self.cam_id, controlType=control, value=value, auto=0)
            logger.info(f"set_control('{control.name}', value={value})")
        except Exception as ex:
            self.log_and_append(f"failed to set_control('{control.name}' ({control.value}), value={value}), {ex=}")
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
            self.errors.append(f"failed to get control AsiControl.CoolerOn ({AsiControl.CoolerOn}), {ex=}")
        return bool(val)

    @cooler_on.setter
    def cooler_on(self, onoff: bool):
        self.errors = []
        try:
            asi.setControlValue(self.cam_id, AsiControl.CoolerOn, int(onoff), 0)
        except Exception as ex:
            self.errors.append(f"failed to set control AsiControl.CoolerOn ({AsiControl.CoolerOn}, {int(onoff)}), {ex=}")

    @property
    def image_array(self):
        return self._image_array

    @image_array.setter
    def image_array(self, value):
        self._image_array = value

if __name__ == "__main__":
    cam = ZWOImager(unit=None)
    cam.startup()
    cam.start_exposure(ImagerSettings(
        seconds=5,
        base_folder="c:/temp/zwo_test_images",
        binning=ImagerBinning(x=1, y=1),
        gain=cam.default_settings.gain,
        roi=cam.default_settings.roi,
        ))
    cam.wait_for_image_ready()
    logger.info("got image ready event")
    cam.wait_for_image_saved()
    logger.info("got image saved event")
    exit(0)
