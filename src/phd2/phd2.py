import copy
import json
import logging
import math
import selectors
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import IntFlag, auto
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from astropy.coordinates import Angle
from pydantic import BaseModel

import common.ASI as ASI
from common.activities import ImagerActivities, UnitActivities
from common.canonical import CanonicalResponse, CanonicalResponse_Ok
from common.config import Config
from common.dlipowerswitch import OutletDomain, SwitchedOutlet
from common.interfaces.guiding import GuiderInterface
from common.interfaces.imager import ImagerExposureSeries, ImagerInterface, ImagerRoi, ImagerSettings
from common.mast_logging import init_log
from common.process import WatchedProcess
from common.utils import Coord, RepeatTimer, boxed_info, function_name
from science.sky_quality import FrameMetrics, SeeingQualityWhilePHD2Guiding

if TYPE_CHECKING:
    pass  # type: ignore[name-defined]

logger = logging.Logger("mast.unit." + __name__)
init_log(logger)


class CoolerStatus(BaseModel):
    temperature: float
    coolerOn: bool  # noqa: N815
    setpoint: float
    power: float


class SettleModel(BaseModel):
    pixels: int = 0
    time: int = 0
    timeout: int = 0


class PHD2Configuration(BaseModel):
    profile: str = "PWI4+ASI-native"
    settle: SettleModel


class PHD2Activities(IntFlag):
    Idle = 0
    Guiding = auto()
    Settling = auto()
    Calibrating = auto()
    Looping = auto()
    Saving = auto()
    Validating = auto()
    ExposingForValidation = auto()
    SolvingForValidation = auto()
    EquipmentHandover = auto()
    EquipmentTakeover = auto()


class PHD2ImagerStatus(BaseModel):
    identifier: str | None = None
    name: str = "phd2"
    activities: int = 0
    activities_verbal: str | None = None
    operational: bool = False
    why_not_operational: list[str] = []
    connected: bool = False

class SkyQualityStatus(BaseModel):
    score: float | None = None
    state: str | None = None
    latest_update: str | None = None

class PHD2GuiderStatus(BaseModel):
    identifier: str | None = None
    is_guiding: bool = False
    is_settling: bool = False
    app_state: str | None = None
    avg_dist: float | None = None
    sky_quality: SkyQualityStatus | None = None


class PHD2SettleProgress:
    """Info related to progress of settling after guiding starts or after
    a dither

    """

    def __init__(self):
        self.done = False
        self.distance = 0.0
        self.settle_px = 0.0
        self.time = 0.0
        self.settle_time = 0.0
        self.status = 0
        self.error = ""


class PHD2GuideStats:
    """cumulative guide stats since guiding started and settling
    completed

    """

    def __init__(self):
        self.rms_tot = 0.0
        self.rms_ra = 0.0
        self.rms_dec = 0.0
        self.peak_ra = 0.0
        self.peak_dec = 0.0


class PHD2ConnectorError(Exception):
    """GuiderException is the base class for any excettions raied by the
    Guider methods

    """

    pass


class PHD2Accumulator:
    def __init__(self):
        self.n = 0
        self.a = self.q = self._peak = 0
        self.reset()

    def reset(self):
        self.n = 0
        self.a = self.q = self._peak = 0

    def add(self, x):
        ax = abs(x)
        if ax > self._peak:
            self._peak = ax
        self.n += 1
        d = x - self.a
        self.a += d / self.n
        self.q += (x - self.a) * d

    def mean(self):
        return self.a

    def stdev(self):
        return math.sqrt(self.q / self.n) if self.n >= 1 else 0.0


class PHD2Connection:
    def __init__(self):
        self.lines = []
        self.buf = b""
        self.sock = None
        self.sel = None
        self._terminate = False

    def __del__(self):
        self.disconnect()

    def connect(self, hostname, port):
        self.sock = socket.socket()
        try:
            self.sock.connect((hostname, port))
            self.sock.setblocking(False)  # non-blocking
            self.sel = selectors.DefaultSelector()
            self.sel.register(self.sock, selectors.EVENT_READ)
        except Exception:
            self.sel = None
            self.sock = None
            raise

    def disconnect(self):
        if self.sel is not None and self.sock:
            self.sel.unregister(self.sock)
            self.sel = None
        if self.sock is not None:
            self.sock.close()
            self.sock = None

    def is_connected(self):
        return self.sock is not None

    def read_line(self):  # noqa: C901
        # print(f"DBG: ReadLine enter lines:{len(self.lines)}")
        if not self.sock:
            raise RuntimeError("socket not connected")
        if not self.sel:
            raise RuntimeError("selector not initialized")

        while not self.lines:
            # print("DBG: begin wait")
            while True:
                if self._terminate:
                    return ""
                events = self.sel.select(0.5)
                if events:
                    break
            # print("DBG: call recv")
            try:
                s = self.sock.recv(4096)
                # print(f"DBG: recvd: {len(s)}: {s}")
            except ConnectionResetError:
                # logger.error(f"{function_name()}: connection reset: {ex=}")
                # raise
                return
            i0 = 0
            i = i0
            while i < len(s):
                if s[i] == b"\r"[0] or s[i] == b"\n"[0]:
                    self.buf += s[i0:i]
                    if self.buf:
                        self.lines.append(self.buf)
                        self.buf = b""
                    i += 1
                    i0 = i
                else:
                    i += 1
            self.buf += s[i0:i]
        return self.lines.pop(0)

    def write_line(self, s):
        if not self.sock:
            raise RuntimeError("socket not connected")
        b = s.encode()
        totsent = 0
        while totsent < len(b):
            sent = self.sock.send(b[totsent:])
            if sent == 0:
                raise RuntimeError("socket connection broken")
            totsent += sent

    def terminate(self):
        self._terminate = True


@dataclass
class SingleFrameResult:
    success: bool
    error_message: str | None
    path: str | None

    def __repr__(self):
        path = Path(self.path) if self.path else None
        return (
            f"SingleFrameResult(success={self.success}, "
            f"error_message='{self.error_message}', path={path.as_posix() if path else None})"
        )


class PHD2Connector(GuiderInterface, ImagerInterface):
    """The main class for interacting with PHD2 both as a guider and as an imager."""

    DEFAULT_STOP_CAPTURE_TIMEOUT = 10
    _instance = None
    _initialized = False

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(
        self,
        parent = None,
        hostname="localhost",
        instance=1,
        _from_imager: bool = False,
    ):
        if self._initialized:
            return  # singleton, do not re-initialize

        GuiderInterface.__init__(self)
        ImagerInterface.__init__(self)
        SwitchedOutlet.group(
            domain=OutletDomain.UnitOutlets,
            group_name="Camera",
            outlet_names=["Camera", "CameraUSB"],
        ).transfer_attributes(self)


        self.parent = parent
        self.hostname = hostname
        self.instance = instance
        self.conn = None
        self._terminate = False
        self.worker = None
        self.lock = threading.Lock()
        self.cond = threading.Condition()
        self.response = None
        self.app_state = ""
        self.avg_dist = 0
        self.version = ""
        self.sub_version = ""
        self.accumulators_active = False
        self.settle_px = 0
        self.ra_accumulator = PHD2Accumulator()
        self.dec_accumulator = PHD2Accumulator()
        self.stats = PHD2GuideStats()
        self.settle = None
        self._setpoint: float | None = None

        self.conf = Config().get_unit().phd2
        self.validation_interval = self.conf.validation_interval

        default_settling = self.conf.settle
        self.settling_settings: SettleModel = SettleModel(
            pixels=default_settling.pixels,
            time=default_settling.time,
            timeout=default_settling.timeout,
        )
        self.errors = []

        self.image_was_saved: bool = False
        self.image_saved_event: threading.Event = threading.Event()

        self.guiding_verification_timer: RepeatTimer | None = None
        if self.validation_interval != 0:
            logger.info(f"{function_name()}: guiding validation every {self.validation_interval} seconds")
            self.guiding_verification_timer = RepeatTimer(
                interval=self.validation_interval, function=self.validate_guiding
            )
        else:
            logger.info(f"{function_name()}: no guiding validation ({self.validation_interval=})")

        self.activities = PHD2Activities.Idle
        self.restart_event: threading.Event = threading.Event()

        self.sky_quality: SeeingQualityWhilePHD2Guiding = SeeingQualityWhilePHD2Guiding()

        self.watched_process = WatchedProcess(
            command="C:/Program Files/PHDGuiding2/phd2.exe",
            # command = "C:/Users/mast/Documents/GitHub/phd2/tmp64/Debug/phd2.exe",
            logger=logger,
            shell=True,
            restart_event=self.restart_event,
            no_restart=True,
        )
        self.watched_process.start()
        secs = 3
        logger.info(f"{function_name()}: sleeping {secs} seconds to allow PHD2 to start")
        time.sleep(secs)

        self._needs_to_resume_guiding = False
        self.need_to_reset_limit_frame = False

        self._connected = False
        try:
            self.connect()
            self.connect_equipment()
        except PHD2ConnectorError as ex:
            self.connected = False
            logger.error(f"{function_name()}: Failed to connect {ex=}")

        self.cooler_on = True
        # threading.Thread(name="phd2-reconnector", target=self.reconnect).start()

        self._initialized = True

    def reconnect(self):
        while not self._terminate:
            self.restart_event.wait()
            self.restart_event.clear()

            try:
                logger.info(f"{function_name()}: reconnect: got the restart event, connecting ...")
                self.connect()
                # self.connect_equipment()
            except PHD2ConnectorError as ex:
                self.connected = False
                logger.error(f"{function_name()}: Failed to connect {ex=}")
            except Exception as ex:
                logger.error(f"{function_name()}: reconnect: caught {ex=}")

            # self.cooler_on = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.disconnect()

    def __del__(self):
        if self.watched_process:
            self.watched_process.terminate()
            self.watched_process = None

    def equipment_is_connected(self) -> bool:
        response = self.call("get_connected")
        return response['result']

    def validate_guiding(self):
        if not self.is_active(PHD2Activities.Guiding):
            logger.warning(f"{function_name()}: not verifying guiding: not guiding")
            return

        assert self.parent is not None and self.parent.unit is not None
        assert self.parent.unit.acquirer.latest_acquisition is not None

        self.start_activity(PHD2Activities.Validating)
        # self.call("stop_capture")  # stop guiding
        self.stop_guiding()
        guiding_settings = self.parent.unit.guider.make_guiding_settings(
            base_folder=str(
                Path(self.parent.unit.acquirer.latest_acquisition.folder)
                / "guiding"
                / "validation"
            )
        )
        assert guiding_settings.roi is not None
        try:
            self.start_activity(PHD2Activities.ExposingForValidation)
            self.call(
                "capture_single_frame",
                params={
                    "exposure": int(
                        guiding_settings.seconds * 1000
                    ),  # convert to milliseconds
                    "gain": int(ASI.gain_absolute_to_percent(guiding_settings.gain)),
                    "binning": (
                        guiding_settings.binning.x if guiding_settings.binning else 1
                    ),
                    "subframe": [
                        guiding_settings.roi.x,
                        guiding_settings.roi.y,
                        guiding_settings.roi.width,
                        guiding_settings.roi.height,
                    ],
                    "save": True,
                    "path": guiding_settings.image_path,
                },
            )
        except PHD2ConnectorError as ex:
            self.log_and_append_error(f"{ex=}")
            self.end_activity(PHD2Activities.ExposingForValidation)
            self.end_activity(PHD2Activities.Validating)
            self.start_guiding()
            return

        self.wait_for_image_saved()

        logger.info(f"{function_name()}: resuming guiding ...")
        self.start_guiding()

        assert self.parent.unit is not None
        assert self.parent.unit.acquirer.latest_acquisition is not None
        assert guiding_settings.image_path is not None
        target: Coord = Coord(
            Angle(
                self.parent.unit.acquirer.latest_acquisition.target_ra,
                unit="hours",
            ),
            Angle(
                self.parent.unit.acquirer.latest_acquisition.target_dec,
                unit="degrees",
            ),
        )
        tolerance = Config().get_unit().guiding.tolerance

        self.start_activity(PHD2Activities.SolvingForValidation)
        with ThreadPoolExecutor() as executor:
            logger.info(f"{function_name()}: starting solving for {target=}")
            self.parent.start_activity(UnitActivities.Solving)
            future = executor.submit(
                self.parent.unit.solver.solve, guiding_settings, target
            )
            solving_result = future.result()
            self.parent.end_activity(UnitActivities.Solving)

        logger.info(f"{function_name()}: solving result: {solving_result=}")
        if solving_result and solving_result.succeeded and solving_result.solution:
            delta_ra = solving_result.solution.ra_hours - target.ra.hours  # type: ignore
            delta_dec = solving_result.solution.dec_degs - target.dec.degrees  # type: ignore
            within_tolerance = (
                abs(delta_ra) <= tolerance.ra_arcsec
                and abs(delta_dec) <= tolerance.dec_arcsec
            )
            boxed_info(
                logger=logger,
                lines=[
                    f"{delta_ra=}, {delta_dec=}",
                    f"{tolerance=}",
                    f"within tolerance: {within_tolerance}",
                ],
            )

            if not within_tolerance:
                # TBD: what to do if the target is not within tolerance?
                logger.error(f"{function_name()}: OUT OF TOLERANCE!, WHAT TO DO?")
                pass

        self.end_activity(PHD2Activities.SolvingForValidation)
        self.end_activity(PHD2Activities.Validating)

    @staticmethod
    def _is_guiding(st):
        return st == "Guiding" or st == "LostLock"

    @staticmethod
    def _get_accumulated_stats(ra, dec):
        stats = PHD2GuideStats()
        stats.rms_ra = ra.stdev()
        stats.rms_dec = dec.stdev()
        stats.peak_ra = ra._peak
        stats.peak_dec = dec._peak
        return stats

    def _handle_event(self, ev):  # noqa: C901
        e = ev["Event"]

        if e == "AppState":
            with self.lock:
                self.app_state = ev["State"]
                logger.debug(f"{function_name()}: {e}, app_state: {self.app_state}")
                if self._is_guiding(self.app_state):
                    self.avg_dist = 0  # until we get a GuideStep event
        elif e == "Alert":
            logger.debug(f"{function_name()}: {e}, Type: {ev["Type"]}, Msg: {ev["Msg"]}")
        elif e == "Version":
            with self.lock:
                self.version = ev["PHDVersion"]
                self.sub_version = ev["PHDSubver"]
                logger.debug(f"{function_name()}: {e}, {self.version=}, {self.sub_version=}")

        elif e == "StartGuiding":
            self.start_activity(PHD2Activities.Guiding)
            if self.guiding_verification_timer is not None:
                self.guiding_verification_timer.start()
            self.accumulators_active = True
            self.ra_accumulator.reset()
            self.dec_accumulator.reset()
            stats = self._get_accumulated_stats(
                self.ra_accumulator, self.dec_accumulator
            )
            with self.lock:
                self.stats = stats
                logger.debug("Started guiding")

        elif e == "GuideStep":
            # | Attribute | Type | Description |
            # |:----------|:-----|:------------|
            # |Frame      |number|The frame number; starts at 1 each time guiding starts|
            # |Time       |number| the time in seconds, including fractional seconds, since guiding started|
            # |Mount      |string|the name of the mount|
            # |dx         |number|the X-offset in pixels|
            # |dy         |number|the Y-offset in pixels|
            # |RADistanceRaw|number|the RA distance in pixels of the guide offset vector|
            # |DECDistanceRaw|number|the Dec distance in pixels of the guide offset vector|
            # |RADistanceGuide|number|the guide algorithm-modified RA distance in pixels of the guide offset vector|
            # |DECDistanceGuide|number|the guide algorithm-modified Dec distance in pixels of the guide offset vector|
            # |RADuration |number|the RA guide pulse duration in milliseconds|
            # |RADirection|string|"East" or "West"   |
            # |DECDuration|number|the Dec guide pulse duration in milliseconds|
            # |DECDirection|string|"South" or "North"   |
            # |StarMass   |number|the Star Mass value of the guide star|
            # |SNR        |number|the computed Signal-to-noise ratio of the guide star|
            # |HFD        |number|the guide star half-flux diameter (HFD) in pixels|
            # |AvgDist    |number|a smoothed average of the guide distance in pixels (equivalent to value returned by socket server MSG\_REQDIST)|
            # |RALimited  |boolean|true if step was limited by the Max RA setting (attribute omitted if step was not limited)|
            # |DecLimited |boolean|true if step was limited by the Max Dec setting (attribute omitted if step was not limited)|
            # |ErrorCode  |number|the star finder error code, 1=saturated, 2=low SNR, 3=low mass, 4=low HFD, 5=High HFD, 6=edge of frame, 7=mass change, 8=unexpected|
            stats = None
            if self.accumulators_active:
                self.ra_accumulator.add(ev["RADistanceRaw"])
                self.dec_accumulator.add(ev["DECDistanceRaw"])
                stats = self._get_accumulated_stats(
                    self.ra_accumulator, self.dec_accumulator
                )
            with self.lock:
                self.app_state = "Guiding"
                self.start_activity(PHD2Activities.Guiding, existing_ok=True)
                if self.is_active(PHD2Activities.EquipmentHandover):
                    self.end_activity(
                        PHD2Activities.EquipmentHandover
                    )  # To get the timing printout
                self.avg_dist = ev["AvgDist"]
                if self.accumulators_active:
                    self.stats = stats
            lines = []
            if "Time" in ev:
                lines.append(f'Seconds since guiding="{ev["Time"]:.1f}s"')
            if "StarMass" in ev:
                lines.append(f"StarMass={ev['StarMass']}")
            if "SNR" in ev:
                lines.append(f"SNR={ev['SNR']:.1f}")
            if "HFD" in ev:
                lines.append(f"HFD={ev['HFD']:.1f}")
            if "ErrorCode" in ev:
                lines.append(f"ErrorCode={ev['ErrorCode']}")
            if lines:
                boxed_info(logger=logger, lines=lines)

            self.sky_quality.update(FrameMetrics(snr=ev['SNR'], hfd_pixels=ev['HFD']))

        elif e == "SettleBegin":
            self.start_activity(PHD2Activities.Settling)
            self.accumulators_active = (
                False  # exclude GuideStep messages from stats while settling
            )
        elif e == "Settling":
            self.start_activity(PHD2Activities.Settling, existing_ok=True)
            s = PHD2SettleProgress()
            s.done = False
            s.distance = ev["Distance"]
            s.settle_px = self.settle_px
            s.time = ev["Time"]
            s.settle_time = ev["SettleTime"]
            s.status = 0
            with self.lock:
                self.settle = s
        elif e == "SettleDone":
            self.end_activity(PHD2Activities.Settling)
            self.accumulators_active = True
            self.ra_accumulator.reset()
            self.dec_accumulator.reset()
            stats = self._get_accumulated_stats(
                self.ra_accumulator, self.dec_accumulator
            )
            s = PHD2SettleProgress()
            s.done = True
            s.status = ev["Status"]
            s.error = ev.get("error")
            with self.lock:
                self.settle = s
                self.stats = stats
        elif e == "Paused":
            with self.lock:
                self.app_state = "Paused"
        elif e == "StartCalibration":
            self.start_activity(PHD2Activities.Calibrating)
            with self.lock:
                self.app_state = "Calibrating"
        elif e == "CalibrationComplete":
            if "Mount" in ev:
                logger.debug(f"{function_name()}: {e}, Mount='{ev["Mount"]}'")

        elif e == "LoopingExposures":
            # | Attribute | Type | Description |
            # |:----------|:-----|:------------|
            # | Frame     | number | the exposure frame number; starts at 1 each time looping starts |
            if self.is_active(PHD2Activities.Calibrating):
                self.end_activity(PHD2Activities.Calibrating)
            # self.start_activity(PHD2Activities.Looping)
            logger.info(f"{function_name()}: LoopingExposures frame# {ev['Frame']}")
            with self.lock:
                self.app_state = "Looping"

        elif e == "LoopingExposuresStopped" or e == "GuidingStopped":
            activity = (
                PHD2Activities.Looping
                if e == "LoopingExposuresStopped"
                else PHD2Activities.Guiding
            )
            self.end_activity(activity)
            if activity == PHD2Activities.Guiding and self.guiding_verification_timer is not None:
                    logger.info(f"{function_name()}: stopping guiding verification timer")
                    self.guiding_verification_timer.cancel()
            with self.lock:
                self.app_state = "Stopped"

        elif e == "StarSelected":
            # | Attribute | Type | Description |
            # |:----------|:-----|:------------|
            # | X         | number | lock position X-coordinate |
            # | Y         | number | lock position Y-coordinate |
            lines = [f'event: StarSelected at x="{ev["X"]}" y="{ev["Y"]}"']
            boxed_info(logger=logger, lines=lines)

        elif e == "LockPositionSet":
            # | Attribute | Type | Description |
            # |:----------|:-----|:------------|
            # | X         | number | lock position X-coordinate |
            # | Y         | number | lock position Y-coordinate |
            lines = [f'event: LockPositionSet at x="{ev["X"]}" y="{ev["Y"]}"']
            boxed_info(logger=logger, lines=lines)

        elif e == "StarLost":
            with self.lock:
                self.app_state = "LostLock"
                self.avg_dist = ev["AvgDist"]
            # | Attribute | Type | Description |
            # |:----------|:-----|:------------|
            # | Frame     | number | frame number |
            # | Time      | number | time since guiding started, seconds |
            # | StarMass  | number | star mass value |
            # | SNR       | number | star SNR value |
            # | AvgDist   | number |a smoothed average of the guide distance in pixels (equivalent to value returned by socket server MSG\_REQDIST)|
            # | ErrorCode | number | error code  |
            # | Status    | string | error message |
            lines = ["event: Star Lost!"]
            if "Time" in ev:
                lines.append(f'Seconds since guiding="{ev["Time"]:.1f}s"')
            if "StarMass" in ev:
                lines.append(f"StarMass={ev['StarMass']}")
            if "SNR" in ev:
                lines.append(f"SNR={ev['SNR']:.1f}")
            if "ErrorCode" in ev:
                lines.append(f"ErrorCode={ev['ErrorCode']}")
            if "Status" in ev:
                lines.append(f'Status="{ev["Status"]}"')
            boxed_info(logger=logger, lines=lines)

        elif e == "SingleFrameComplete":
            result = SingleFrameResult(
                success=ev["Success"],
                error_message=ev.get("Error"),
                path=ev.get("Path"),
            )
            with self.lock:
                self.single_frame = result
            logger.info(f"{function_name()}: SingleFrameComplete, {result=}")
            self.image_was_saved = True
            self.image_saved_event.set()
            if self.parent is not None:
                self.parent.end_activity(ImagerActivities.Saving)
                self.parent.end_activity(ImagerActivities.Exposing)

        elif e == "ConfigurationChange":
            # logger.debug(f"{function_name()}: event: {e}")
            pass
        else:
            logger.warning(f"{function_name()}: TODO: Unhandled event {e}")
            pass

    def _worker(self):
        if not self.conn:
            raise RuntimeError("no connection to PHD2 server")

        while not self._terminate:
            try:
                line = self.conn.read_line()
            except ConnectionResetError:
                # logger.info(f"trying to reconnect {ex=}...")
                # # time.sleep(3)
                # self.connect()
                # self.connect_equipment()
                # continue

                # logger.error(f"worker exiting on {ex}")
                # break
                self._terminate = True
                return

            # print(f"DBG: L: {line}")
            if not line:
                if not self._terminate:
                    # server disconnected
                    # print("DBG: server disconnected")
                    pass
                break
            try:
                j = json.loads(line)
            except json.JSONDecodeError:
                # ignore invalid json
                # print("DBG: ignoring invalid json response")
                continue
            if "jsonrpc" in j:
                # a response
                # print(f"DBG: R: {line}\n")
                with self.cond:
                    self.response = j
                    self.cond.notify()
            else:
                self._handle_event(j)

    def connect(self):
        """connect to PHD2 -- call Connect before calling any of the server API methods below"""
        self.disconnect()
        try:
            self.conn = PHD2Connection()
            self.conn.connect(self.hostname, 4400 + self.instance - 1)
            self._terminate = False
            self.worker = threading.Thread(name="phd2-worker", target=self._worker)
            self.worker.start()
            self._connected = True
            # print("DBG: connect done")
        except Exception as ex:
            logger.error(f"{function_name()}: connect: {ex=}")
            # self.disconnect()
            # raise

    def disconnect(self):
        """disconnect from PHD2"""
        if self.worker is not None:
            if self.worker.is_alive():
                # print("DBG: terminating worker")
                self._terminate = True
                if self.conn:
                    self.conn.terminate()
                print("DBG: joining worker")
                self.worker.join()
            self.worker = None
        if self.conn is not None:
            self.conn.disconnect()
            self.conn = None
        self._connected = False
        # print("DBG: disconnect done")

    @staticmethod
    def _make_jsonrpc(method, params):
        req = {"method": method, "id": 1}
        if params is not None:
            if isinstance(params, list | dict):
                req["params"] = params
            else:
                # single non-null parameter
                req["params"] = [params]
        return json.dumps(req, separators=(",", ":"))

    @staticmethod
    def _failed(res):
        return "error" in res

    def call(self, method, params=None):
        """this function can be used for raw JSONRPC method
        invocation. Generally you won't need to use this as it is much
        more convenient to use the higher-level methods below

        """
        if not self.conn:
            raise RuntimeError("no connection to PHD2 server")

        s = self._make_jsonrpc(method, params)
        # print(f"DBG: Call: {s}")
        # send request
        self.conn.write_line(s + "\r\n")
        # wait for response
        with self.cond:
            while not self.response:
                self.cond.wait()
            response = self.response
            self.response = None
        if self._failed(response):
            raise PHD2ConnectorError(
                f"{function_name()}: error from RPC: {method=}, {params=}, message={response["error"]["message"]}"
            )
        return response

    def check_connected(self):
        if not self.conn or not self.conn.is_connected():
            raise PHD2ConnectorError("PHD2 Server not connected")

    def set_limit_frame(self, roi: ImagerRoi | None = None):
        if not self.connected:
            logger.error(f"{function_name()}: not connected")

        if roi:
            logger.debug(f"{function_name()}: setting {roi=}")
            self.call("set_limit_frame", params={
                "roi": [
                    roi.x, roi.y,
                    roi.width, roi.height
                ]
            })
            self.need_to_reset_limit_frame = True
        else:
            logger.debug(f"{function_name()}: resetting ROI")
            self.call("set_limit_frame", params={"roi": None})
            self.need_to_reset_limit_frame = False

    def guide(self,
              imager_settings: ImagerSettings | None = None,
              settling_settings: SettleModel | None = None,
              new_interface: bool = False):
        """Start guiding with the given settling parameters. PHD2 takes care
        of looping exposures, guide star selection, and settling. Call
        CheckSettling() periodically to see when settling is complete.

        """
        # self.check_connected()
        if not self.connected:
            logger.error(f"{function_name()}: not connected")
            return CanonicalResponse(errors=["not connected"])
        if not settling_settings:
            settling_settings = self.settling_settings

        s = PHD2SettleProgress()
        s.done = False
        s.distance = 0
        s.settle_px = settling_settings.pixels
        s.time = 0
        s.settle_time = settling_settings.time
        s.status = 0
        with self.lock:
            if self.settle and not self.settle.done:
                raise PHD2ConnectorError("cannot guide while settling")
            self.settle = s
        try:
            if not imager_settings:
                if self.latest_settings:
                    imager_settings = self.latest_settings
                    logger.debug(f"{function_name()}: using self.latest_settings to set ROI")
                else:
                    logger.error(f"{function_name()}: no imager_settings paremeter and no self.latest_settings, cannot set ROI")
                    return

            if not self.cooler_on:
                self.cooler_on = True

            assert imager_settings and imager_settings.roi
            if new_interface:
                roi = imager_settings.roi.binned(imager_settings.binning)
                self.call(
                    method="guide",
                    params=[
                        {
                            "pixels": settling_settings.pixels,
                            "time": settling_settings.time,
                            "timeout": settling_settings.timeout,
                        },
                        {
                            "x": roi.x,
                            "y": roi.y,
                            "width": roi.width,
                            "height": roi.height,
                        },
                        False,  # don't force calibration
                    ],
                )
            else:
                self.set_limit_frame(roi=imager_settings.roi.binned(imager_settings.binning))
                self.call(
                    method="guide",
                    params=[
                        {
                            "pixels": settling_settings.pixels,
                            "time": settling_settings.time,
                            "timeout": settling_settings.timeout,
                        },
                        False,  # don't force calibration
                    ],
                )
            self.settle_px = settling_settings.pixels
        except Exception:
            with self.lock:
                self.settle = None
            raise

    def dither(self, dither_pixels, settle_pixels, settle_time, settle_timeout):
        """Dither guiding with the given dither amount and settling parameters. Call CheckSettling()
        periodically to see when settling is complete.
        """
        self.check_connected()
        s = PHD2SettleProgress()
        s.done = False
        s.distance = dither_pixels
        s.settle_px = settle_pixels
        s.time = 0
        s.settle_time = settle_time
        s.status = 0
        with self.lock:
            if self.settle and not self.settle.done:
                raise PHD2ConnectorError("cannot dither while settling")
            self.settle = s
        try:
            self.call(
                "dither",
                [
                    dither_pixels,
                    False,
                    {
                        "pixels": settle_pixels,
                        "time": settle_time,
                        "timeout": settle_timeout,
                    },
                ],
            )
            self.settle_px = settle_pixels
        except Exception:
            with self.lock:
                self.settle = None
            raise

    def is_settling(self):
        """Check if phd2 is currently in the process of settling after a Guide
        or Dither"""
        # self.check_connected()
        if not self.connected:
            return False
        with self.lock:
            if self.settle:
                return True
        # for app init, initialize the settle state to a consistent
        # value as if Guide had been called
        res = self.call("get_settling")
        val = res["result"]
        if val:
            s = PHD2SettleProgress()
            s.done = False
            s.distance = -1.0
            s.settle_px = 0.0
            s.time = 0.0
            s.settle_time = 0.0
            s.status = 0
            with self.lock:
                if self.settle is None:
                    self.settle = s
        return val

    def check_settling(self):
        """Get the progress of settling"""
        self.check_connected()
        ret = PHD2SettleProgress()
        with self.lock:
            if not self.settle:
                raise PHD2ConnectorError("not settling")
            if self.settle.done:
                # settle is done
                ret.done = True
                ret.status = self.settle.status
                ret.error = self.settle.error
                self.settle = None
            else:
                # settle in progress
                ret.done = False
                ret.distance = self.settle.distance
                ret.settle_px = self.settle_px
                ret.time = self.settle.time
                ret.settle_time = self.settle.settle_time
        return ret

    def get_stats(self):
        """Get the guider statistics since guiding started. Frames captured
        while settling is in progress are excluded from the stats.

        """
        self.check_connected()
        with self.lock:
            stats = copy.copy(self.stats)
        if stats:
            stats.rms_tot = math.hypot(stats.rms_ra, stats.rms_dec)
        return stats

    def stop_capture(self, timeout_seconds=DEFAULT_STOP_CAPTURE_TIMEOUT):
        """stop looping and guiding"""
        res = self.call("stop_capture")
        for _ in range(0, timeout_seconds):
            with self.lock:
                if self.app_state == "Stopped":
                    return
            time.sleep(1)
            self.check_connected()
        # hack! workaround bug where PHD2 sends a GuideStep after stop
        # request and fails to send GuidingStopped
        res = self.call("get_app_state")
        st = res["result"]
        with self.lock:
            self.app_state = st
        if st == "Stopped":
            return
        # end workaround
        raise PHD2ConnectorError(
            f"guider did not stop capture after {timeout_seconds} seconds!"
        )

    def loop(self, timeout_seconds=10):
        """start looping exposures"""
        self.check_connected()
        # already looping?
        with self.lock:
            if self.app_state == "Looping":
                return
        res = self.call("get_exposure")
        exp_ms = res["result"]
        self.call("loop")
        time.sleep((exp_ms * 1.5) / 1000)
        for _ in range(0, timeout_seconds):
            with self.lock:
                if self.app_state == "Looping":
                    return
            time.sleep(1)
            self.check_connected()
        # raise PHD2ConnectorError("timed-out waiting for guiding to start looping")
        logger.warning(f"{function_name()}: timed-out after {timeout_seconds}s while waiting for guider to start looping")

    def pixel_scale(self):
        """get the guider pixel scale in arc-seconds per pixel"""
        res = self.call("get_pixel_scale")
        return res["result"]

    def get_equipment_profiles(self):
        """get a list of the Equipment Profile names"""
        res = self.call("get_profiles")
        profiles = []
        for p in res["result"]:
            profiles.append(p["name"])
        return profiles

    def connect_equipment(self):
        """connect the equipment in an equipment profile"""
        res = self.call("get_profile")
        current_profile = res["result"]
        if not current_profile or current_profile["name"] != self.conf.profile:
            res = self.call("get_profiles")
            profiles = res["result"]
            profile_id = -1
            for p in profiles:
                name = p["name"]
                if name == self.conf.profile:
                    profile_id = p.get("id", -1)
                    break
            if profile_id == -1:
                raise PHD2ConnectorError(
                    f"invalid phd2 profile name: {self.conf.profile}"
                )
            self.stop_capture()
            self.call("set_connected", False)
            self.call("set_profile", profile_id)
        self.call("set_connected", True)

    def disconnect_equipment(self):
        """disconnect equipment"""
        self.stop_capture()
        self.call("set_connected", False)

    def get_status(self):
        """get the AppState
        (https://github.com/OpenPHDGuiding/phd2/wiki/EventMonitoring#appstate)
        and current guide error

        """
        self.check_connected()
        # with self.lock:
        #     return self.app_state, self.avg_dist
        return self.app_state, self.avg_dist

    def __repr__(self):
        return f"PHD2Connector(profile='{self.conf.profile}')"

    def status(
        self, capacity: Literal["imager", "guider"] = "imager"
    ) -> PHD2ImagerStatus | PHD2GuiderStatus:

        if capacity == "imager":
            ret = PHD2ImagerStatus(
                identifier=self.identifier,
                activities=int(self.activities),
                activities_verbal=self.activities.__repr__(),
                connected=self.connected,
                operational=self.operational,
                why_not_operational=self.why_not_operational,
            )
        else:
            st = self.sky_quality.state
            sky_quality: SkyQualityStatus | None = None if self.sky_quality.latest_update is None \
                else SkyQualityStatus(
                    score=st.score_0_to_100,
                    state=st.quality_state,
                    latest_update=self.sky_quality.latest_update
                )

            ret = PHD2GuiderStatus(
                identifier=self.identifier,
                is_guiding=self.is_guiding,
                is_settling=self.is_settling(),
                app_state=self.app_state,
                avg_dist=self.avg_dist,
                sky_quality=sky_quality,
            )
        return ret

    @property
    def identifier(self):
        return f"profile='{self.conf.profile}'"

    @property
    def is_guiding(self) -> bool:
        """check if currently guiding"""
        if not self.connected:
            return False

        st, _ = self.get_status()
        return self._is_guiding(st)

    def pause(self):
        """pause guiding (looping exposures continues)"""
        self.call("set_paused", True)

    def unpause(self):
        """un-pause guiding"""
        self.call("set_paused", False)

    def save_image(self):
        """
        Save the current guide camera frame (FITS format), returning the name of the
        file.  The caller will need to remove the file when done.
        """
        res = self.call("save_image")
        return res["result"]["filename"]

    def shutdown(self):
        self.stop_guiding()
        self.disconnect()
        if self.watched_process:
            self.watched_process.terminate()

    def start_guiding(self) -> CanonicalResponse:

        if not self.connected:
            self.start_activity(PHD2Activities.EquipmentHandover)
            try:
                self.connect()
                self.connect_equipment()
            except PHD2ConnectorError as ex:
                return CanonicalResponse(errors=[f"cannot connect {ex=}"])

        assert self.parent and self.parent.unit, "phd2.start_guiding(): self.parent or self.unit is None. cannot make_guiding_settings"
        guiding_settings = self.parent.unit.guider.make_guiding_settings(save=False)

        logger.info(f"{function_name()}: starting guiding")
        self.parent.unit.start_activity(UnitActivities.Guiding)
        self.guide(imager_settings=guiding_settings)
        return CanonicalResponse_Ok

    def stop_guiding(self) -> CanonicalResponse:
        if not self.connected:
            return CanonicalResponse(errors=["not connected"])

        logger.info(f"{function_name()}: stopping guiding")
        self.call("stop_capture")
        if self.need_to_reset_limit_frame:
            self.set_limit_frame(roi=None)

        # self.start_activity(PHD2Activities.EquipmentTakeover)
        # self.disconnect_equipment()  # stops the exposure as well
        # if self.parent is not None:
        #     self.parent.connect()
        # self.end_activity(PHD2Activities.EquipmentTakeover)

        return CanonicalResponse_Ok

    @property
    def can_image_to_memory(self) -> bool:
        return False  # PHD2 does not support imaging to memory directly

    #
    # phd2 is not a reliable source for getting the imager's chip dimensions.
    # It only knows them after an exposure was made and then if the exposure was
    #  binned, it gives the binned dimensions
    #
    @property
    def camera_x_size(self) -> int:
        return ASI.ASI_294MM_WIDTH
        # self.check_connected()
        # response = self.call("get_camera_frame_size")
        # if response and response["result"]:
        #     arr = response["result"]
        #     return arr[0]
        # logger.error(f"{function_name()}: got None from 'get_camera_frame_size'")
        # return 0

    @property
    def camera_y_size(self) -> int:
        return ASI.ASI_294MM_HEIGHT
        # self.check_connected()
        # response = self.call("get_camera_frame_size")
        # if response and response["result"]:
        #     arr = response["result"]
        #     return arr[1]
        # logger.error(f"{function_name()}: got None from 'get_camera_frame_size'")
        # return 0

    def startup(self):
        pass

    def abort(self):
        self.stop_capture()

    @property
    def connected(self) -> bool:
        return self._connected

    @connected.setter
    def connected(self, value: bool):
        if value:
            self.connect()
            self.connect_equipment()
        else:
            self.disconnect_equipment()
            self.disconnect()

    def log_and_append_error(self, err: str):
        self.errors.append(err)
        logger.error(err)

    def start_exposure_series(self, series: ImagerExposureSeries):
        if self._is_guiding(self.app_state):
            self.stop_guiding()
            self._needs_to_resume_guiding = True
        else:
            self._needs_to_resume_guiding = False

    def end_exposure_series(self, series: ImagerExposureSeries):
        if self._needs_to_resume_guiding:
            self.start_guiding()
            self._needs_to_resume_guiding = False

    def new_start_exposure(self, settings: ImagerSettings) -> CanonicalResponse:
        op = function_name()

        self.errors = []
        if not self.connected:
            err = f"{op}: not connected"
            self.log_and_append_error(err)
            return CanonicalResponse(errors=[err])

        logger.info(f"{function_name()}: starting {settings.seconds}s exposure")
        self.image_was_saved = False
        if self.parent is not None:
            self.parent.start_activity(
                ImagerActivities.Exposing, details=f"{settings.seconds} seconds"
            )
            self.parent.start_activity(
                ImagerActivities.Saving,
                details=f"{Path(settings.image_path).as_posix() if settings.image_path else None}",
            )

        try:
            assert settings.roi
            roi = settings.roi.binned(settings.binning)
            self.call(
                "capture_single_frame",
                params={
                    "exposure": int(
                        settings.seconds * 1000  # convert to milliseconds
                    ),
                    "gain": int(ASI.gain_absolute_to_percent(settings.gain)),
                    "binning": settings.binning,
                    "save": True,
                    "path": settings.image_path,
                    "limit_frame": [roi.x, roi.y, roi.width, roi.height]
                },
            )

        except PHD2ConnectorError as ex:
            self.log_and_append_error(f"{ex=}")

        return (
            CanonicalResponse(errors=self.errors)
            if self.errors
            else CanonicalResponse_Ok
        )

    def start_exposure(self, settings: ImagerSettings) -> CanonicalResponse:
        """
        The main entry point for starting an exposure with PHD2.
        If PHD2 is currently guiding we stop it, start an exposure and then resume guiding.
        """
        op = function_name()

        self.errors = []
        if not self.connected:
            err = f"{op}: not connected"
            self.log_and_append_error(err)
            return CanonicalResponse(errors=[err])

        if not settings.image_path:
            raise PHD2ConnectorError("PHD2 save_image settings MUST have an image_path")

        logger.info(f"{function_name()}: starting {settings.seconds} exposure")
        self.image_was_saved = False
        if self.parent is not None:
            self.parent.start_activity(
                ImagerActivities.Exposing, details=f"{settings.seconds} seconds"
            )
            self.parent.start_activity(
                ImagerActivities.Saving,
                details=f"{Path(settings.image_path).as_posix()}",
            )

        if self._is_guiding(self.app_state):
            # while guiding we use save_image()
            try:
                self.call("save_image", {"path": settings.image_path})
            except PHD2ConnectorError as ex:
                self.log_and_append_error(f"{ex=}")
        else:
            # while not guiding we use capture_single_frame()

            try:
                assert settings.roi
                self.set_limit_frame(roi=settings.roi.binned(settings.binning))
                self.call(
                    "capture_single_frame",
                    params={
                        "exposure": int(
                            settings.seconds * 1000  # convert to milliseconds
                        ),
                        "gain": int(ASI.gain_absolute_to_percent(settings.gain)),
                        "binning": settings.binning,
                        "save": True,
                        "path": settings.image_path,
                    },
                )

            except PHD2ConnectorError as ex:
                self.log_and_append_error(f"{ex=}")
        return (
            CanonicalResponse(errors=self.errors)
            if self.errors
            else CanonicalResponse_Ok
        )

    def stop_exposure(self) -> CanonicalResponse:
        logger.info(f"{function_name()}: stopping exposure")
        self.call("stop_capture")
        if self.parent is not None:
            self.parent.end_activity(ImagerActivities.Exposing)
        return CanonicalResponse_Ok

    def abort_exposure(self) -> CanonicalResponse:
        logger.info(f"{function_name()}: aborting exposure")
        self.call("stop_capture")
        if self.parent is not None:
            self.parent.end_activity(ImagerActivities.Exposing)
        return CanonicalResponse_Ok

    def wait_for_image_ready(self):
        return

    def wait_for_image_saved(self):
        if not self.image_was_saved:
            self.image_saved_event.wait()
            # logger.info(f"{op}: got image_saved_event")
            self.image_saved_event.clear()

    @property
    def temperature(self) -> float | None:
        try:
            reply = self.call("get_ccd_temperature")
            if reply and "result" in reply and "temperature" in reply["result"]:
                return reply["result"]["temperature"]
        except Exception as ex:
            logger.error(f"{function_name()}: could not get temperature {ex=}")
            return None

    @property
    def set_point(self):
        return self._setpoint

    @property
    def cooler_on(self) -> bool | None:
        try:
            reply = self.call("get_cooler_status")
            if reply and "result" in reply and "coolerOn" in reply["result"]:
                self._setpoint = reply["result"]["setpoint"]
                return reply["result"]["coolerOn"]
        except Exception as ex:
            logger.error(f"{function_name()}: could not get coolerOn {ex=}")
            return None

    @cooler_on.setter
    def cooler_on(self, onoff: bool):
        self.call("set_cooler_state", onoff)

    @property
    def cooler_power(self) -> float | None:
        try:
            reply = self.call("get_cooler_status")
            if "result" in reply and "power" in reply["result"]:
                return reply["result"]["power"]
        except Exception as ex:
            logger.error(f"{function_name()}: could not get power {ex=}")
            return None

    @property
    def name(self) -> str:
        return "phd2"

    @property
    def image_array(self):
        return None

    @property
    def operational(self) -> bool:
        return self.connected  # Assuming PHD2 is always operational when connected

    @property
    def why_not_operational(self) -> list[str]:
        ret = []
        if not self.connected:
            ret.append(f"{self.name}: not connected")
        return ret

    @property
    def was_shut_down(self) -> bool:
        return False  # Assuming PHD2 is not shut down in this implementation

    @property
    def detected(self) -> bool:
        return self.connected

    @property
    def default_settings(self) -> ImagerSettings:
        self.check_connected()

        imager_conf = Config().get_unit().imager
        return ImagerSettings(
            seconds=5,
            base_folder="c:/temp/phd2_images",
            binning=1,
            gain=int(ASI.gain_absolute_to_percent(imager_conf.gain)),
            roi=ImagerRoi(
                x=0, y=0, width=self.camera_x_size, height=self.camera_y_size
            ),
            format=imager_conf.format,
        )

    @property
    def can_send_image_ready_event(self) -> bool:
        return False

    @property
    def can_send_image_saved_event(self) -> bool:
        return True


if __name__ == "__main__":

    def test_exposures(nexposures: int = 1,
                       gain: int = 180,
                       binning: int = 1,
                       x: int = 0,
                       y: int = 0,
                       width: int | None = None,
                       height: int | None = None):
        from imagers import Imager

        imager = Imager(imager_type="phd2")
        imager.connect()

        imager_settings = ImagerSettings.model_validate(
            {
                "seconds": 5,
                "gain": gain,
                "binning": binning,
                "roi": ImagerRoi.model_validate(
                    {
                    "x": x,
                    "y": y,
                    "width": width or imager.camera_x_size,
                    "height": height or imager.camera_y_size,
                    },
                )
                },
            context={"imager": imager},
        )
        i = 0
        for i in range(nexposures):
            print(f"=== image #{i} ===")
            imager.start_exposure(imager_settings)

            if imager.can_send_image_ready_event:
                imager.wait_for_image_ready()

            if imager.can_send_image_saved_event:
                imager.wait_for_image_saved()
                assert imager_settings.image_path
                # Path(imager_settings.image_path).unlink()

            imager_settings.make_file_name()
            i += 1

    def test_guiding():
        guider = PHD2Connector()
        guider.start_guiding()
        while True:
            status = guider.get_status()
            if status:
                print(f"{status=}")
                # if status[0] == "Stopped":
                #     guider.abort()
                #     break
            time.sleep(1)

    def test_new_guiding():
        PHD2Connector().guide(imager_settings=ImagerSettings(
            seconds=3.4,
            save=False,
            binning=2,
            gain=200,
            roi = ImagerRoi(x=200, y=150, width=2000, height=1000)
        ), new_interface=True)

    def test_new_single_frame():
        PHD2Connector().new_start_exposure(settings=ImagerSettings(
            seconds=3.4,
            save=False,
            binning=2,
            gain=200,
            image_path="c:/dummy.fits",
            roi = ImagerRoi(x=200, y=150, width=2000, height=1000)))

    test_exposures(
        nexposures=1,
        binning=2,
        x=1000, y=2000, width=4000, height=3000
    )
    # test_guiding()

    # test_new_guiding()

    # test_new_single_frame()

    exit(0)
