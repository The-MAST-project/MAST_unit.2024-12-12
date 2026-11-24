import contextlib
import ctypes
import datetime
import logging
import os
import platform
import sys
import threading
import time
from collections import deque
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from fastapi.routing import APIRouter

from common.activities import StageActivities
from common.canonical import CanonicalResponse, CanonicalResponse_Ok
from common.config.rois import FcuVersion
from common.const import Const
from common.dlipowerswitch import OutletDomain, SwitchedOutlet
from common.endpoints import Completion, Tier, add_api_route, endpoint, register_component_endpoints
from common.interfaces.components import Component
from common.mast_logging import get_logger
from common.models.statuses import StageDrive, StageStatus
from common.utils import RepeatTimer, Timeout, boxed_log, function_name, time_stamp
from stage_port import release_xilab

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

# The DLL search path is a Windows concern; the import below is not. Splitting them is what
# lets this module be imported where pyximc resolves but Windows does not -- previously the
# names came in only inside the platform test, while the module body used them unconditionally.
if sys.platform == "win32":  # not platform.system(): only this narrows for type checkers (add_dll_directory)
    # Determining the directory with dependencies for windows depending on the bit depth.
    arch_dir = "win64" if "64" in platform.architecture()[0] else "win32"
    lib_dir = ximc_top_dir / arch_dir  # lib directory for ximc library
    if not lib_dir.exists():
        raise FileNotFoundError(f"Directory with ximc library not found: {lib_dir=}. ")
    os.add_dll_directory(str(lib_dir))  # add dll path into an environment variable

# Must follow `add_dll_directory` above, which is what makes the ximc DLL resolvable on
# Windows, so it cannot move to the top of the file.
from pyximc import (  # noqa: E402
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
    chart_data_t,
    device_information_t,
    edges_settings_t,
    secure_settings_t,
    serial_number_t,
    status_t,
    string_at,
)
from pyximc import lib as ximclib  # type: ignore[name]  # noqa: E402

RESULT_MAP = {
    Result.Ok: "Ok",
    Result.Error: "error",
    Result.NotImplemented: "NotImplemented",
    Result.ValueError: "ValueError",
    Result.NoDevice: "NoDevice",
}

#: The controller's command-error flags, by value (pyximc's `StateFlags`; literal so this
#: module's scope needs no arithmetic on what is a stub off a unit). They latch, and the
#: controller keeps no record of which command set one -- `status_t` has no field for it and
#: libximc no "last error" call. The only way to name the culprit is to read the flags after
#: every call, which `Stage._call` does.
CONTROLLER_ERROR_FLAGS: dict[int, str] = {
    0x1: "STATE_ERRC (command not recognised by the controller)",
    0x2: "STATE_ERRD (data integrity: the command's CRC did not match)",
    0x4: "STATE_ERRV (value out of range: the controller used a corrected one)",
}
CONTROLLER_ERROR_MASK = 0x7


def describe_drive(drive: StageDrive) -> str:
    """One line of the controller's electrical readings, for the log."""

    def fmt(value, spec: str = "") -> str:
        return "?" if value is None else format(value, spec)

    return (
        f"supply {fmt(drive.supply_volts, '.2f')} V / {fmt(drive.supply_milliamps)} mA, "
        f"power stage {fmt(drive.power_state)}, duty {fmt(drive.duty_cycle)}, "
        f"winding {fmt(drive.winding_volts, '.2f')} V / {fmt(drive.winding_milliamps)} mA, "
        f"encoder {fmt(drive.encoder_position)}"
    )


def describe_controller_errors(flags: int) -> str:
    """The controller-error bits set in `flags`, named, comma-separated."""
    return ", ".join(text for bit, text in CONTROLLER_ERROR_FLAGS.items() if flags & bit)


# libximc's own log, which went to its default (stderr / syslog) and so was read by nobody.
# Its levels are LOGLEVEL_ERROR..LOGLEVEL_DEBUG = 1..4 (ximc.h); INFO goes to DEBUG with DEBUG,
# so a unit at INFO hears only the library's warnings and errors.
_LIBXIMC_LEVELS = {1: logging.ERROR, 2: logging.WARNING, 3: logging.DEBUG, 4: logging.DEBUG}
_libximc_logger = get_logger(f"{__name__}.libximc")
# `XIMC_CALLCONV` is __stdcall on Windows (ximc.h). On x64 the two conventions coincide.
_LoggingCallback = ctypes.WINFUNCTYPE if sys.platform == "win32" else ctypes.CFUNCTYPE


@_LoggingCallback(None, ctypes.c_int, ctypes.c_wchar_p, ctypes.c_void_p)
def _forward_libximc_log(loglevel, message, user_data):  # noqa: ARG001 -- the C signature
    """Forward one libximc message to `mast.stage.libximc`. Runs on the library's thread."""
    with contextlib.suppress(Exception):  # an exception must never unwind into C
        _libximc_logger.log(_LIBXIMC_LEVELS.get(loglevel, logging.WARNING), f"libximc: {message}")


try:
    # The module-level function object is the reference that keeps the callback alive.
    ximclib.set_logging_callback(_forward_libximc_log, None)
except Exception as ex:  # noqa: BLE001 -- diagnostics only; the stage works without it
    logger.warning(f"cannot route libximc's log into ours: {ex!r}")


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
    """Preset stage positions: `sky`, `spec`, `min`, `mid`, `max`.

    `min` is the lowest position the stage is parked at, not the end of travel: it is kept
    `MIN_PRESET_CLEARANCE` steps clear of the lower border (see `Stage.presets`).
    """

    Sky = "sky"
    Spec = "spec"
    Min = "min"
    Middle = "mid"
    Max = "max"


#: How far from the end of travel the `Min` preset stays. On limit-switch borders `min_travel` is 0,
#: which is the switch itself; parking the mirror exactly there leans on it. `Min` is therefore
#: `max(min_travel, MIN_PRESET_CLEARANCE)`. `Middle` is still computed from the real `min_travel`.
MIN_PRESET_CLEARANCE = 100

#: Where the stage is sent on startup. Deliberately not an enum member: as an alias of `Sky`
#: it published a duplicate "sky" in the OpenAPI enum, and it is a policy choice rather than
#: a position anyone can command.
STARTUP_PRESET = StagePresetPosition.Sky

#: How long a homing may run before `_advance_homing` stops it and calls it failed. From the
#: SPEC end of travel homing outlasts a minute: on mast03 on 2026-10-12 it was still running
#: at 60 s with about a fifth of the travel left, so this must cover a full-travel homing.
HOMING_TIMEOUT_SECONDS = 150.0

#: The controller's power stage, by `status_t.PWRSts` (pyximc's `PowerState`; literal for the
#: same reason as `CONTROLLER_ERROR_FLAGS`).
POWER_STATES: dict[int, str] = {0x00: "unknown", 0x01: "off", 0x03: "normal", 0x04: "reduced", 0x05: "max"}

#: Two drive faults the controller does not flag, both seen on mast07 on 2026-11-24. Each is
#: reported once per move, after this many consecutive status reads (`ontimer`, 2 s apart),
#: so the ramp-up at the start of a move is not mistaken for one.
DRIVE_FAULT_READS = 2
#: A winding at this voltage or more is being driven ...
DRIVEN_WINDING_VOLTS = 1.0
#: ... and below this current nothing flows through it: an open motor circuit. A DC motor
#: driven at a few volts, moving or stalled, draws hundreds of mA.
NO_CURRENT_MILLIAMPS = 20

#: The preset names the API accepts, for anyone building a UI over it.
stage_position_names: list[str] = [p.value for p in StagePresetPosition]


class Stage(Component, SwitchedOutlet):
    _instance = None
    _initialized = False
    #: The controller-error bits (`CONTROLLER_ERROR_MASK`) at the last read; see `_call`.
    _controller_errors: int = 0
    #: `time.monotonic()` when the current homing was commanded, None when none is under way.
    _homing_started_at: float | None = None
    #: Why the last homing failed; None once one succeeds. See `home`.
    _homing_error: str | None = None
    #: The controller's electrical readings at the last `ontimer`; see `_read_drive`.
    _drive: StageDrive | None = None
    #: Consecutive status reads on which each drive fault was seen, and the faults already
    #: reported during the current move. Both reset when no move is running.
    _drive_fault_reads: dict[str, int] | None = None
    _drive_faults_reported: set[str] | None = None

    # Keyed by the controller's flag *values*: `StateFlags` is a plain class of int constants, not an enum.
    state_flags_dict: ClassVar[dict[int, str]] = {
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

    @property
    def conf(self):
        """This component's configuration, live.

        Was snapshotted in ``__init__``, which is why a value edited in the database
        reached a running unit only at the next service restart. Within one configuration
        generation this returns the same object every time, so it is a memo lookup, not a
        rebuild -- which is what makes a property affordable here.
        """
        assert self.unit is not None and self.unit.unit_conf is not None
        return self.unit.unit_conf.stage

    @property
    def stage_model(self) -> str | None:
        """The controller's model, read live from the configuration (never snapshotted)."""
        return self.conf.model

    @property
    def presets(self) -> dict[StagePresetPosition, int]:
        """The preset positions, live.

        Two sources, which is why this was a dict built in ``__init__`` and then added to
        after enumeration: `sky` and `spec` are configuration, and were snapshotted; the
        travel-derived three are not configuration at all -- they come from the
        controller's edge settings and are known only once the device answers.
        """
        presets = {
            StagePresetPosition.Sky: self.conf.presets.sky,
            StagePresetPosition.Spec: self.conf.presets.spec,
        }
        if self.min_travel is not None and self.max_travel is not None:
            presets[StagePresetPosition.Min] = max(self.min_travel, MIN_PRESET_CLEARANCE)
            presets[StagePresetPosition.Max] = self.max_travel
            presets[StagePresetPosition.Middle] = int((self.max_travel - self.min_travel) / 2)
        return presets

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, unit: "Unit"):  # type: ignore  # noqa: C901
        if self._initialized:
            return

        op = "Stage.__init__"
        self.unit = unit
        if not (unit and unit.unit_conf and unit.unit_conf.stage):
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

        # This is device search and enumeration with probing. It gives more information about devices.
        probe_flags = EnumerateFlags.ENUMERATE_PROBE
        ports = self.find_ximc_ports()
        # XILab holds the port while it is connected, and the scan below then finds nothing and
        # is never repeated.  End it first; if the scan still finds nothing the device is gone.
        release_xilab(ports)
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

        self._baseline_controller_errors()

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

        serial_number = serial_number_t()
        result = self._call("get_serial_number", byref(serial_number))
        if result == Result.Ok:
            self.serial_number = serial_number.SN
        else:
            logger.warning(f"{op}: cannot get serial number ({result=})")

        # self.set_profile()  # FUTURE: set motion profile parameters for known stage models

        x_device_information = device_information_t()
        result = self._call("get_device_information", byref(x_device_information))

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
        result = self._call("get_edges_settings", byref(x_edges_settings))
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
        result = self._call("get_secure_settings", byref(self.secure_settings))
        if result != Result.Ok:
            logger.warning(f"{op}: cannot get secure settings ({result=})")

        status = status_t()
        result = ximclib.get_status(self.device, byref(status))
        if result == Result.Ok:
            if status.Flags & StateFlags.STATE_EEPROM_CONNECTED:
                self.info["eeprom_connected"] = True
            else:
                self.info["eeprom_connected"] = False

        device_info = (
            f"port='{comport}', manufacturer='{self.info['controller']}', product='{self.info['product']}', "
            + f"version='{self.info['version']}', model='{self.conf.model}', serial={self.serial_number}, "
            + f"fcu_version='{self.fcu_version.value}', "
            + f"EEPROM connected={self.info.get('eeprom_connected', 'unknown')}, "
            + f"range={self.min_travel}..{self.max_travel} (borders by: {self.border_by}), "
            + f"close_enough={self.conf.close_enough}"
        )
        self.stage_lock = threading.Lock()

        # get initial values from the hardware
        hw_status = status_t()
        with self.stage_lock:
            result = ximclib.get_status(self.device, byref(hw_status))
        if result == Result.Ok:
            self._position = hw_status.CurPosition
            self.is_moving = (hw_status.MvCmdSts & MvcmdStatus.MVCMD_RUNNING) != 0

        self.latest_positions = deque(maxlen=3)

        self.timer = RepeatTimer(2, function=self.ontimer)
        self.timer.name = "stage-timer-thread"
        self.timer.start()

        logger.info(f"detected: {device_info}")

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

    def _call(self, name: str, *args) -> int:
        """`ximclib.<name>(self.device, *args)`, naming the call if it fails or sets a controller error.

        Takes no lock: callers hold ``stage_lock`` exactly where they did before. The status read
        that follows is what attributes a newly set ERRC/ERRD/ERRV to this call; skipped once all
        three are already set, when there is nothing left to attribute.
        """
        assert ximclib
        result = getattr(ximclib, name)(self.device, *args)
        call = f"{name}({', '.join(str(a) for a in args if isinstance(a, int))})"
        if result != Result.Ok:
            logger.error(f"{call}: returned {RESULT_MAP.get(result, result)}")
        if self._controller_errors != CONTROLLER_ERROR_MASK:
            status = status_t()
            if ximclib.get_status(self.device, byref(status)) == Result.Ok:
                self._note_controller_errors(status.Flags, after=call)
        return result

    def _note_controller_errors(self, flags: int, after: str) -> None:
        """Log the controller-error bits that changed since the last read, and remember them.

        Once per change, not once per read: latched, they used to fill a night's log with the same
        line every 2 s (965 times on mast07, 2026-11-23) while saying nothing about the cause.
        """
        current = flags & CONTROLLER_ERROR_MASK
        raised = current & ~self._controller_errors
        cleared = self._controller_errors & ~current
        self._controller_errors = current
        if raised:
            logger.error(f"controller error after {after}: {describe_controller_errors(raised)}")
        if cleared:
            logger.info(f"controller error cleared: {describe_controller_errors(cleared)}")

    def _baseline_controller_errors(self) -> None:
        """Read the controller-error bits just after `open_device`, before this session sends anything.

        Bits set here were not set by any call this process made: they are from an earlier session
        (they latch until the controller is power-cycled), or from enumeration / `open_device` itself.
        """
        assert ximclib
        self._controller_errors = 0
        status = status_t()
        if ximclib.get_status(self.device, byref(status)) != Result.Ok:
            return
        self._controller_errors = status.Flags & CONTROLLER_ERROR_MASK
        if self._controller_errors:
            logger.warning(
                f"controller error already set on opening the device: {describe_controller_errors(self._controller_errors)}"
                " -- left by an earlier session or by open_device; power-cycle the stage to clear it"
            )

    def home(self) -> CanonicalResponse:
        """Start homing and return at once. `Homing` ends when the stage is home and zeroed, or homing failed.

        `command_home` only starts it; `ontimer` sends `command_zero` once the stage has stopped,
        or stops it after `HOMING_TIMEOUT_SECONDS` (`_advance_homing`). A failure is kept in
        `homing_error` and makes the stage non-operational until a homing succeeds.

        This used to be `command_homezero`, which is libximc's home + wait-for-stop + zero in one
        blocking call, made under ``stage_lock`` from `Stage.__init__`. A stage that does not home
        therefore held up the unit's whole startup for the full timeout and blinded the status
        timer, which needs the lock, for the same 150 s: mast07 logged nothing for 2.5 minutes on
        every start of 2026-11-23.
        """
        op = function_name()
        if self.is_active(StageActivities.Homing):
            return CanonicalResponse_Ok
        with self.stage_lock:
            result = self._call("command_home")
            if result != Result.Ok:
                self._homing_error = f"command_home returned {RESULT_MAP.get(result, result)}"
                return CanonicalResponse(errors=[f"{op}: {self._homing_error}"])
            self._homing_error = None
            # Under the lock, after the command: a status read that sees this set was taken after
            # homing began, so its "not running" means finished, not "not started yet".
            self._homing_started_at = time.monotonic()
            # Homing moves the stage, so the staleness `move_absolute` describes applies here too.
            self.is_moving = True
            self.start_activity(StageActivities.Homing)
        logger.info(f"{op}: homing started")
        return CanonicalResponse_Ok

    @property
    def homing_error(self) -> str | None:
        """Why the last homing failed, or None if it succeeded (or none has finished yet)."""
        return self._homing_error

    def _advance_homing(self, hw_status: status_t, read_after_homing_began: bool) -> None:
        """Finish a homing from the timer: zero once stopped, or stop it once it has run too long."""
        started = self._homing_started_at
        if started is None or not self.is_active(StageActivities.Homing):
            return
        running = (hw_status.MvCmdSts & MvcmdStatus.MVCMD_RUNNING) != 0
        if read_after_homing_began and not running:
            if hw_status.MvCmdSts & MvcmdStatus.MVCMD_ERROR:
                self._end_homing(f"homing ended with MVCMD_ERROR (MvCmdSts=0x{hw_status.MvCmdSts:02X})")
                return
            with self.stage_lock:
                result = self._call("command_zero")
            self._end_homing(None if result == Result.Ok else f"command_zero returned {RESULT_MAP.get(result, result)}")
        elif time.monotonic() - started > HOMING_TIMEOUT_SECONDS:
            # Stopped, not abandoned: an abandoned homing would still zero the stage when it
            # finished, in the middle of whatever move came next (MAST_unit#311).
            with self.stage_lock:
                self._call("command_stop")
            self._end_homing(
                f"not home within {HOMING_TIMEOUT_SECONDS:.0f} s (stopped at {hw_status.CurPosition}); "
                "the motor may not be driven -- check its supply and cable"
                + ("" if self._drive is None else f" (last reading: {describe_drive(self._drive)})")
            )

    def _end_homing(self, error: str | None) -> None:
        """End `Homing`, keep its outcome, and take a startup waiting on it on to its next step."""
        op = function_name()
        self._homing_started_at = None
        self._homing_error = error
        if error:
            logger.error(f"{op}: homing failed: {error}")
        else:
            logger.info(f"{op}: home, and zeroed")
        self.end_activity(StageActivities.Homing)

        if self.is_active(StageActivities.StartingUp):
            if error:
                logger.error(f"{op}: startup: not moving to '{STARTUP_PRESET.value}', homing failed")
                self.end_activity(StageActivities.StartingUp)
            else:
                self.move_to_preset(STARTUP_PRESET)

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

        # Idempotent. `open_device` on an already-open device lost it: the stage then read
        # disconnected, its `ontimer` returned early, and an `Aborting` raised by the shutdown
        # could never end (mast00, 2026-10-12).
        if bool(value) == self.connected:
            return

        assert ximclib
        assert self.device
        if value:
            self.device = ximclib.open_device(self.device_uri)
            if self.detected:
                self._baseline_controller_errors()
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

    @endpoint(tier=Tier.INTERFACE, completion=StageActivities.StartingUp)
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
        if self._homing_error is not None and not self.is_active(StageActivities.Homing):
            # A position counted from a failed homing means nothing; try again first.
            self.home()
        if self.is_active(StageActivities.Homing):
            # A move now would cut the homing short. `_end_homing` makes it once homing is over.
            self.start_activity(StageActivities.StartingUp, existing_ok=True)
            logger.info(f"startup: moving to '{STARTUP_PRESET.value}' once homing ends")
        elif not self.at_preset(StagePresetPosition.Sky):
            self.start_activity(StageActivities.StartingUp)
            self.move_to_preset(StagePresetPosition.Sky)
        return CanonicalResponse_Ok

    @endpoint(tier=Tier.INTERFACE, completion=Completion.IMMEDIATE)
    def shutdown(self):
        """
        Shutdown routine for the **MAST** stage.  Makes it ``idle``
        """
        self.disconnect()
        self._was_shut_down = True
        # The machine's power_down_on_shutdown (opmode-design 4a); off by default.
        if self._power_down_on_shutdown():
            self.powerdown()
        return CanonicalResponse_Ok

    def _power_down_on_shutdown(self) -> bool:
        conf = getattr(getattr(self, "unit", None), "unit_conf", None)
        return bool(getattr(conf, "power_down_on_shutdown", False))

    @property
    def is_shutting_down(self) -> bool:
        return False

    def powerdown(self):
        if not self._was_shut_down:
            self.shutdown()
        self.power_off()

    def at_preset(self, preset: StagePresetPosition) -> bool:
        """At `preset`, and stopped there.

        Not while moving: within `close_enough` of a preset is not at it while the stage is
        still leaving it or still arriving -- a wait on this (flux metering's, for SPEC) used
        to return with the mirror still moving. `is_moving` is set when a move is commanded
        and cleared by the 2-second `ontimer`, so this lags the stop by up to that.
        """
        if self.is_moving:
            return False
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

    def at_preset_name(self) -> str | None:
        """The preset the stage is currently parked at, as the API spells it.

        `value`, not `name.lower()`: the latter reported "middle" for a preset the API calls
        "mid", so what status said did not round-trip into `move_to_preset` (#85).

        None while moving, as `at_preset`: the preset being left, or being approached, is not
        where the stage is. `target_verbal` says where it is going.
        """
        if not self.detected:
            return None
        if self.is_moving:
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

    @endpoint(tier=Tier.INTERFACE, completion=Completion.IMMEDIATE)
    def status(self) -> StageStatus:
        at_preset = self.at_preset_name()
        target_verbal = self.target_preset_name()

        return StageStatus(
            **self.power_status().model_dump(),
            **self.component_status().model_dump(),
            drive=self._drive,
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

    def _end_abort_when_at_rest(self) -> None:
        """End `Aborting` once `MVCMD_RUNNING` clears. Not `is_stationary`, which is broken (#150)."""
        if self.is_active(StageActivities.Aborting) and not self.is_moving:
            self.end_activity(StageActivities.Aborting)

    @property
    def is_stationary(self) -> bool:
        """
        Returns True if the stage was at the same position for the last
          self.latest_positions.maxlen readings (every 2 seconds by ontimer).
        """
        return len(self.latest_positions) == self.latest_positions.maxlen and all(
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

        # First: `status` predates the `get_secure_settings` below, whose own read would otherwise
        # be noted before this older one and log every bit it saw as cleared and raised again.
        # A bit seen here first was set by nothing that went through `_call`.
        self._note_controller_errors(status.Flags, after="no call made through Stage._call (periodic status read)")

        secure_settings = secure_settings_t()
        with self.stage_lock:
            result = self._call("get_secure_settings", byref(secure_settings))
            if result == Result.Ok:
                if status.CurT > secure_settings.CriticalT:
                    logger.warning(
                        f"{op}: WARNING: controller temperature {(status.CurT / 10):.2f}C "
                        + f"exceeds critical temperature {(secure_settings.CriticalT / 10):.2f}C"
                    )
                    logger.debug(f"{op}: secure settings: {secure_settings:x}")
            else:
                logger.warning(f"{op}: cannot get secure settings ({result=})")

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
                    result = self._call("command_stop")
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
            read_after_homing_began = self._homing_started_at is not None
            if result == Result.Ok:
                self._drive = self._read_drive(hw_status)
        if result != Result.Ok:
            logger.error(f"{op}: could not get_status(), {result=} ({RESULT_MAP[result]})")
            return

        self.check_current_status(hw_status)
        self._watch_drive(self._drive, running=(hw_status.MvCmdSts & MvcmdStatus.MVCMD_RUNNING) != 0)

        self._position = hw_status.CurPosition
        self.latest_positions.append(self._position)

        self.is_moving = (hw_status.MvCmdSts & MvcmdStatus.MVCMD_RUNNING) != 0

        self._end_abort_when_at_rest()

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
                        self._clear_mvcmd_error(hw_status)

            if self.is_active(StageActivities.StartingUp) and self.close_enough(self.presets[STARTUP_PRESET]):
                self.end_activity(StageActivities.StartingUp)

        self._advance_homing(hw_status, read_after_homing_began)

    def _read_drive(self, hw_status: status_t) -> StageDrive:
        """The controller's electrical readings: `hw_status`'s, and `get_chart_data`'s.

        The caller holds ``stage_lock``. A failed chart read leaves the winding fields None
        rather than failing the status read it rides on.
        """
        chart = chart_data_t()
        try:
            charted = ximclib.get_chart_data(self.device, byref(chart)) == Result.Ok
        except Exception:  # noqa: BLE001 -- a diagnostic read must never break ontimer
            charted = False
        upwr = getattr(hw_status, "Upwr", None)
        pwr = getattr(hw_status, "PWRSts", None)
        return StageDrive(
            supply_volts=None if upwr is None else upwr / 100,  # tens of mV
            supply_milliamps=getattr(hw_status, "Ipwr", None),
            power_state=None if pwr is None else POWER_STATES.get(pwr, f"0x{pwr:x}"),
            duty_cycle=chart.DutyCycle if charted else None,
            winding_volts=chart.WindingVoltageA / 100 if charted else None,  # tens of mV
            winding_milliamps=chart.WindingCurrentA if charted else None,
            encoder_position=getattr(hw_status, "EncPosition", None),
        )

    def _drive_faults(self, drive: StageDrive) -> dict[str, str]:
        """The drive faults `drive` shows while a move runs, by name, with what to do about each."""
        faults = {}
        if drive.power_state == "off":
            faults["power stage off"] = (
                "a move is running with the motor power stage off, so the motor is not driven; "
                "power-cycle the stage controller"
            )
        if (
            drive.power_state in ("normal", "reduced", "max")
            and drive.winding_volts is not None
            and drive.winding_milliamps is not None
            and abs(drive.winding_volts) >= DRIVEN_WINDING_VOLTS
            and abs(drive.winding_milliamps) < NO_CURRENT_MILLIAMPS
        ):
            faults["no motor current"] = (
                f"the motor is driven at {drive.winding_volts:.2f} V but draws {drive.winding_milliamps} mA: "
                "the motor circuit is open -- check the motor connector, cable and winding"
            )
        return faults

    def _watch_drive(self, drive: StageDrive | None, running: bool) -> None:
        """Report each drive fault once per move, once it has lasted `DRIVE_FAULT_READS` reads.

        Neither fault raises a controller flag, so without this a move just runs out its
        timeout with the position unchanged and nothing in the log to say why.
        """
        if self._drive_fault_reads is None or self._drive_faults_reported is None:
            self._drive_fault_reads, self._drive_faults_reported = {}, set()
        if not running or drive is None:
            self._drive_fault_reads.clear()
            self._drive_faults_reported.clear()
            return
        faults = self._drive_faults(drive)
        for name in list(self._drive_fault_reads):
            if name not in faults:
                del self._drive_fault_reads[name]
        for name, text in faults.items():
            self._drive_fault_reads[name] = self._drive_fault_reads.get(name, 0) + 1
            if self._drive_fault_reads[name] >= DRIVE_FAULT_READS and name not in self._drive_faults_reported:
                self._drive_faults_reported.add(name)
                logger.error(f"drive fault: {text} ({describe_drive(drive)})")

    def _clear_mvcmd_error(self, hw_status: status_t, attempts: int = 3) -> bool:
        """Clear a move command's MVCMD_ERROR with `command_stop()`; True once it is clear.

        The caller holds ``stage_lock``.  A stop leaves the command bits at MVCMD_STOP with the
        error bit clear (``MvCmdSts == 0x05`` on mast02 and mast03), so the error is cleared
        when the bit is *no longer* set.  This used to test the opposite, and so never saw the
        success: it ran every attempt, and logged none as cleared.
        """
        op = function_name()
        assert ximclib
        for i in range(attempts):
            logger.error(f"{op}: attempt #{i} (of {attempts}): attempting to clear MVCMD_ERROR by calling command_stop()")
            self._call("command_stop")
            time.sleep(0.2)
            result = ximclib.get_status(self.device, byref(hw_status))
            if result != Result.Ok:
                logger.error(f"{op}: attempt #{i} (of {attempts}): could not get_status(), {result=} ({RESULT_MAP[result]})")
                return False
            logger.error(f"{op}: attempt #{i} (of {attempts}): status after command_stop(): {hw_status.MvCmdSts=:08X}")
            if (hw_status.MvCmdSts & MvcmdStatus.MVCMD_ERROR) == 0:
                logger.info(f"{op}: attempt #{i} (of {attempts}): successfully cleared MVCMD_ERROR")
                return True
        logger.error(f"{op}: MVCMD_ERROR still set after {attempts} attempts")
        return False

    @endpoint(tier=Tier.OPERATION, completion=StageActivities.Moving)
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
        if self.is_active(StageActivities.Homing):
            return CanonicalResponse(errors=[f"{op}: homing in progress; a move now would cut it short"])
        try:
            with self.stage_lock:
                assert ximclib
                response = self._call("command_move", position, 0)
                if response != Result.Ok:
                    msg = f"{op}: Failed to start stage move absolute (command_move({self.device}, {position}), {response=}"
                    logger.error(msg)
                    return CanonicalResponse(errors=[msg])
        except Exception as ex:
            msg = f"{op}: Failed to start stage move absolute (command_move({self.device}, {position}), {ex=}"
            logger.exception(msg)
            return CanonicalResponse(errors=[msg])

        # Same reason as the focuser: stationarity has to be concluded from samples taken
        # after the command, or the pre-move readings report the stage as settled before it
        # has started (#150).
        self.latest_positions.clear()
        self.ticks_at_start = self.position
        self.target = position
        self.motion_start_time = datetime.datetime.now(datetime.UTC)
        # Set HERE, not left to `ontimer`. `is_moving` is refreshed only by that 2-second
        # poll, so between issuing the command and the next tick it still reads False --
        # and `while stage.is_moving` therefore falls straight through on a move that has
        # only just begun. On 2026-09-02 `acquirer._await_stage` returned 6 ms into a
        # 133,000-count traverse and the acquisition exposed with the fold mirror in
        # transit. The flag now means "a move has been commanded and not yet observed to
        # finish", which is what every caller already assumed it meant.
        self.is_moving = True
        self.start_activity(StageActivities.Moving, details=[f"from {self.position} to {self.target}"])

        return CanonicalResponse_Ok

    @endpoint(tier=Tier.OPERATION, completion=StageActivities.Moving)
    def move_relative(self, amount: int):
        """
        Starts moving the stage by a signed number of native units.

        Parameters
        ----------
        amount
            How far to move, and which way: positive is away from the motor, negative is
            towards it. The sign carries what the `direction` parameter used to (#41).
        """
        op = function_name()

        current_position = self.position
        if current_position is None:
            return CanonicalResponse(errors=["cannot get current position"])
        if self.is_active(StageActivities.Homing):
            return CanonicalResponse(errors=[f"{op}: homing in progress; a move now would cut it short"])

        try:
            self.target = current_position + amount
            self.is_moving = True  # before the command, for the reason in `move_absolute`
            self.start_activity(StageActivities.Moving, details=[f"from {self.position} to {self.target}"])
            with self.stage_lock:
                assert ximclib
                response = self._call("command_movr", amount, 0)
            if response != Result.Ok:
                msg = f"Failed to start stage move (command_movr({self.device}, {amount})"
                logger.error(f"{op}: " + msg)
                return CanonicalResponse(errors=[msg])
        except Exception as ex:
            msg = f"{op}: Failed to start stage move relative (command_movr({self.device}, {amount}), {ex=}"
            logger.exception(msg)
            return CanonicalResponse(errors=[msg])
        return CanonicalResponse_Ok

    @endpoint(tier=Tier.INTERFACE, completion=StageActivities.Aborting)
    def abort(self):
        """
        Aborts any in-progress stage activities
        """
        if self.is_active(StageActivities.Homing):
            self._homing_started_at = None
            self._homing_error = "homing aborted"
            self.end_activity(StageActivities.Homing)
        for activity in (
            StageActivities.StartingUp,
            StageActivities.Moving,
            StageActivities.ShuttingDown,
        ):
            if self.is_active(activity):
                self.end_activity(activity)

        self.start_activity(StageActivities.Aborting)
        assert ximclib
        self._call("command_stop")
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
                (self.at_preset(StagePresetPosition.Spec) or self.at_preset(StagePresetPosition.Sky)),
                self._currently_operational,
                not self.is_active(StageActivities.Homing),
                # A position counted from a failed homing is not one to work at.
                self._homing_error is None,
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
            elif self.is_active(StageActivities.Homing):
                ret.append(f"{label}: homing")
            elif self._homing_error is not None:
                ret.append(f"{label}: homing failed: {self._homing_error}")
            elif self.is_moving:
                # `at_preset` is False while moving; "not at a preset" would read like a fault.
                ret.append(f"{label}: moving to '{self.target_preset_name()}'")
            elif not (self.at_preset(StagePresetPosition.Spec) or self.at_preset(StagePresetPosition.Sky)):
                ret.append(
                    f"{label}: at {self.position}, not at '{StagePresetPosition.Spec.value}' "
                    + f"({self.presets[StagePresetPosition.Spec]}) or '{StagePresetPosition.Sky.value}' "
                    + f"({self.presets[StagePresetPosition.Sky]}) preset positions"
                )
            if not self._currently_operational:
                ret.extend(self._why_not_currently_operational)
        return ret

    @property
    def detected(self) -> bool:
        return self.device != -1

    @property
    def was_shut_down(self) -> bool:
        return self._was_shut_down

    @endpoint(tier=Tier.OPERATION, completion=Completion.IMMEDIATE)
    def endpoint_get_position(self) -> CanonicalResponse:
        return self.get_position()

    def get_position(self) -> CanonicalResponse:
        return CanonicalResponse(value=self.position)

    @endpoint(tier=Tier.OPERATION, completion=StageActivities.Moving)
    def endpoint_set_position(self, position: int):
        return self.set_position(position)

    def set_position(self, position: int) -> CanonicalResponse:
        """`PUT /stage/position`: the only way to command an absolute position over the API.

        It used to assign the `position` property, which is a second implementation of the
        same move that never acquired `move_absolute`'s guards -- so the one route an
        operator has for an absolute move was the one that skipped the travel-limit check
        and could drive the stage past `max_travel`. It also raised rather than returning,
        so a refusal surfaced as a 500 with a traceback, while this returned `Ok`
        unconditionally regardless of what happened.
        """
        return self.move_absolute(position)

    @property
    def api_router(self) -> APIRouter:
        base_stage_path = Const.BASE_UNIT_PATH + "/stage"

        router = APIRouter()
        register_component_endpoints(router, self, base_stage_path)
        add_api_route(
            router,
            base_stage_path + "/position",
            endpoint=self.endpoint_get_position,
        )
        add_api_route(
            router,
            base_stage_path + "/position",
            methods=["PUT"],
            endpoint=self.endpoint_set_position,
        )
        add_api_route(router, base_stage_path + "/move_relative", endpoint=self.move_relative, methods=["PUT"])
        add_api_route(
            router,
            base_stage_path + "/move_to_preset",
            endpoint=self.move_to_preset,
            methods=["PUT"],
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
