import datetime
import logging
import os
import platform
import sys
import threading
import time
from collections import deque
from enum import Enum, IntEnum, auto
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from fastapi.routing import APIRouter

from common.activities import StageActivities
from common.canonical import CanonicalResponse, CanonicalResponse_Ok
from common.config import Config
from common.config.rois import FcuVersion
from common.const import Const
from common.dlipowerswitch import OutletDomain, SwitchedOutlet
from common.interfaces.components import Component
from common.mast_logging import init_log
from common.models.statuses import StageStatus
from common.utils import RepeatTimer, Timeout, boxed_log, function_name, time_stamp

if TYPE_CHECKING:
    from unit import Unit

logger = logging.getLogger("mast.unit." + __name__)
init_log(logger)

# os.environ["XILOG"] = "C:/temp/ximc.log"  # Enables logging for ximc library.

XIMC_VERSION = '2.13.6'
ximc_top_dir = Path().cwd() / "Standa" / f"ximc-{XIMC_VERSION}" / "ximc"

for path in [
        ximc_top_dir / "crossplatform" / "wrappers" / "python", # examples
        ximc_top_dir / "python-profiles" / "STANDA"             # profiles
        ]:
    sys.path.append(str(path))

if platform.system() == "Windows":
    # Determining the directory with dependencies for windows depending on the bit depth.
    arch_dir = "win64" if "64" in platform.architecture()[0] else "win32"  #
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
        controller_name_t,
        device_information_t,
        edges_settings_t,
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


class StagePresetPosition(Enum):
    Sky = ("sky",)
    Spec = ("spec",)
    Min = ("min",)
    Middle = ("mid",)
    Max = ("max",)
    StartUp = Sky


stage_position_names: list[str] = [k for k in StagePresetPosition.__dict__]

stage_direction_str2int_dict: dict = {
    "Up": StageDirection.Up,
    "Down": StageDirection.Down,
}

class Stage(Component, SwitchedOutlet):
    _instance = None
    _initialized = False

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    _positioning_precision: int = 100

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
                dev_enum = timeout.run(
                    ximclib.enumerate_devices, probe_flags, enum_hints
                )
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
            logger.error(f"{op}: no device detected ({self.device=}")
            return

        self.stage_model = None
        x_controller_name = controller_name_t()
        result = ximclib.get_controller_name(self.device, byref(x_controller_name))
        if result == Result.Ok:
            self.stage_model = repr(
                string_at(x_controller_name.ControllerName).decode()
            ).replace("'", "")

            match self.stage_model:
                case "8MT167-25LS-MEn1":
                    self.fcu_version = FcuVersion.v1
                case "8MT173-20DCE2":
                    self.fcu_version = FcuVersion.v2
                case _:
                    raise Exception(f"{op}: unsupported stage model '{self.stage_model}'")
        else:
            raise Exception(f"{op}: cannot get controller name ({result=})")

        # self.set_profile()  # FUTURE: set motion profile parameters for known stage models

        x_device_information = device_information_t()
        result = ximclib.get_device_information(
            self.device, byref(x_device_information)
        )

        if result == Result.Ok:
            comport = str(self.device_uri)
            comport = comport[comport.find("COM") :].removesuffix("'")

            self.info["port"] = comport
            self.info["controller"] = repr(
                string_at(x_device_information.Manufacturer).decode()
            ).replace("'", "")
            self.info["product"] = repr(
                string_at(x_device_information.ProductDescription).decode()
            ).replace("'", "")
            self.info["version"] = (
                f"{repr(x_device_information.Major)}.{repr(x_device_information.Minor)}"
                + f".{repr(x_device_information.Release)}"
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

        self.device_info = (
            f"port='{comport}', manufacturer='{self.info['controller']}', product='{self.info['product']}', "
            + f"version='{self.info['version']}', model='{self.stage_model}', "
            + f"fcu_version='{self.fcu_version.value}', "
            + f"range={self.min_travel}..{self.max_travel} (borders by: {self.border_by}), "
            + f"close_enough={self.conf.close_enough}"
        )
        self.stage_lock = threading.Lock()

        if self.min_travel is not None and self.max_travel is not None:
            self.presets[StagePresetPosition.Min] = self.min_travel
            self.presets[StagePresetPosition.Max] = self.max_travel
            self.presets[StagePresetPosition.Middle] = int(
                (self.max_travel - self.min_travel) / 2
            )

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
        except Exception as e:
            logger.warning(f"Error executing profile file '{file_path.as_posix()}': {e}")
            return

        profile_setter = namespace.get(profile_setter_function)
        if profile_setter is None:
            logger.warning(f"Profile setter function '{profile_setter_function}' not found in profile file '{file_path.as_posix()}'")
            return

        try:
            result = profile_setter(ximclib, self.device)
            logger.info(f"set profile for stage model '{self.stage_model}' from file '{file_path.as_posix()}', "
                        + f"result: {RESULT_MAP.get(result, result)}")
        except Exception as e:
            logger.error(f"error setting profile for stage model '{self.stage_model}' "
                         + f"from file '{file_path.as_posix()}': {e}")

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

        :mastapi:
        """

        if not self.is_on():
            self.power_on()
        self.connected = True
        return CanonicalResponse_Ok

    def disconnect(self):
        """
        Disconnects from the **MAST** stage controller

        :mastapi:
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

        :mastapi:
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
        if not self.connected:
            raise Exception("Not connected")

        if self.close_enough(value):
            logger.info(
                f"Not changing position ({self.position} is close enough to {value}"
            )
            return

        self.target = value
        with self.stage_lock:
            assert ximclib, "No ximclib"
            result = ximclib.command_move(self.device, value)
        if result == Result.Ok:
            self.start_activity(StageActivities.Moving, details=[f"to {self.target}"])
        else:
            raise Exception(f"Could not start move to {value} ({result=})")

    def endpoint_status(self) -> StageStatus:
        return self.status()

    def status(self) -> StageStatus:
        at_preset = None
        if self.detected:
            for k in self.presets:
                if self.close_enough(self.presets[k]):
                    at_preset = k.name.lower()
                    break

        target_verbal = f"{self.target}"
        if self.target is not None:
            for preset in self.presets:
                if self.target == preset.value:
                    target_verbal = preset.name
                    break

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

    def ontimer(self):  # noqa: C901
        if self.unit and self.unit.unit_shutdown_event.is_set():
            if self.timer:
                self.timer.cancel()
            return

        if not self.connected:
            return

        if not self.detected or not self.stage_lock:
            return

        hw_status = status_t()
        with self.stage_lock:
            assert ximclib
            result = ximclib.get_status(self.device, byref(hw_status))
        if result != Result.Ok:
            logger.error(f"could not get_status(), {result=} ({RESULT_MAP[result]})")
            return

        self._position = hw_status.CurPosition
        self.latest_positions.append(self._position)

        error_bits = (
            StateFlags.STATE_ERRC | StateFlags.STATE_ERRV | StateFlags.STATE_ERRD
        )
        controller_error = hw_status.Flags & error_bits
        if controller_error:
            logger.error(f"CONTR ERROR 0x{controller_error:08X}")

        security_error = hw_status.Flags & StateFlags.STATE_SECUR
        if security_error:
            logger.error(f"SECUR ERROR 0x{security_error:08X}")

        if hw_status.Flags & StateFlags.STATE_ALARM:
            # should wait for the ALARM cause to go away and then issue a command_stop()
            # for now, just log
            logger.info("Detected StateFlags.STATE_ALARM, issuing a STOP command")
            with self.stage_lock:
                result = ximclib.command_stop(self.device)
            if result != Result.Ok:
                logger.error(f"could not command_stop({self.device}), {result=}")
            # TBD:  What else needs to be done?

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
                        f"move command 0x{hw_status.MvCmdSts & MvcmdStatus.MVCMD_NAME_BITS:08X} "
                        + "ended with MVCMD_ERROR"
                    )
                    with self.stage_lock:
                        for i in range(3):
                            logger.error(
                                f"attempt #{i} (of 3): attempting to clear MVCMD_ERROR by calling command_stop()"
                            )
                            ximclib.command_stop(self.device)
                            time.sleep(0.2)
                            result = ximclib.get_status(self.device, byref(hw_status))
                            if result != Result.Ok:
                                logger.error(
                                    f"attempt #{i} (of 3): could not get_status(), {result=} ({RESULT_MAP[result]})"
                                )
                                break
                            logger.error(
                                f"attempt #{i} (of 3): status after command_stop(): MvCmdSts=0x{hw_status.MvCmdSts:08X}"
                            )
                            if (hw_status.MvCmdSts & MvcmdStatus.MVCMD_ERROR) != 0:
                                logger.error(
                                    f"attempt #{i} (of 3): successfully cleared MVCMD_ERROR"
                                )
                                break

            if self.is_active(StageActivities.StartingUp) and self.close_enough(
                self.presets[StagePresetPosition.StartUp]
            ):
                self.end_activity(StageActivities.StartingUp)

            if self.is_active(StageActivities.Homing):
                self.end_activity(StageActivities.Homing)

    def move_to_preset(
        self,
        preset: Const.SolvingPhase | Literal["Min", "Mid", "Max"] | StagePresetPosition,
    ):
        """
        Starts moving the stage to one of the preset positions

        Parameters
        ----------
        preset
            Name of a preset position
        """
        if not self.detected or not self.connected:
            return

        if isinstance(preset, str):
            try:
                preset = StagePresetPosition.__getitem__(preset)
            except KeyError:
                logger.warning(f"No such preset position '{preset}'")
                return

        preset_position = self.presets[preset]
        if self.close_enough(preset_position):
            logger.info(
                f"Not moving {self.position=} is close enough to {preset_position=}"
            )
            return

        return self.move_absolute(preset_position)

    def move_absolute(self, position: int | str):
        op = function_name()

        if not self.detected:
            return CanonicalResponse(errors=["not detected"])
        if not self.connected:
            return CanonicalResponse(errors=["not connected"])

        if isinstance(position, str):
            position = int(position)

        if self.close_enough(position):
            logger.info(
                f"{op}: Not moving {self.position=} is close enough to {position=}"
            )
            return

        if self.max_travel is None or self.min_travel is None:
            return CanonicalResponse(
                errors=["cannot move - min_travel or max_travel is None"]
            )

        if not (self.min_travel <= position < self.max_travel):
            return CanonicalResponse(
                errors=[
                    f"out of range: {self.min_travel} <= position < {self.max_travel}"
                ]
            )
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
            logger.error(msg)
            return CanonicalResponse(errors=[msg])

        self.ticks_at_start = self.position
        self.target = position
        self.motion_start_time = datetime.datetime.now()
        self.start_activity(
            StageActivities.Moving, details=[f"from {self.position} to {self.target}"]
        )

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
            self.start_activity(
                StageActivities.Moving, details=[f"from {self.position} to {self.target}"]
            )
            with self.stage_lock:
                assert ximclib
                response = ximclib.command_movr(self.device, amount, 0)
            if response != Result.Ok:
                msg = (
                    f"Failed to start stage move (command_movr({self.device}, {amount})"
                )
                logger.error(f"{op}: " + msg)
                return CanonicalResponse(errors=[msg])
        except Exception as ex:
            msg = f"{op}: Failed to start stage move relative (command_movr({self.device}, {amount}), {ex=}"
            logger.error(msg)
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
    def operational(self) -> bool:
        return all(
            [
                self.is_on(),
                self.detected,
                self.connected,
                not self.was_shut_down,
                (
                    self.at_preset(StagePresetPosition.Spec)
                    or self.at_preset(StagePresetPosition.Sky)
                ),
            ]
        )

    @property
    def why_not_operational(self) -> list[str]:
        label = f"{self.name}"
        ret = []
        if not self.is_on():
            ret.append(f"{label}: not powered")
        else:
            if not self.detected:
                ret.append(f"{label}: not detected")
            if self.was_shut_down:
                ret.append(f"{label}: shut down")
            if not self.connected:
                ret.append(f"{label}: not connected")
            elif not (
                self.at_preset(StagePresetPosition.Spec)
                or self.at_preset(StagePresetPosition.Sky)
            ):
                ret.append(
                    f"{label}: at {self.position}, not at 'Spec' "
                    + f"({self.presets[StagePresetPosition.Spec]}) or 'Sky' "
                    + f"({self.presets[StagePresetPosition.Sky]}) preset positions"
                )
        return ret

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

    def set_position(self, pos: int):
        self.position = pos
        return CanonicalResponse_Ok

    @property
    def api_router(self) -> APIRouter:
        base_stage_path = Const.BASE_UNIT_PATH + "/stage"
        tag = "Stage"

        router = APIRouter()
        router.add_api_route(
            base_stage_path + "/startup", tags=[tag], endpoint=self.endpoint_startup
        )
        router.add_api_route(
            base_stage_path + "/shutdown", tags=[tag], endpoint=self.endpoint_shutdown
        )
        router.add_api_route(
            base_stage_path + "/abort", tags=[tag], endpoint=self.endpoint_abort
        )
        router.add_api_route(
            base_stage_path + "/status", tags=[tag], endpoint=self.endpoint_status
        )
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
        router.add_api_route(
            base_stage_path + "/connect", tags=[tag], endpoint=self.connect
        )
        router.add_api_route(
            base_stage_path + "/disconnect", tags=[tag], endpoint=self.disconnect
        )
        router.add_api_route(
            base_stage_path + "/move", tags=[tag], endpoint=self.move_relative
        )
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

    test_set_profile()
    sys.exit(0)
