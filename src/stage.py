import logging
import threading
import time
from enum import IntEnum, auto, Enum
import datetime
from typing import List, Union, Literal

from common.utils import RepeatTimer, Component, time_stamp, CanonicalResponse, CanonicalResponse_Ok
from common.utils import BASE_UNIT_PATH, function_name
from common.config import Config
from common.mast_logging import init_log
from common.dlipowerswitch import SwitchedOutlet, OutletDomain
import os
import sys
import platform
from fastapi.routing import APIRouter
from common.activities import StageActivities
from common.stopping import StoppingMonitor

cur_dir = os.path.abspath(os.path.dirname(__file__))                            # Specifies the current directory.
ximc_dir = os.path.join(cur_dir, "Standa", "ximc-2.13.6", "ximc")               # dependencies for examples.
sys.path.append(os.path.join(ximc_dir, "crossplatform", "wrappers", "python"))  # add pyximc.py wrapper to python path

if platform.system() == "Windows":
    # Determining the directory with dependencies for windows depending on the bit depth.
    arch_dir = "win64" if "64" in platform.architecture()[0] else "win32"  #
    lib_dir = os.path.join(ximc_dir, arch_dir)
    if sys.version_info >= (3, 8):
        os.add_dll_directory(lib_dir)
    else:
        os.environ["Path"] = lib_dir + ";" + os.environ["Path"]  # add dll path into an environment variable

    from pyximc import (Result,  EnumerateFlags, device_information_t, string_at, byref, MvcmdStatus, StateFlags, cast, POINTER,
                        c_int, status_t, edges_settings_t)
    from pyximc import lib as ximclib

logger = logging.getLogger('mast.unit.' + __name__)
init_log(logger)


class StageDirection(IntEnum):
    Up = auto()   
    Down = auto()


class StagePresetPosition(Enum):
    Sky = 'sky',
    Spec = 'spec',
    Min = 'min',
    Middle = 'mid',
    Max = 'max',
    StartUp = Sky


stagePositionNames: List[str] = [k for k in StagePresetPosition.__dict__.keys()]

stage_direction_str2int_dict: dict = {
    'Up': StageDirection.Up,
    'Down': StageDirection.Down,
}


class Stage(Component, SwitchedOutlet, StoppingMonitor):
    # class Stage(Component, SwitchedOutlet, StoppingMonitor):
    _instance = None
    _initialized = False

    CLOSE_ENOUGH = 2

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super(Stage, cls).__new__(cls)
        return cls._instance

    _positioning_precision: int = 100

    def __init__(self, unit: 'Unit'):
        if self._initialized:
            return

        self.unit = unit
        self.unit_conf: dict = Config().get_unit()
        self.conf = self.unit_conf['stage']

        SwitchedOutlet.__init__(self, OutletDomain.Unit, outlet_name='Stage')
        # SwitchedOutlet.__init__(self, identifier='Stage')
        Component.__init__(self)
        # StoppingMonitor.__init__(self, 'stage', max_len=5, sampler=self.position_sampler, interval=.5, epsilon=0)

        self.errors: List[str] = []
        self.device = None
        self.ticks_at_start: int | None = None
        self.ticks_at_target: int | None = None
        self.motion_start_time: datetime.datetime | None = None
        self.timer: RepeatTimer | None = None
        self.device_uri: str | None = None
        self._position: int | None = None
        self.is_moving: bool = False
        self.target: int | None = None
        self.stage_lock: threading.Lock | None = None
        self.min_travel: int | None = None
        self.max_travel: int | None = None

        self.info = {}
        self._was_shut_down = False

        if not self.is_on():
            self.power_on()
            time.sleep(3)

        self.presets = {
            StagePresetPosition.Sky: self.conf['presets']['sky'],
            StagePresetPosition.Spec: self.conf['presets']['spec']
            }

        # This is device search and enumeration with probing. It gives more information about devices.
        probe_flags = EnumerateFlags.ENUMERATE_PROBE | EnumerateFlags.ENUMERATE_ALL_COM
        enum_hints = b"addr="
        dev_enum = ximclib.enumerate_devices(probe_flags, enum_hints)

        self.device = -1
        dev_count = ximclib.get_device_count(dev_enum)
        if dev_count == 0:
            logger.error(f"stage.__init__: no device detected ({dev_count=})")
            return

        self.device_uri = ximclib.get_device_name(dev_enum, 0)
        ximclib.free_enumerate_devices(dev_enum)
        self.device = ximclib.open_device(self.device_uri)

        if not self.detected:
            logger.error(f"no device detected ({self.device=}")
            return

        x_device_information = device_information_t()
        result = ximclib.get_device_information(self.device, byref(x_device_information))
        x_edges_settings = edges_settings_t()
        result1 = ximclib.get_edges_settings(self.device, byref(x_edges_settings))
        if result == Result.Ok and result1 == Result.Ok:
            comport = str(self.device_uri)
            comport = comport[comport.find('COM'):]
            self.min_travel = x_edges_settings.LeftBorder
            self.max_travel = x_edges_settings.RightBorder

            self.info['port'] = comport
            self.info['controller'] = repr(string_at(x_device_information.Manufacturer).decode()).replace("'", '')
            self.info['product'] = repr(string_at(x_device_information.ProductDescription).decode()).replace("'", '')
            self.info['version'] = (f"{repr(x_device_information.Major)}.{repr(x_device_information.Minor)}" +
                                    f".{repr(x_device_information.Release)}")
            self.info['travel'] = {
                'min': self.min_travel,
                'max': self.max_travel,
            }

            self.device_info = (
                "Port: {}, Manufacturer={}, Product={}, Version={}, Range={}..{}, CLOSE_ENOUGH={}".format(
                    comport,
                    self.info['controller'],
                    self.info['product'],
                    self.info['version'],
                    self.min_travel,
                    self.max_travel,
                    self.CLOSE_ENOUGH,
                ))
        self.stage_lock = threading.Lock()

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

        self.timer = RepeatTimer(2, function=self.ontimer)
        self.timer.name = 'stage-timer-thread'
        self.timer.start()

        with self.stage_lock:
            result = ximclib.command_homezero(self.device)
            if result == Result.Ok:
                self.start_activity(StageActivities.Homing)

        self._initialized = True
        logger.info(f'initialized ({self.device_info})')

    def __del__(self):
        logger.info(f"Closing {self.device=}")
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

        if value:
            self.device = ximclib.open_device(self.device_uri)
        else:
            ximclib.close_device(byref(cast(self.device, POINTER(c_int))))
            self.device = -1

        logger.info(f'connected = {value} => {self.connected}')

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
            raise Exception('Not connected')

        if self.close_enough(value):
            logger.info(f'Not changing position ({self.position} is close enough to {value}')
            return

        self.target = value
        with self.stage_lock:
            result = ximclib.command_move(self.device, value)
        if result == Result.Ok:
            self.start_activity(StageActivities.Moving)
        else:
            raise Exception(f'Could not start move to {value} ({result=})')

    def status(self) -> dict:
        """
        Returns the status of the MAST stage
        :mastapi:
        """
        ret = self.power_status() | self.component_status()
        at_preset = None
        presets = {}
        for k, v in self.presets.items():
            presets[k.name] = v

        if self.detected:
            for k in self.presets.keys():
                if self.close_enough(self.presets[k]):
                    at_preset = k.name
                    break

        target_verbal = f"{self.target}"
        if self.target is not None:
            for preset in self.presets:
                if self.target == preset.value:
                    target_verbal = preset.name
                    break

        ret |= {
            'info': self.info,
            'presets': presets,
            'position': self.position if self.connected else None,
            'at_preset': at_preset,
            'target': self.target,
            'target_verbal': target_verbal
        }
        time_stamp(ret)
        return ret

    def close_enough(self, target):
        # logger.info(f"{self._position=}, {target=}")
        return abs(self._position - target) <= self.CLOSE_ENOUGH

    def ontimer(self):
        if not self.detected or not self.stage_lock:
            return

        hw_status = status_t()
        with self.stage_lock:
            result = ximclib.get_status(self.device, byref(hw_status))
        if result != Result.Ok:
            logger.error(f"could not get_status(), {result=}")
            return

        self._position = hw_status.CurPosition

        error_bits = StateFlags.STATE_ERRC|StateFlags.STATE_ERRV|StateFlags.STATE_ERRD
        controller_error = hw_status.Flags & error_bits
        if controller_error:
            logger.error(f"CONTR ERROR 0x{controller_error:08X}")

        security_error = hw_status.Flags & StateFlags.STATE_SECUR
        if security_error:
            logger.error(f"SECUR ERROR 0x{security_error:08X}")

        if hw_status.Flags & StateFlags.STATE_ALARM:
            # should wait for the ALARM cause to go away and then issue a command_stop()
            # for now, just log
            logger.info(f"Detected StateFlags.STATE_ALARM, issuing a STOP command")
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
                    logger.error(f"move command 0x{hw_status.MvCmdSts & MvcmdStatus.MVCMD_NANE_BITS:08X} " +
                                 "ended with MVCMD_ERROR")

            if (self.is_active(StageActivities.StartingUp) and
                    self.close_enough(self.presets[StagePresetPosition.StartUp])):
                self.end_activity(StageActivities.StartingUp)

            if self.is_active(StageActivities.Homing):
                self.end_activity(StageActivities.Homing)

    #
    def move_to_preset(self, preset: Union[Literal['Sky', 'Spec', 'Min', 'Mid', 'Max'] | StagePresetPosition]):
        """
        Starts moving the stage to one of the preset positions

        Parameters
        ----------
        preset
            Name of a preset position

        :mastapi:
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
            logger.info(f'Not moving {self.position=} is close enough to {preset_position=}')
            return

        return self.move_absolute(preset_position)

    def move_absolute(self, position: int | str):
        op = function_name()

        if not self.detected:
            return CanonicalResponse(errors=['not detected'])
        if not self.connected:
            return CanonicalResponse(errors=['not connected'])

        if isinstance(position, str):
            position = int(position)

        if self.close_enough(position):
            logger.info(f'{op}: Not moving {self.position=} is close enough to {position=}')
            return

        if not (self.min_travel <= position < self.max_travel):
            return CanonicalResponse(errors=[f"out of range: {self.min_travel} <= position < {self.max_travel}"])
        try:
            with self.stage_lock:
                response = ximclib.command_move(self.device, position, 0)
                if response != Result.Ok:
                    msg = f'Failed to start stage move absolute (command_move({self.device}, {position})'
                    logger.error(f"{op}: " + msg)
                    return CanonicalResponse(errors=msg)
        except Exception as ex:
            msg = f'Failed to start stage move absolute (command_move({self.device}, {position})'
            logger.exception(f"{op}: " + msg, ex)
            return CanonicalResponse(exception=ex)

        self.ticks_at_start = self.position
        self.target = position
        self.motion_start_time = datetime.datetime.now()
        logger.info(f'{op}: move: from {self.position=} to {self.target=}')
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
        :mastapi:
        """
        op = function_name()

        if isinstance(direction, str):
            direction = StageDirection(stage_direction_str2int_dict[direction])
        if isinstance(amount, str):
            amount = abs(int(amount))

        amount *= 1 if direction == StageDirection.Up else -1
        try:
            self.target = self.position + amount
            self.start_activity(StageActivities.Moving)
            with self.stage_lock:
                response = ximclib.command_movr(self.device, amount, 0)
            if response != Result.Ok:
                msg = f'Failed to start stage move (command_movr({self.device}, {amount})'
                logger.error(f"{op}: " + msg)
                return CanonicalResponse(errors=msg)
        except Exception as ex:
            msg = f'Failed to start stage move relative (command_movr({self.device}, {amount})'
            logger.exception(f"{op}: " + msg, ex)
            return CanonicalResponse(exception=ex)
        return CanonicalResponse_Ok

    def abort(self):
        """
        Aborts any in-progress stage activities

        :mastapi:
        Returns
        -------

        """
        for activity in (StageActivities.StartingUp, StageActivities.Moving, StageActivities.ShuttingDown):
            if self.is_active(activity):
                self.end_activity(activity)

        ximclib.command_stop(self.device)
        return CanonicalResponse_Ok

    @property
    def name(self) -> str:
        return 'stage'

    @property
    def operational(self) -> bool:
        return all([self.is_on(), self.detected, self.connected, not self.was_shut_down,
                    (self.at_preset(StagePresetPosition.Spec) or self.at_preset(StagePresetPosition.Sky))])

    @property
    def why_not_operational(self) -> List[str]:
        label = f'{self.name}'
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
            elif not (self.at_preset(StagePresetPosition.Spec) or self.at_preset(StagePresetPosition.Sky)):
                ret.append(f"not at 'Spec' or 'Sky' preset positions")
        return ret

    @property
    def detected(self) -> bool:
        return self.device != -1

    @property
    def was_shut_down(self) -> bool:
        return self._was_shut_down


base_path = BASE_UNIT_PATH + "/stage"
tag = 'Stage'

stage = Stage(unit=None)


def get_position() -> int:
    return stage.position


def set_position(pos: int):
    stage.position = pos
    return CanonicalResponse_Ok


router = APIRouter()
router.add_api_route(base_path + '/startup', tags=[tag], endpoint=stage.startup)
router.add_api_route(base_path + '/shutdown', tags=[tag], endpoint=stage.shutdown)
router.add_api_route(base_path + '/abort', tags=[tag], endpoint=stage.abort)
router.add_api_route(base_path + '/status', tags=[tag], endpoint=stage.status)
router.add_api_route(base_path + '/position', tags=[tag], endpoint=get_position)
router.add_api_route(base_path + '/position', methods=['PUT'], tags=[tag], endpoint=set_position)
router.add_api_route(base_path + '/connect', tags=[tag], endpoint=stage.connect)
router.add_api_route(base_path + '/disconnect', tags=[tag], endpoint=stage.disconnect)
router.add_api_route(base_path + '/move', tags=[tag], endpoint=stage.move_relative)
router.add_api_route(base_path + '/move_to_preset', tags=[tag], endpoint=stage.move_to_preset)
