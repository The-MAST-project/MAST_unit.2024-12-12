import ctypes
import logging
from ctypes import c_int
from enum import IntEnum, auto
from typing import Literal, get_args

logger = logging.Logger("ASI")


#
# Extracted at runtime from the ZWO ASI SDK for camera model: ZWO ASI294MM Pro
#
class Control(IntEnum):
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


ControlDict: dict[Control, dict] = {
    Control.Gain: {
        "description": "Gain",
        "min_value": 0,
        "max_value": 570,
        "default": 200,
        "is_writable": 1,
        "is_auto_supported": 1,
        "control_type": 0,
        "auto": False,
    },
    Control.Exposure: {
        "description": "Exposure Time(us)",
        "min_value": 32,
        "max_value": 2000000000,
        "default": 10000,
        "is_writable": 1,
        "is_auto_supported": 1,
        "control_type": 1,
        "auto": False,
    },
    Control.Offset: {
        "description": "offset",
        "min_value": 0,
        "max_value": 80,
        "default": 8,
        "is_writable": 1,
        "is_auto_supported": 0,
        "control_type": 5,
        "auto": False,
    },
    Control.BandWidth: {
        "description": "The total data transfer rate percentage",
        "min_value": 40,
        "max_value": 100,
        "default": 50,
        "is_writable": 1,
        "is_auto_supported": 1,
        "control_type": 6,
        "auto": True,
    },
    Control.Flip: {
        "description": "Flip: 0->None 1->Horiz 2->Vert 3->Both",
        "min_value": 0,
        "max_value": 3,
        "default": 0,
        "is_writable": 1,
        "is_auto_supported": 0,
        "control_type": 9,
        "auto": False,
    },
    Control.AutoExpMaxGain: {
        "description": "Auto exposure maximum gain value",
        "min_value": 0,
        "max_value": 570,
        "default": 285,
        "is_writable": 1,
        "is_auto_supported": 0,
        "control_type": 10,
        "auto": False,
    },
    Control.AutoExpMaxExpMS: {
        "description": "Auto exposure maximum exposure value(unit ms)",
        "min_value": 1,
        "max_value": 60000,
        "default": 100,
        "is_writable": 1,
        "is_auto_supported": 0,
        "control_type": 11,
        "auto": False,
    },
    Control.AutoExpTargetBrightness: {
        "description": "Auto exposure target brightness value",
        "min_value": 50,
        "max_value": 160,
        "default": 100,
        "is_writable": 1,
        "is_auto_supported": 0,
        "control_type": 12,
        "auto": False,
    },
    Control.HighSpeedMode: {
        "description": "Is high speed mode:0->No 1->Yes",
        "min_value": 0,
        "max_value": 1,
        "default": 0,
        "is_writable": 1,
        "is_auto_supported": 0,
        "control_type": 14,
        "auto": False,
    },
    Control.Temperature: {
        "description": "Sensor temperature(degrees Celsius)",
        "min_value": -500,
        "max_value": 1000,
        "default": 20,
        "is_writable": 0,
        "is_auto_supported": 0,
        "control_type": 8,
        "auto": False,
    },
    Control.CoolPowerPerc: {
        "description": "Cooler power percent",
        "min_value": 0,
        "max_value": 100,
        "default": 0,
        "is_writable": 0,
        "is_auto_supported": 0,
        "control_type": 15,
        "auto": False,
    },
    Control.TargetTemp: {
        "description": "Target temperature(cool camera only)",
        "min_value": -40,
        "max_value": 30,
        "default": 0,
        "is_writable": 1,
        "is_auto_supported": 0,
        "control_type": 16,
        "auto": False,
    },
    Control.CoolerOn: {
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


class OutputFormat(IntEnum):
    RAW8 = 0
    RGB24 = 1
    RAW16 = 2
    Y8 = 3

    @staticmethod
    def from_string(s: str):
        s = s.lower()
        if s in get_args(ValidOutputFormats):
            if s == "raw8":
                return OutputFormat.RAW8
            elif s == "raw16":
                return OutputFormat.RAW16
            else:
                raise ValueError(f"OutputFormat.from_string: '{s}' not in {get_args(ValidOutputFormats)}")
        else:
            raise ValueError(f"OutputFormat.from_string: '{s}' not in {get_args(ValidOutputFormats)}")

ValidOutputFormats = Literal["raw8", "raw16"]


class ExposureStatus(IntEnum):
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


ASI_MAX_CAMERA_NAME = 64


# ASI_CAMERA_INFO structure
class ASI_CAMERA_INFO(ctypes.Structure):  # noqa: N801
    _fields_ = [
        ("Name", ctypes.c_char * ASI_MAX_CAMERA_NAME),
        ("CameraID", c_int),
        ("MaxHeight", c_int),
        ("MaxWidth", c_int),
        ("IsColorCam", c_int),
        ("BayerPattern", c_int),
        ("SupportedBins", c_int * 16),
        ("SupportedVideoFormat", c_int * 8),
        ("SupportedPreviewFormat", c_int * 8),
        ("PixelSize", ctypes.c_float),
        ("MechanicalShutter", c_int),
        ("ST4Port", c_int),
        ("IsCoolerCam", c_int),
        ("IsUSB3Host", c_int),
        ("IsUSB3Camera", c_int),
        ("ElecPerADU", ctypes.c_float),
        ("BitDepth", c_int),
        ("IsTriggerCam", c_int),
        ("Unused", c_int * 8),
        ("CameraSN", ctypes.c_char * 16),
        ("PortType", c_int),
        ("DevPath", ctypes.c_char * 32),
        ("ProductID", c_int),
        ("VendorID", c_int),
    ]


def gain_absolute_to_percent(value) -> float:
    d = ControlDict[Control.Gain]
    min = d["min_value"]
    max = d["max_value"]
    ret = ((value - min) / (max - min)) * 100
    # print(f"{max=}, {min=}, {value=}, {ret=}")
    return ret


def gain_percent_to_absolute(percent: float) -> int:
    d = ControlDict[Control.Gain]
    min = d["min_value"]
    max = d["max_value"]
    ret = int(min + (max - min) * (percent / 100))
    # print(f"{max=}, {min=}, {percent=:2.0f}%, {ret=}")
    return ret


def make_pythonian_classes():
    """
    Makes pythonian classes from ASI internals (controls, etc.)
    """
    import pyzwoasi as asi

    n_cameras = asi.getNumOfConnectedCameras()
    logger.info(f"found {n_cameras} ASI camera(s), SDK={asi.getSDKVersion()}")

    if n_cameras < 1:
        logger.error("no ASI cameras")
        exit(0)

    cam_id = 0

    n_controls = asi.getNumOfControls(cam_id)
    enum_lines = ["class Control(IntEnum):"]
    dict_lines = ["ControlDict: dict[AsiControl, dict] = {"]

    info = asi.getCameraProperty(cam_id)
    model = info.Name.decode()

    for control in range(n_controls):
        cap = asi.getControlCaps(cam_id, controlIndex=control)
        val, auto = asi.getControlValue(cam_id, controlType=cap.ControlType)
        line = f"{cap.Name.decode()} = {cap.ControlType}"
        enum_lines.append(
            f"    {line}{' ' * (30 - len(line))}# {cap.Description.decode()}, "
        )
        dict_lines.append(f"    Control.{cap.Name.decode()}: {{")
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
        f"#\n# Extracted at runtime from the ZWO ASI SDK for camera model: {model}\n#"
    )
    for line in enum_lines:
        print(line)
    print()
    for line in dict_lines:
        print(line)
    print()


def list_cameras():
    import pyzwoasi as asi

    n_cameras = asi.getNumOfConnectedCameras()
    print()
    print(f"found {n_cameras} ASI camera(s), SDK='{asi.getSDKVersion()}'")

    # for id in range(n_cameras):
    #     info = asi.getCameraProperty(id)

    #     supported = asi.cameraCheck(info.VendorID, info.ProductID)
    #     print(
    #         f"{id=:2}: model={info.Name.decode()}, width={info.MaxWidth}, "
    #           + f"height={info.MaxHeight}, depth={info.BitDepth}, {supported=}"
    #     )


if __name__ == "__main__":
    # make_pythonian_classes()
    list_cameras()
