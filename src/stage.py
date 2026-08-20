import datetime
import os
import platform
import sys
import threading
import time
from collections import deque
from enum import Enum, IntEnum, auto
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from fastapi.routing import APIRouter

from common.activities import StageActivities
from common.canonical import CanonicalResponse, CanonicalResponse_Ok
from common.config import Config
from common.config.rois import FcuVersion
from common.const import Const
from common.dlipowerswitch import OutletDomain, SwitchedOutlet
from common.interfaces.components import Component
from common.mast_logging import get_logger
from common.models.statuses import StageStatus
from common.utils import RepeatTimer, Timeout, boxed_log, function_name, time_stamp

if TYPE_CHECKING:
    from unit import Unit

logger = get_logger(__name__)
# os.environ["XILOG"] = "C:/temp/ximc.log"  # Enables logging for ximc library.

XIMC_VERSION = "2.13.6"
ximc_top_dir = Path(__file__).parent / "Standa" / f"ximc-{XIMC_VERSION}" / "ximc"

for path in [
    ximc_top_dir / "crossplatform" / "wrappers" / "python",  # examples
    ximc_top_dir / "python-profiles" / "STANDA",  # profiles
]:
    sys.path.append(str(path))

if platform.system() == "Windows":
    # Determining the directory with dependencies for windows depending on the bit depth.
    arch_dir = "win64" if "64" in platform.architecture()[0] else "win32"
    lib_dir = ximc_top_dir / arch_dir  # lib directory for ximc library
    if not lib_dir.exists():
        raise FileNotFoundError(f"Directory with ximc library not found: {lib_dir=}. ")
    os.add_dll_directory(str(lib_dir))  # add dll path into an environment variable

    from pyximc import (
        POINTER,
        BorderFlags,
        EnumerateFlags,  # type: ignore[name]
        MvcmdStatus,
        Result,  # type: ignore[name]
        StateFlags,
        byref,
        c_char_p,
        c_int,
        cast,
        device_information_t,
        edges_settings_t,
        secure_settings_t,
        serial_number_t,
        status_t,
        string_at,
    )
    from pyximc import lib as ximclib  # type: ignore[name]

RESULT_MAP = {
    Result.Ok: "Ok",
    Result.Error: "error",
    Result.NotImplemented: "NotImplemented",
    Result.ValueError: "ValueError",
    Result.NoDevice: "NoDevice",
}


class StageDirection(IntEnum):
    Up = auto()
    Down = auto()


# The values below are the wire format, and the member names are Python identifiers with no
# meaning outside this file. Keeping those two apart is the fix for #85: the names used to
# leak into the HTTP contract, leaving three of the five presets unreachable -- request
# validation advertised the lowercase values while the handler resolved by member name, so
# `spec` passed validation and then failed the lookup, `Spec` was rejected by validation
# before the handler saw it, and `mid` failed because the member is `Middle`. Both
# operationally meaningful presets were among the unreachable ones.
#
# Plain strings rather than the 1-tuples used previously, which made the OpenAPI schema an
# *array* enum (`[["sky"], ["spec"], ...]`) and left `value` a tuple no caller could compare
# against.
class StagePresetPosition(Enum):
    """Preset stage positions: `sky`, `spec`, `min`, `mid`, `max`."""

    Sky = "sky"
    Spec = "spec"
    Min = "min"
    Middle = "mid"
    Max = "max"


#: Where the stage is sent on startup. Deliberately not an enum member: as an alias of `Sky`
#: it published a duplicate "sky" in the OpenAPI enum, and it is a policy choice rather than
#: a position anyone can command.
STARTUP_PRESET = StagePresetPosition.Sky

#: The preset names the API accepts, for anyone building a UI over it.
stage_position_names: list[str] = [p.value for p in StagePresetPosition]

stage_direction_str2int_dict: dict = {
    "Up": StageDirection.Up,
    "Down": StageDirection.Down,
}


class Stage(Component, SwitchedOutlet):
    _instance = None
    _initialized = False

    state_flags_dict: ClassVar[dict[StateFlags, str]] = {
        StateFlags.STATE_ERRC: "STATE_ERRC",
        StateFlags.STATE_ERRV: "STATE_ERRV",
        StateFlags.STATE_ERRD: "STATE_ERRD",
        StateFlags.STATE_IS_HOMED: "STATE_IS_HOMED",
        StateFlags.STATE_EEPROM_CONNECTED: "STATE_EEPROM_CONNECTED",
        StateFlags.STATE_ALARM: "STATE_ALARM",
        StateFlags.STATE_CTP_ERROR: "STATE_CTP_ERROR",
        StateFlags.STATE_POWER_OVERHEAT: "STATE_POWER_OVERHEAT",
        StateFlags.STATE_CONTROLLER_OVERHEAT: "STATE_CONTROLLER_OVERHEAT",
        StateFlags.STATE_OVERLOAD_POWER_VOLTAGE: "STATE_OVERLOAD_POWER_VOLTAGE",
        StateFlags.STATE_OVERLOAD_POWER_CURRENT: "STATE_OVERLOAD_POWER_CURRENT",
        StateFlags.STATE_OVERLOAD_USB_VOLTAGE: "STATE_OVERLOAD_USB_VOLTAGE",
        StateFlags.STATE_LOW_USB_VOLTAGE: "STATE_LOW_USB_VOLTAGE",
        StateFlags.STATE_OVERLOAD_USB_CURRENT: "STATE_OVERLOAD_USB_CURRENT",
        StateFlags.STATE_BORDERS_SWAP_MISSET: "STATE_BORDERS_SWAP_MISSET",
        StateFlags.STATE_LOW_POWER_VOLTAGE: "STATE_LOW_POWER_VOLTAGE",
        StateFlags.STATE_H_BRIDGE_FAULT: "STATE_H_BRIDGE_FAULT",
        StateFlags.STATE_WINDING_RES_MISMATCH: "STATE_WINDING_RES_MISMATCH",
        StateFlags.STATE_ENCODER_FAULT: "STATE_ENCODER_FAULT",
        StateFlags.STATE_ENGINE_RESPONSE_ERROR: "STATE_ENGINE_RESPONSE_ERROR",
        StateFlags.STATE_EXTIO_ALARM: "STATE_EXTIO_ALARM",
    }

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, unit: "Unit"):  # type: ignore  # noqa: C901
        if self._initialized:
            return

        op = "Stage.__init__"
        self.unit = unit
        if unit and unit.unit_conf and unit.unit_conf.stage:
            self.conf = unit.unit_conf.stage
        else:
            unit_conf = Config().get_unit()
            if unit_conf and unit_conf.stage:
                self.conf = unit_conf.stage
            else:
                raise Exception(f"{op}: cannot get stage configuration")

        SwitchedOutlet.__init__(self, OutletDomain.UnitOutlets, outlet_name="Stage")
        Component.__init__(self, StageActivities)

        self.errors: list[str] = []
        self.device = None
        self.ticks_at_start: int | None = None
        self.ticks_at_target: int | None = None
        self.motion_start_time: datetime.datetime | None = None
        self.timer: RepeatTimer | None = None
        self.device_uri: str | None = None
        self._position: int | None = None
        self.is_moving: bool = False
        self.target: int | None = None
        self.min_travel: int | None = None
        self.max_travel: int | None = None

        self.info = {}
        self._was_shut_down = False
        self._currently_operational = False
        self._why_not_currently_operational = ["stage not yet initialized"]

        if not self.is_on():
            self.power_on()
            time.sleep(3)

        self.presets = {
            StagePresetPosition.Sky: self.conf.presets.sky,
            StagePresetPosition.Spec: self.conf.presets.spec,
        }

        # This is device search and enumeration with probing. It gives more information about devices.
        probe_flags = EnumerateFlags.ENUMERATE_PROBE
        ports = self.find_ximc_ports()
        hint = f"addr={ports[0]}" if ports else "addr="
        logger.info(f"{op}: using {hint=} as enumeration hint")
        enum_hints = c_char_p(hint.encode())  # type: ignore
        assert ximclib
        self.device = -1
        try:
            with Timeout(10) as timeout:
                dev_enum = timeout.run(ximclib.enumerate_devices, probe_flags, enum_hints)
        except TimeoutError as ex:
            logger.error(f"{op}: timeout while enumerating devices: {ex}")
            return

        dev_count = ximclib.get_device_count(dev_enum)
        if dev_count == 0:
            logger.error(f"{op}: no device detected ({dev_count=})")
            return

        self.device_uri = ximclib.get_device_name(dev_enum, 0)
        ximclib.free_enumerate_devices(dev_enum)
        self.device = ximclib.open_device(self.device_uri)

        if not self.detected:
            self._currently_operational = False
            self._why_not_currently_operational = ["No device detected"]
            logger.error(f"{op}: no device detected ({self.device=})")
            return

        # these two are set by the ontimer() method
        self._currently_operational = True
        self._why_not_currently_operational = []

        # stage_information = stage_information_t()
        # result = ximclib.get_stage_information(self.device, byref(stage_information))

        # if result == Result.Ok:
        #     self.stage_model = repr(
        #         string_at(stage_information.PartNumber).decode()
        #     ).replace("'", "")

        #     match self.stage_model:
        #         case "8MT167-25LS-MEn1":
        #             self.fcu_version = FcuVersion.v1
        #         case "8MT173-20DCE2":
        #             self.fcu_version = FcuVersion.v2
        #         case _:
        #             raise Exception(f"{op}: unsupported stage model '{self.stage_model}'")
        # else:
        #     raise Exception(f"{op}: cannot get controller name ({result=})")

        match self.conf.model:
            case "8MT167-25LS-MEn1":
                self.fcu_version = FcuVersion.v1
            case "8MT173-20DCE2":
                self.fcu_version = FcuVersion.v2
            case _:
                logger.warning(f"{op}: unsupported stage model in config: '{self.conf.model}'")
                raise Exception(f"{op}: unsupported stage model '{self.conf.model}'")
        self.stage_model = self.conf.model

        serial_number = serial_number_t()
        result = ximclib.get_serial_number(self.device, byref(serial_number))
        if result == Result.Ok:
            self.serial_number = serial_number.SN
        else:
            logger.warning(f"{op}: cannot get serial number ({result=})")

        # self.set_profile()  # FUTURE: set motion profile parameters for known stage models

        x_device_information = device_information_t()
        result = ximclib.get_device_information(self.device, byref(x_device_information))

        if result == Result.Ok:
            comport = str(self.device_uri)
            comport = comport[comport.find("COM") :].removesuffix("'")

            self.info["port"] = comport
            self.info["controller"] = repr(string_at(x_device_information.Manufacturer).decode()).replace("'", "")
            self.info["product"] = repr(string_at(x_device_information.ProductDescription).decode()).replace("'", "")
            self.info["version"] = (
                f"{x_device_information.Major!r}.{x_device_information.Minor!r}" + f".{x_device_information.Release!r}"
            )

        x_edges_settings = edges_settings_t()
        result = ximclib.get_edges_settings(self.device, byref(x_edges_settings))
        if result == Result.Ok:
            if x_edges_settings.BorderFlags & BorderFlags.BORDER_IS_ENCODER:
                self.min_travel = x_edges_settings.LeftBorder
                self.max_travel = x_edges_settings.RightBorder
                self.border_by = "encoder values"
            else:
                self.border_by = "limit switches"
                match self.fcu_version:
                    case FcuVersion.v1:
                        self.min_travel = 0
                        self.max_travel = 195000
                    case FcuVersion.v2:
                        self.min_travel = 0
                        self.max_travel = 343544
        else:
            raise Exception(f"{op}: cannot get edges settings ({result=})")

        self.secure_settings: secure_settings_t = secure_settings_t()
        result = ximclib.get_secure_settings(self.device, byref(self.secure_settings))
        if result != Result.Ok:
            logger.warning(f"{op}: cannot get secure settings ({result=})")

        status = status_t()
        result = ximclib.get_status(self.device, byref(status))
        if result == Result.Ok:
            if status.Flags & StateFlags.STATE_EEPROM_CONNECTED:
                self.info["eeprom_connected"] = True
            else:
                self.info["eeprom_connected"] = False

        self.device_info = (
            f"port='{comport}', manufacturer='{self.info['controller']}', product='{self.info['product']}', "
            + f"version='{self.info['version']}', model='{self.stage_model}', serial={self.serial_number}, "
            + f"fcu_version='{self.fcu_version.value}', "
            + f"EEPROM connected={self.info.get('eeprom_connected', 'unknown')}, "
            + f"range={self.min_travel}..{self.max_travel} (borders by: {self.border_by}), "
            + f"close_enough={self.conf.close_enough}"
        )
        self.stage_lock = threading.Lock()

        if self.min_travel is not None and self.max_travel is not None:
            self.presets[StagePresetPosition.Min] = self.min_travel
            self.presets[StagePresetPosition.Max] = self.max_travel
            self.presets[StagePresetPosition.Middle] = int((self.max_travel - self.min_travel) / 2)

        # get initial values from the hardware
        hw_status = status_t()
        with self.stage_lock:
            result = ximclib.get_status(self.device, byref(hw_status))
        if result == Result.Ok:
            self._position = hw_status.CurPosition
            self.is_moving = hw_status.MvCmdSts & MvcmdStatus.MVCMD_RUNNING

        self.latest_positions = deque(maxlen=3)

        self.timer = RepeatTimer(2, function=self.ontimer)
        self.timer.name = "stage-timer-thread"
        self.timer.start()

        logger.info(f"detected: {self.device_info}")

        self.home()
        self._initialized = True

    def __del__(self):
        logger.info(f"Closing {self.device=}")
        assert ximclib
        assert self.device
        ximclib.close_device(byref(cast(self.device, POINTER(c_int))))

        assert self.timer is not None
        self.timer.cancel()

    def __repr__(self):
        return f"<Stage device={self.device}>"

    def home(self):
        """
        Homes the stage
        """

        op = function_name()
        with self.stage_lock:
            assert ximclib
            try:
                with Timeout(60) as timeout:
                    result = timeout.run(ximclib.command_homezero, self.device)
                if result == Result.Ok:
                    self.start_activity(StageActivities.Homing)
            except TimeoutError as ex:
                logger.error(f"{op}: timeout during homing: {ex}")

    def find_ximc_ports(self):
        from serial.tools import list_ports

        ports = list_ports.comports()
        ximc_ports = []
        for port in ports:
            if "XIMC" in port.description:
                ximc_ports.append(port.device)  # e.g., 'COM7'
        return ximc_ports

    def set_profile(self):

        if self.stage_model is None:
            logger.warning("Stage model is unknown, cannot set profile")
            return

        file_path = Path(ximc_top_dir) / "python-profiles" / "Standa" / f"{self.stage_model}.py"
        if not file_path.exists():
            logger.warning(f"Profile file '{file_path}' missing, cannot set profile for stage model '{self.stage_model}'")
            return
        profile_setter_function = f"set_profile_{self.stage_model.replace('-', '_')}"
        with open(file_path) as f:
            code = f.read()

        preamble = """
from pyximc import *
"""

        namespace = {}
        try:
            exec(compile(preamble + code, f"{self.stage_model}", "exec"), namespace)
        except Exception as e:  # noqa: BLE001 -- executes a profile file: arbitrary code, so arbitrary exceptions
            logger.warning(f"Error executing profile file '{file_path.as_posix()}': {e}")
            return

        profile_setter = namespace.get(profile_setter_function)
        if profile_setter is None:
            logger.warning(f"Profile setter function '{profile_setter_function}' not found in '{file_path.as_posix()}'")
            return

        try:
            result = profile_setter(ximclib, self.device)
            logger.info(
                f"set profile for stage model '{self.stage_model}' from file '{file_path.as_posix()}', "
                + f"result: {RESULT_MAP.get(result, result)}"
            )
        except Exception:
            logger.exception(
                f"error setting profile for stage model '{self.stage_model}' " + f"from file '{file_path.as_posix()}'"
            )

    def position_sampler(self):
        return self.position

    @property
    def connected(self) -> bool:
        return self.detected

    @connected.setter
    def connected(self, value):

        if not self.is_on():
            return

        assert ximclib
        assert self.device
        if value:
            self.device = ximclib.open_device(self.device_uri)
        else:
            ximclib.close_device(byref(cast(self.device, POINTER(c_int))))
            self.device = -1

        logger.info(f"connected = {value} => {self.connected}")

    def connect(self):
        """
        Connects to the **MAST** stage controller
        """

        if not self.is_on():
            self.power_on()
        self.connected = True
        return CanonicalResponse_Ok

    def disconnect(self):
        """
        Disconnects from the **MAST** stage controller
        """

        if self.is_on():
            self.connected = False
        return CanonicalResponse_Ok

    def endpoint_startup(self):
        return self.startup()

    def startup(self):
        """
        Startup routine for the **MAST** stage.  Makes it ``operational``:
        * If not powered, powers it ON
        * If not connected, connects to the controller
        * If the stage is not at operational position, it is moved
        """

        if not self.is_on():
            self.power_on()
        if not self.connected:
            self.connect()
        self._was_shut_down = False
        if not self.at_preset(StagePresetPosition.Sky):
            self.start_activity(StageActivities.StartingUp)
            self.move_to_preset(StagePresetPosition.Sky)
        return CanonicalResponse_Ok

    def endpoint_shutdown(self):
        return self.shutdown()

    def shutdown(self):
        """
        Shutdown routine for the **MAST** stage.  Makes it ``idle``
        """
        self.disconnect()
        self._was_shut_down = True
        return CanonicalResponse_Ok

    @property
    def is_shutting_down(self) -> bool:
        return False

    def powerdown(self):
        if not self._was_shut_down:
            self.shutdown()
        self.power_off()

    def at_preset(self, preset: StagePresetPosition) -> bool:
        current_position = self.position
        if current_position is not None:
            for p in self.presets:
                if p == preset and self.close_enough(self.presets[p]):
                    return True
        return False

    @property
    def position(self) -> int | None:
        return self._position

    @position.setter
    def position(self, value):
        """Delegates to `move_absolute`, raising on refusal to keep the property contract.

        This was a parallel implementation of the same move, and it had drifted: no
        `detected` check, no travel-limit range check, and `command_move(device, value)`
        with two arguments where the C signature is `(id, Position, uPosition)`. `pyximc`
        binds the library as a bare `WinDLL` with no `argtypes`, so ctypes never caught the
        missing argument and the controller was handed an undefined micro-step offset.

        It has no callers in this repository -- it exists for interactive use, which is
        exactly why it went unnoticed.
        """
        response = self.move_absolute(value)
        if response is not None and response.failed:
            raise ValueError(f"cannot move to {value}: {'; '.join(response.errors or [])}")

    def endpoint_status(self) -> StageStatus:
        return self.status()

    def at_preset_name(self) -> str | None:
        """The preset the stage is currently parked at, as the API spells it.

        `value`, not `name.lower()`: the latter reported "middle" for a preset the API calls
        "mid", so what status said did not round-trip into `move_to_preset` (#85).
        """
        if not self.detected:
            return None
        for preset, position in self.presets.items():
            if self.close_enough(position):
                return preset.value
        return None

    def target_preset_name(self) -> str:
        """The preset being moved to, as the API spells it; the raw target if it is not one.

        The comparison is against the preset's *position*. It used to be against the enum's
        value -- a 1-tuple, and now a string -- neither of which can equal the integer
        `target`, so this never once resolved to a preset name.
        """
        if self.target is not None:
            for preset, position in self.presets.items():
                if self.target == position:
                    return preset.value
        return f"{self.target}"

    def status(self) -> StageStatus:
        at_preset = self.at_preset_name()
        target_verbal = self.target_preset_name()

        return StageStatus(
            **self.power_status().model_dump(),
            **self.component_status().model_dump(),
            info=self.info,
            presets=self.presets,
            position=self.position,
            at_preset=at_preset,
            target=self.target,
            target_verbal=target_verbal,
            date=time_stamp(),
        )

    def close_enough(self, target):
        # logger.info(f"{self._position=}, {target=}")
        return abs(self._position - target) <= self.conf.close_enough

    @property
    def is_stationary(self) -> bool:
        """
        Returns True if the stage was at the same position for the last
          self.latest_positions.maxlen readings (every 2 seconds by ontimer).
        """
        return self.latest_positions.count == self.latest_positions.maxlen and all(
            pos == self.latest_positions[0] for pos in self.latest_positions
        )

    def check_current_status(self, status: status_t):  # noqa: C901
        """
        Checks the current status of the controller,
            updates currently_operational and why_not_currently_operational accordingly,
            and logs any relevant information or errors.
        """
        op = function_name()
        assert ximclib

        self._why_not_currently_operational = []
        self._currently_operational = True

        secure_settings = secure_settings_t()
        with self.stage_lock:
            result = ximclib.get_secure_settings(self.device, byref(secure_settings))
            if result == Result.Ok:
                if status.CurT > secure_settings.CriticalT:
                    logger.warning(
                        f"{op}: WARNING: controller temperature {(status.CurT / 10):.2f}C "
                        + f"exceeds critical temperature {(secure_settings.CriticalT / 10):.2f}C"
                    )
                    logger.debug(f"{op}: secure settings: {secure_settings:x}")
            else:
                logger.warning(f"{op}: cannot get secure settings ({result=})")

        controller_errors = []
        for error_bit in [
            StateFlags.STATE_ERRC,
            StateFlags.STATE_ERRV,
            StateFlags.STATE_ERRD,
        ]:
            if status.Flags & error_bit:
                controller_errors.append(self.state_flags_dict.get(error_bit, f"Unknown error bit 0x{error_bit:08X}"))
        if controller_errors:
            logger.error(f"{op}: controller errors detected: {', '.join(controller_errors)}")

        security_error = status.Flags & StateFlags.STATE_SECUR
        if security_error != 0:
            security_errors = []
            for bit in [
                StateFlags.STATE_CTP_ERROR,
                StateFlags.STATE_POWER_OVERHEAT,
                StateFlags.STATE_CONTROLLER_OVERHEAT,
                StateFlags.STATE_OVERLOAD_POWER_VOLTAGE,
                StateFlags.STATE_OVERLOAD_POWER_CURRENT,
                StateFlags.STATE_OVERLOAD_USB_VOLTAGE,
                StateFlags.STATE_LOW_USB_VOLTAGE,
                StateFlags.STATE_OVERLOAD_USB_CURRENT,
                StateFlags.STATE_BORDERS_SWAP_MISSET,
                StateFlags.STATE_LOW_POWER_VOLTAGE,
                StateFlags.STATE_H_BRIDGE_FAULT,
                StateFlags.STATE_WINDING_RES_MISMATCH,
                StateFlags.STATE_ENCODER_FAULT,
                StateFlags.STATE_ENGINE_RESPONSE_ERROR,
                StateFlags.STATE_EXTIO_ALARM,
            ]:
                if (security_error & bit) != 0:
                    security_errors.append(self.state_flags_dict.get(bit, f"Unknown security error bit 0x{bit:08X}"))

            if security_errors:
                logger.error(f"{op}: security errors detected: {', '.join(security_errors)}")

            if (security_error & StateFlags.STATE_ALARM) != 0:
                if (security_error & (StateFlags.STATE_POWER_OVERHEAT | StateFlags.STATE_CONTROLLER_OVERHEAT)) != 0:
                    if status.CurT > self.secure_settings.CriticalT:
                        logger.error(
                            f"{op}: controller temperature {(status.CurT / 10):.2f}C exceeds critical temperature "
                            + f"{(self.secure_settings.CriticalT / 10):.2f}C"
                        )
                        self.start_activity(StageActivities.Overheating, existing_ok=True)
                    else:
                        pass  # TODO: Check other ALARM conditions
            else:
                # STATE_ALARM was set but is no longer. If we were previously in Overheating activity, end it.
                if self.is_active(StageActivities.Overheating):
                    logger.info(f"{op}: controller temperature back to normal, ending Overheating activity")
                    result = ximclib.command_stop(self.device)
                    if result != Result.Ok:
                        logger.error(f"{op}: failed to stop stage after overheating: {RESULT_MAP.get(result, result)}")
                    self.end_activity(StageActivities.Overheating)

    def ontimer(self):  # noqa: C901
        if self.unit and self.unit.unit_shutdown_event.is_set():
            if self.timer:
                self.timer.cancel()
            return

        if not self.connected:
            return

        if not self.detected or not self.stage_lock:
            return

        op = function_name()
        assert ximclib
        hw_status = status_t()
        with self.stage_lock:
            result = ximclib.get_status(self.device, byref(hw_status))
        if result != Result.Ok:
            logger.error(f"{op}: could not get_status(), {result=} ({RESULT_MAP[result]})")
            return

        self.check_current_status(hw_status)

        self._position = hw_status.CurPosition
        self.latest_positions.append(self._position)

        self.is_moving = (hw_status.MvCmdSts & MvcmdStatus.MVCMD_RUNNING) != 0

        if not self.is_moving:
            if self.is_active(StageActivities.Moving):
                if self.is_stationary and not self.close_enough(self.target):
                    #
                    # The stage has been at the same position for a while,
                    # but it is not close enough to the target position.
                    # Try to nudge it to the target position.
                    #
                    boxed_log(
                        logger,
                        [
                            "Stage is stationary, but not close enough to target: ",
                            f"{self.position} != {self.target} (CLOSE_ENOUGH={self.conf.close_enough})",
                            f"Moving to {self.target} again",
                        ],
                        center=True,
                    )
                    assert self.target is not None
                    self.move_absolute(self.target)

                elif self.close_enough(self.target):
                    self.target = None
                    self.end_activity(StageActivities.Moving)
                elif (hw_status.MvCmdSts & MvcmdStatus.MVCMD_ERROR) != 0:
                    self.end_activity(StageActivities.Moving)
                    logger.error(
                        f"{op}: move command 0x{hw_status.MvCmdSts & MvcmdStatus.MVCMD_NAME_BITS:08X} "
                        + "ended with MVCMD_ERROR"
                    )
                    with self.stage_lock:
                        for i in range(3):
                            logger.error(f"attempt #{i} (of 3): attempting to clear MVCMD_ERROR by calling command_stop()")
                            ximclib.command_stop(self.device)
                            time.sleep(0.2)
                            result = ximclib.get_status(self.device, byref(hw_status))
                            if result != Result.Ok:
                                logger.error(
                                    f"{op}: attempt #{i} (of 3): could not get_status(), {result=} ({RESULT_MAP[result]})"
                                )
                                break
                            logger.error(
                                f"{op}: attempt #{i} (of 3): status after command_stop(): {hw_status.MvCmdSts=:08X}"
                            )
                            if (hw_status.MvCmdSts & MvcmdStatus.MVCMD_ERROR) != 0:
                                logger.error(f"{op}: attempt #{i} (of 3): successfully cleared MVCMD_ERROR")
                                break

            if self.is_active(StageActivities.StartingUp) and self.close_enough(self.presets[STARTUP_PRESET]):
                self.end_activity(StageActivities.StartingUp)

            if self.is_active(StageActivities.Homing):
                self.end_activity(StageActivities.Homing)

    def move_to_preset(self, preset: StagePresetPosition) -> CanonicalResponse:
        """
        Starts moving the stage to one of the preset positions.

        Parameters
        ----------
        preset
            One of `sky`, `spec`, `min`, `mid`, `max`.

        Notes
        -----
        Annotating this with the enum itself is what fixes #85: pydantic validates a query
        string against an Enum **by value**, so `?preset=spec` arrives already resolved and
        an unknown name is a 422 from FastAPI listing the valid ones. The previous signature
        spliced together `Const.SolvingPhase` and a capitalised `Literal`, neither of which
        matched the by-name lookup the body then performed.
        """
        op = function_name()

        # Direct Python callers may still pass a string; the API path never reaches this,
        # having been resolved by pydantic already.
        if isinstance(preset, str):
            try:
                preset = StagePresetPosition(preset.lower())
            except ValueError:
                return CanonicalResponse(errors=[f"{op}: no such preset '{preset}', expected one of {stage_position_names}"])

        # Each of these used to be a bare `return`, i.e. HTTP 200 with a null body and the
        # stage motionless -- a failure the caller could not tell from success (#85, #47).
        if not self.detected:
            return CanonicalResponse(errors=[f"{op}: stage not detected"])
        if not self.connected:
            return CanonicalResponse(errors=[f"{op}: stage not connected"])

        # min/mid/max are only populated once travel limits are known (see startup), so a
        # preset can be valid yet have no position yet.
        if preset not in self.presets:
            return CanonicalResponse(errors=[f"{op}: preset '{preset.value}' has no position yet; is the stage up?"])

        preset_position = self.presets[preset]
        if self.close_enough(preset_position):
            logger.info(f"{op}: not moving, {self.position=} is close enough to {preset_position=}")
            return CanonicalResponse_Ok

        return self.move_absolute(preset_position)

    def move_absolute(self, position: int | str):
        op = function_name()

        if not self.detected:
            return CanonicalResponse(errors=[f"{op}: not detected"])
        if not self.connected:
            return CanonicalResponse(errors=[f"{op}: not connected"])

        if isinstance(position, str):
            try:
                position = int(position)
            except ValueError:
                return CanonicalResponse(errors=[f"{op}: '{position}' is not a position"])

        if self.close_enough(position):
            # Ok, not None: this is the "already there" success, and returning None from a
            # route makes it HTTP 200 with a null body -- indistinguishable from a refusal
            # (#85, #47). It matters more now that every absolute move comes through here.
            logger.info(f"{op}: not moving, {self.position=} is close enough to {position=}")
            return CanonicalResponse_Ok

        if self.max_travel is None or self.min_travel is None:
            return CanonicalResponse(errors=["cannot move - min_travel or max_travel is None"])

        if not (self.min_travel <= position < self.max_travel):
            return CanonicalResponse(errors=[f"out of range: {self.min_travel} <= position < {self.max_travel}"])
        try:
            with self.stage_lock:
                assert ximclib
                response = ximclib.command_move(self.device, position, 0)
                if response != Result.Ok:
                    msg = f"{op}: Failed to start stage move absolute (command_move({self.device}, {position}), {response=}"
                    logger.error(msg)
                    return CanonicalResponse(errors=[msg])
        except Exception as ex:
            msg = f"{op}: Failed to start stage move absolute (command_move({self.device}, {position}), {ex=}"
            logger.exception(msg)
            return CanonicalResponse(errors=[msg])

        self.ticks_at_start = self.position
        self.target = position
        self.motion_start_time = datetime.datetime.now(datetime.UTC)
        self.start_activity(StageActivities.Moving, details=[f"from {self.position} to {self.target}"])

        return CanonicalResponse_Ok

    def move_relative(self, direction: StageDirection | str, amount: int | str):
        """
        Starts moving the stage in the specified direction by the specified number of native units

        Parameters
        ----------
        direction
            The direction to move (**Up**: away from the motor, **Down**: towards the motor)
        amount
            How many units to move
        """
        op = function_name()

        current_position = self.position
        if current_position is None:
            return CanonicalResponse(errors=["cannot get current position"])

        if isinstance(direction, str):
            direction = StageDirection(stage_direction_str2int_dict[direction])
        if isinstance(amount, str):
            amount = abs(int(amount))

        amount *= 1 if direction == StageDirection.Up else -1
        try:
            self.target = current_position + amount
            self.start_activity(StageActivities.Moving, details=[f"from {self.position} to {self.target}"])
            with self.stage_lock:
                assert ximclib
                response = ximclib.command_movr(self.device, amount, 0)
            if response != Result.Ok:
                msg = f"Failed to start stage move (command_movr({self.device}, {amount})"
                logger.error(f"{op}: " + msg)
                return CanonicalResponse(errors=[msg])
        except Exception as ex:
            msg = f"{op}: Failed to start stage move relative (command_movr({self.device}, {amount}), {ex=}"
            logger.exception(msg)
            return CanonicalResponse(errors=[msg])
        return CanonicalResponse_Ok

    def endpoint_abort(self):
        return self.abort()

    def abort(self):
        """
        Aborts any in-progress stage activities
        """
        for activity in (
            StageActivities.StartingUp,
            StageActivities.Moving,
            StageActivities.ShuttingDown,
        ):
            if self.is_active(activity):
                self.end_activity(activity)

        assert ximclib
        ximclib.command_stop(self.device)
        return CanonicalResponse_Ok

    @property
    def name(self) -> str:
        return "stage"

    @property
    def reachable(self) -> bool | None:
        # `_currently_operational` is a driver-failure latch, so it belongs here: a stage whose
        # controller has reported a failure cannot be commanded, whatever its position.
        return self.is_on() and self.detected and self.connected and self._currently_operational

    @property
    def deployed(self) -> bool | None:
        return self.at_preset(StagePresetPosition.Spec) or self.at_preset(StagePresetPosition.Sky)

    @property
    def why_not_reachable(self) -> list[str] | None:
        label = f"{self.name}"
        if not self.is_on():
            return [f"{label}: not powered"]
        ret = []
        if not self.detected:
            ret.append(f"{label}: not detected")
        if not self.connected:
            ret.append(f"{label}: not connected")
        if not self._currently_operational:
            ret.extend(self._why_not_currently_operational)
        return ret

    @property
    def why_not_deployed(self) -> list[str] | None:
        label = f"{self.name}"
        if self.at_preset(StagePresetPosition.Spec) or self.at_preset(StagePresetPosition.Sky):
            return []
        return [
            f"{label}: at {self.position}, not at '{StagePresetPosition.Spec.value}' "
            + f"({self.presets[StagePresetPosition.Spec]}) or '{StagePresetPosition.Sky.value}' "
            + f"({self.presets[StagePresetPosition.Sky]}) preset positions"
        ]

    @property
    def operational(self) -> bool:
        return bool(self.reachable) and bool(self.deployed)

    # `operational` prefers the reachability reasons when there are any, rather than
    # concatenating both halves: a component that cannot be reached has nothing useful to say
    # about a motion it was never able to start, and that is also what this list said before the
    # split -- so no consumer of `why_not_operational` sees a change. The two halves are reported
    # separately in their own fields (MAST_unit#144).
    @property
    def why_not_operational(self) -> list[str]:
        return list(self.why_not_reachable or []) or list(self.why_not_deployed or [])

    @property
    def detected(self) -> bool:
        return self.device != -1

    @property
    def was_shut_down(self) -> bool:
        return self._was_shut_down

    def endpoint_get_position(self) -> CanonicalResponse:
        return self.get_position()

    def get_position(self) -> CanonicalResponse:
        return CanonicalResponse(value=self.position)

    def endpoint_set_position(self, pos: int):
        return self.set_position(pos)

    def set_position(self, pos: int) -> CanonicalResponse:
        """`PUT /stage/position`: the only way to command an absolute position over the API.

        It used to assign the `position` property, which is a second implementation of the
        same move that never acquired `move_absolute`'s guards -- so the one route an
        operator has for an absolute move was the one that skipped the travel-limit check
        and could drive the stage past `max_travel`. It also raised rather than returning,
        so a refusal surfaced as a 500 with a traceback, while this returned `Ok`
        unconditionally regardless of what happened.
        """
        return self.move_absolute(pos)

    @property
    def api_router(self) -> APIRouter:
        base_stage_path = Const.BASE_UNIT_PATH + "/stage"
        tag = "Stage"

        router = APIRouter()
        router.add_api_route(base_stage_path + "/startup", tags=[tag], endpoint=self.endpoint_startup)
        router.add_api_route(base_stage_path + "/shutdown", tags=[tag], endpoint=self.endpoint_shutdown)
        router.add_api_route(base_stage_path + "/abort", tags=[tag], endpoint=self.endpoint_abort)
        router.add_api_route(base_stage_path + "/status", tags=[tag], endpoint=self.endpoint_status)
        router.add_api_route(
            base_stage_path + "/position",
            tags=[tag],
            endpoint=self.endpoint_get_position,
        )
        router.add_api_route(
            base_stage_path + "/position",
            methods=["PUT"],
            tags=[tag],
            endpoint=self.endpoint_set_position,
        )
        router.add_api_route(base_stage_path + "/connect", tags=[tag], endpoint=self.connect)
        router.add_api_route(base_stage_path + "/disconnect", tags=[tag], endpoint=self.disconnect)
        router.add_api_route(base_stage_path + "/move", tags=[tag], endpoint=self.move_relative)
        router.add_api_route(
            base_stage_path + "/move_to_preset",
            tags=[tag],
            endpoint=self.move_to_preset,
        )

        return router


if __name__ == "__main__":
    stage = Stage(unit=None)  # type: ignore

    def move_between_presets():

        def move_and_wait(preset: StagePresetPosition):
            stage.move_to_preset(preset)
            time.sleep(0.5)
            while stage.is_active(StageActivities.Moving):
                time.sleep(1)

        for _ in range(3):
            move_and_wait(StagePresetPosition.Sky)
            time.sleep(5)
            move_and_wait(StagePresetPosition.Spec)

    def get_position():
        logger.info(f"Stage position: {stage.position}")

    def test_set_profile():
        stage.set_profile()

    def test_move_between_presets():
        move_between_presets()
        if stage.is_moving:
            logger.info("Stage is moving, waiting to get position until it stops...")
        while stage.is_moving:
            time.sleep(1)
        logger.info("Stage stopped, getting position...")
        get_position()

    # test_set_profile()
    get_position()
    sys.exit(0)
