import datetime
import logging
import os
import platform
import sys
import threading
import time
from enum import Enum, IntEnum, auto
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from fastapi.routing import APIRouter

from common.activities import StageActivities
from common.canonical import CanonicalResponse, CanonicalResponse_Ok
from common.config import Config
from common.const import Const
from common.dlipowerswitch import OutletDomain, PowerStatus, SwitchedOutlet
from common.interfaces.components import Component, ComponentStatus
from common.mast_logging import init_log
from common.utils import RepeatTimer, Timeout, function_name, time_stamp

if TYPE_CHECKING:
    from unit import Unit

logger = logging.getLogger("mast.unit." + __name__)
init_log(logger)

os.environ["XILOG"] = "C:/temp/ximc.log"  # Enables logging for ximc library.

# cur_dir = os.path.abspath(os.path.dirname(__file__))  # Specifies the current directory.
cur_dir = Path().cwd()
ximc_dir = cur_dir / "Standa" / "ximc-2.13.6" / "ximc"  # dependencies for examples.
sys.path.append(
    str(ximc_dir / "crossplatform" / "wrappers" / "python")
)  # add pyximc.py wrapper to python path

if platform.system() == "Windows":
    # Determining the directory with dependencies for windows depending on the bit depth.
    arch_dir = "win64" if "64" in platform.architecture()[0] else "win32"  #
    lib_dir = ximc_dir / arch_dir  # lib directory for ximc library
    if not lib_dir.exists():
        raise FileNotFoundError(f"Directory with ximc library not found: {lib_dir=}. ")
    # logger.info(f"calling os.add_dll_directory({lib_dir=}) ...")
    os.add_dll_directory(str(lib_dir))  # add dll path into an environment variable

    from pyximc import EnumerateFlags  # type: ignore[name]
    from pyximc import Result  # type: ignore[name]
    from pyximc import (
        POINTER,
        MvcmdStatus,
        StateFlags,
        byref,
        c_int,
        cast,
        device_information_t,
        edges_settings_t,
    )
    from pyximc import lib as ximclib  # type: ignore[name]
    from pyximc import status_t, string_at

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


class StageStatus(PowerStatus, ComponentStatus):
    info: dict | None = None
    presets: dict | None = None
    position: int | None = None
    at_preset: str | None = None
    target: int | None = None
    target_verbal: str | None = None
    date: str | None = None


class Stage(Component, SwitchedOutlet):
    _instance = None
    _initialized = False

    CLOSE_ENOUGH = 2

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    _positioning_precision: int = 100

    def __init__(self, unit: "Unit"):  # type: ignore
        if self._initialized:
            return

        op = "Stage.__init__"
        self.unit = unit
        self.conf = Config().get_unit().stage

        SwitchedOutlet.__init__(self, OutletDomain.Unit, outlet_name="Stage")
        Component.__init__(self)

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
        enum_hints = b"hint=only_usb"
        assert ximclib
        self.device = -1
        try:
            with Timeout(2) as timeout:
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

        assert ximclib
        self.device_uri = ximclib.get_device_name(dev_enum, 0)
        ximclib.free_enumerate_devices(dev_enum)
        self.device = ximclib.open_device(self.device_uri)

        if not self.detected:
            logger.error(f"{op}: no device detected ({self.device=}")
            return

        x_device_information = device_information_t()
        assert ximclib
        result = ximclib.get_device_information(
            self.device, byref(x_device_information)
        )
        x_edges_settings = edges_settings_t()
        assert ximclib
        result1 = ximclib.get_edges_settings(self.device, byref(x_edges_settings))
        if result == Result.Ok and result1 == Result.Ok:
            comport = str(self.device_uri)
            comport = comport[comport.find("COM") :].removesuffix("'")
            self.min_travel = x_edges_settings.LeftBorder
            self.max_travel = x_edges_settings.RightBorder

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
            self.info["travel"] = {
                "min": self.min_travel,
                "max": self.max_travel,
            }

            self.device_info = (
                f"port='{comport}', manufacturer='{self.info['controller']}', product='{self.info['product']}' "
                + f"version='{self.info['version']}', range={self.min_travel}..{self.max_travel}, close_enough={self.CLOSE_ENOUGH}"
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
            assert ximclib
            result = ximclib.get_status(self.device, byref(hw_status))
        if result == Result.Ok:
            self._position = hw_status.CurPosition
            self.is_moving = hw_status.MvCmdSts & MvcmdStatus.MVCMD_RUNNING

        self.timer = RepeatTimer(2, function=self.ontimer)
        self.timer.name = "stage-timer-thread"
        self.timer.start()

        logger.info(f"detected: {self.device_info}")
        with self.stage_lock:
            assert ximclib
            result = ximclib.command_homezero(self.device)
            if result == Result.Ok:
                self.start_activity(StageActivities.Homing)

        self._initialized = True

    def __del__(self):
        logger.info(f"Closing {self.device=}")
        assert ximclib
        assert self.device
        ximclib.close_device(byref(cast(self.device, POINTER(c_int))))

    def __repr__(self):
        return f"<Stage device={self.device}>"

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

    def shutdown(self):
        """
        Shutdown routine for the **MAST** stage.  Makes it ``idle``

        :mastapi:
        """
        self.disconnect()
        self.power_off()
        self._was_shut_down = True
        return CanonicalResponse_Ok

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
            self.start_activity(StageActivities.Moving)
        else:
            raise Exception(f"Could not start move to {value} ({result=})")

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
        return abs(self._position - target) <= self.CLOSE_ENOUGH

    def ontimer(self):  # noqa: C901
        if not self.detected or not self.stage_lock:
            return

        hw_status = status_t()
        with self.stage_lock:
            assert ximclib
            result = ximclib.get_status(self.device, byref(hw_status))
        if result != Result.Ok:
            # result_name = Result(result).name
            logger.error(f"could not get_status(), {result=} ({RESULT_MAP[result]})")
            return

        self._position = hw_status.CurPosition

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

        self.is_moving = hw_status.MvCmdSts & MvcmdStatus.MVCMD_RUNNING

        if not self.is_moving:
            if self.is_active(StageActivities.Moving):
                if self.close_enough(self.target):
                    self.target = None
                    self.end_activity(StageActivities.Moving)
                elif hw_status.MvCmdSts & MvcmdStatus.MVCMD_ERROR:
                    self.end_activity(StageActivities.Moving)
                    logger.error(
                        f"move command 0x{hw_status.MvCmdSts & MvcmdStatus.MVCMD_NAME_BITS:08X} "
                        + "ended with MVCMD_ERROR"
                    )

            if self.is_active(StageActivities.StartingUp) and self.close_enough(
                self.presets[StagePresetPosition.StartUp]
            ):
                self.end_activity(StageActivities.StartingUp)

            if self.is_active(StageActivities.Homing):
                self.end_activity(StageActivities.Homing)

    def move_to_preset(
        self,
        preset: Literal["Sky", "Spec", "Min", "Mid", "Max"] | StagePresetPosition,
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
        logger.info(f"{op}: move: from {self.position=} to {self.target=}")
        self.start_activity(StageActivities.Moving)

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
            self.start_activity(StageActivities.Moving)
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
        return CanonicalResponse(value=self.position)

    def endpoint_set_position(self, pos: int):
        self.position = pos
        return CanonicalResponse_Ok

    @property
    def api_router(self) -> APIRouter:
        base_stage_path = Const.BASE_UNIT_PATH + "/stage"
        tag = "Stage"

        router = APIRouter()
        router.add_api_route(
            base_stage_path + "/startup", tags=[tag], endpoint=self.startup
        )
        router.add_api_route(
            base_stage_path + "/shutdown", tags=[tag], endpoint=self.shutdown
        )
        router.add_api_route(
            base_stage_path + "/abort", tags=[tag], endpoint=self.abort
        )
        router.add_api_route(
            base_stage_path + "/status", tags=[tag], endpoint=self.status
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
