import copy
import json
import logging
import math
import os
import selectors
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import IntFlag, auto
from typing import TYPE_CHECKING, Literal

from astropy.coordinates import Angle
from pydantic import BaseModel

from common.activities import ImagerActivities
from common.canonical import CanonicalResponse, CanonicalResponse_Ok
from common.config import Config
from common.dlipowerswitch import OutletDomain, SwitchedOutlet
from common.interfaces.guiding import GuiderInterface
from common.interfaces.imager import ImagerBinning, ImagerExposureSeries, ImagerInterface, ImagerRoi, ImagerSettings
from common.mast_logging import init_log
from common.process import WatchedProcess
from common.utils import Coord, RepeatTimer, boxed_info, function_name

if TYPE_CHECKING:
    from unit import Unit  # type: ignore[name-defined]

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


class PHD2ImagerStatus(BaseModel):
    name: str = "phd2"
    activities: int = 0
    activities_verbal: str | None = None
    operational: bool = False
    why_not_operational: list[str] = []
    connected: bool = False
    temperature: float | None = None
    cooler_on: bool = False
    cooler_power: float | None = None


class PHD2GuiderStatus(BaseModel):
    is_guiding: bool = False
    is_settling: bool = False
    app_state: str | None = None
    avg_dist: float | None = None


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
            except ConnectionResetError as ex:
                logger.error(f"connection reset: {ex=}")
                raise
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
        unit=None,
        hostname="localhost",
        instance=1,
        _from_imager: bool = False,
    ):
        if self._initialized:
            return  # singleton, do not re-initialize

        GuiderInterface.__init__(self)
        ImagerInterface.__init__(self)
        if not _from_imager:
            SwitchedOutlet.group(
                domain=OutletDomain.Unit,
                group_name="Camera",
                outlet_names=["Camera", "CameraUSB"]).populate(self)

        self.unit: "Unit" | None = unit  # type: ignore
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
        self.validation_interval = Config().get_unit().phd2.validation_interval

        default_settling = Config().get_unit().phd2.settle
        self.settling_settings: SettleModel = SettleModel(
            pixels=default_settling.pixels,
            time=default_settling.time,
            timeout=default_settling.timeout,
        )
        self.errors = []

        self.image_was_saved: bool = False
        self.image_saved_event: threading.Event = threading.Event()

        self.guiding_verification_timer: RepeatTimer = RepeatTimer(
            interval=self.validation_interval, function=self.validate_guiding
        )

        self.conf = Config().get_unit().phd2

        self.activities = PHD2Activities.Idle

        self.watched_process = WatchedProcess(
            command='"C:/Program Files (x86)/PHDGuiding2/phd2.exe"',
            logger=logger,
            shell=True,
        )
        self.watched_process.start()
        secs = 3
        logger.info(f"sleeping {secs} seconds to allow PHD2 to start")
        time.sleep(secs)

        self._needs_to_resume_guiding = False

        self._connected = False
        try:
            self.connect()
            self.connect_equipment()
        except PHD2ConnectorError as ex:
            self.connected = False
            logger.error(f"{function_name()}: Failed to connect {ex=}")

        self.cooler_on = True

        self._initialized = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.disconnect()

    def __del__(self):
        if self.watched_process:
            self.watched_process.terminate()
            self.watched_process = None

    def validate_guiding(self):
        if not self.is_active(PHD2Activities.Guiding):
            logger.warning(f"{function_name()}: not verifying guiding: not guiding")
            return

        self.start_activity(PHD2Activities.Validating)
        self.call("stop_capture")  # stop guiding
        assert self.unit is not None
        assert self.unit.acquirer.latest_acquisition is not None
        settings = self.unit.guider.make_guiding_settings(
            base_folder=os.path.join(
                self.unit.acquirer.latest_acquisition.folder, "guiding", "validation"
            )
        )
        assert settings.roi is not None
        try:
            self.start_activity(PHD2Activities.ExposingForValidation)
            self.call(
                "capture_single_frame",
                params={
                    "exposure": int(settings.seconds * 1000),  # convert to milliseconds
                    "gain": settings.gain,
                    "binning": settings.binning.x if settings.binning else 1,
                    "subframe": [
                        settings.roi.x,
                        settings.roi.y,
                        settings.roi.width,
                        settings.roi.height,
                    ],
                    "save": True,
                    "path": settings.image_path,
                },
            )
        except PHD2ConnectorError as ex:
            self.log_and_append_error(f"{ex=}")
            self.end_activity(PHD2Activities.ExposingForValidation)
            self.end_activity(PHD2Activities.Validating)
            self.guide()
            return

        self.wait_for_image_saved()

        logger.info(f"{function_name()}: resuming guiding ...")
        self.guide()

        assert self.unit.acquirer.latest_acquisition is not None
        assert settings.image_path is not None
        target: Coord = Coord(
            Angle(self.unit.acquirer.latest_acquisition.target_ra, unit="hours"),
            Angle(self.unit.acquirer.latest_acquisition.target_dec, unit="degrees"),
        )
        tolerance = Config().get_unit().guiding.tolerance

        self.start_activity(PHD2Activities.SolvingForValidation)
        with ThreadPoolExecutor() as executor:
            logger.info(f"{function_name()}: starting solving for {target=}")
            future = executor.submit(self.unit.solver.solve, settings, target)
            solving_result = future.result()

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
                logger.error("OUT OF TOLERANCE!, WHAT TO DO?")
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
                logger.debug(f"event: {e}, app_state: {self.app_state}")
                if self._is_guiding(self.app_state):
                    self.avg_dist = 0  # until we get a GuideStep event
        elif e == "Alert":
            logger.debug(f"event: {e}, Type: {ev["Type"]}, Msg: {ev["Msg"]}")
        elif e == "Version":
            with self.lock:
                self.version = ev["PHDVersion"]
                self.sub_version = ev["PHDSubver"]
                logger.debug(f"event: {e}, {self.version=}, {self.sub_version=}")
        elif e == "StartGuiding":
            self.start_activity(PHD2Activities.Guiding)
            self.guiding_verification_timer.start()
            self.accumulators_active = True
            self.ra_accumulator.reset()
            self.dec_accumulator.reset()
            stats = self._get_accumulated_stats(
                self.ra_accumulator, self.dec_accumulator
            )
            with self.lock:
                self.stats = stats
        elif e == "GuideStep":
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
                self.avg_dist = ev["AvgDist"]
                if self.accumulators_active:
                    self.stats = stats
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
                logger.debug(f"event: {e}, Mount='{ev["Mount"]}'")
        elif e == "LoopingExposures":
            if self.is_active(PHD2Activities.Calibrating):
                self.end_activity(PHD2Activities.Calibrating)
            # self.start_activity(PHD2Activities.Looping)
            logger.info("event: LoopingExposures")
            with self.lock:
                self.app_state = "Looping"
        elif e == "LoopingExposuresStopped" or e == "GuidingStopped":
            activity = (
                PHD2Activities.Looping
                if e == "LoopingExposuresStopped"
                else PHD2Activities.Guiding
            )
            self.end_activity(activity)
            if activity == PHD2Activities.Guiding:
                self.guiding_verification_timer.cancel()
            with self.lock:
                self.app_state = "Stopped"

        elif e == "StarLost":
            with self.lock:
                self.app_state = "LostLock"
                self.avg_dist = ev["AvgDist"]

        elif e == "SingleFrameComplete":
            result = SingleFrameResult(
                success=ev["Success"],
                error_message=ev.get("Error"),
                path=ev.get("Path"),
            )
            with self.lock:
                self.single_frame = result
            logger.info(f"event: SingleFrameComplete, {result=}")
            self.image_was_saved = True
            self.image_saved_event.set()
            self.end_activity(ImagerActivities.Exposing)
            self.end_activity(ImagerActivities.Saving)
        elif e == "ConfigurationChange":
            # logger.debug(f"event: {e}")
            pass
        else:
            logger.error(f"TODO: Unhandled event {e}")
            pass

    def _worker(self):
        if not self.conn:
            raise RuntimeError("no connection to PHD2 server")

        while not self._terminate:
            try:
                line = self.conn.read_line()
            except ConnectionResetError as ex:
                logger.info(f"trying to reconnect {ex=}...")
                time.sleep(3)
                self.connect()
                self.connect_equipment()
                continue

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
            self.worker = threading.Thread(target=self._worker)
            self.worker.start()
            self._connected = True
            # print("DBG: connect done")
        except Exception:
            self.disconnect()
            raise

    def disconnect(self):
        """disconnect from PHD2"""
        if self.worker is not None:
            if self.worker.is_alive():
                # print("DBG: terminating worker")
                self._terminate = True
                if self.conn:
                    self.conn.terminate()
                # print("DBG: joining worker")
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
                f"error from RPC: {method=}, {params=}, message={response["error"]["message"]}"
            )
        return response

    def check_connected(self):
        if not self.conn or not self.conn.is_connected():
            raise PHD2ConnectorError("PHD2 Server not connected")

    def guide(self, settings: SettleModel | None = None):
        """Start guiding with the given settling parameters. PHD2 takes care
        of looping exposures, guide star selection, and settling. Call
        CheckSettling() periodically to see when settling is complete.

        """
        # self.check_connected()
        if not self.connected:
            logger.error(f"{function_name()}: not connected")
            return CanonicalResponse(errors=["not connected"])
        if not settings:
            settings = self.settling_settings

        s = PHD2SettleProgress()
        s.done = False
        s.distance = 0
        s.settle_px = settings.pixels
        s.time = 0
        s.settle_time = settings.time
        s.status = 0
        with self.lock:
            if self.settle and not self.settle.done:
                raise PHD2ConnectorError("cannot guide while settling")
            self.settle = s
        try:
            self.call(
                "guide",
                [
                    {
                        "pixels": settings.pixels,
                        "time": settings.time,
                        "timeout": settings.timeout,
                    },
                    False,  # don't force calibration
                ],
            )
            self.settle_px = settings.pixels
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
        time.sleep(exp_ms / 1000)
        for _ in range(0, timeout_seconds):
            with self.lock:
                if self.app_state == "Looping":
                    return
            time.sleep(1)
            self.check_connected()
        raise PHD2ConnectorError("timed-out waiting for guiding to start looping")

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
        if current_profile["name"] != self.conf.profile:
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
        with self.lock:
            return self.app_state, self.avg_dist

    def status(
        self, capacity: Literal["imager", "guider"] = "imager"
    ) -> PHD2ImagerStatus | PHD2GuiderStatus:

        if capacity == "imager":
            return PHD2ImagerStatus(
                activities=int(self.activities),
                activities_verbal=self.activities.__repr__(),
                connected=self.connected,
                operational=self.operational,
                why_not_operational=self.why_not_operational,
                temperature=self.temperature,
                cooler_on=self.cooler_on,
                cooler_power=self.cooler_power if self.cooler_power else None,
            )
        else:
            return PHD2GuiderStatus(
                is_guiding=self.is_guiding,
                is_settling=self.is_settling(),
                app_state=self.app_state,
                avg_dist=self.avg_dist,
            )

    @property
    def is_guiding(self) -> bool:
        """check if currently guiding"""
        if not self.connected:
            return False

        st, dist = self.get_status()
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
            try:
                self.connect()
                self.connect_equipment()
            except PHD2ConnectorError as ex:
                return CanonicalResponse(errors=[f"cannot connect {ex=}"])

        logger.info("starting guiding")
        self.guide()
        return CanonicalResponse_Ok

    def stop_guiding(self) -> CanonicalResponse:
        if not self.connected:
            return CanonicalResponse(errors=["not connected"])

        logger.info("stopping guiding")
        self.stop_capture()
        return CanonicalResponse_Ok

    @property
    def can_image_to_memory(self) -> bool:
        return False  # PHD2 does not support imaging to memory directly

    @property
    def camera_x_size(self) -> int:
        self.check_connected()
        response = self.call("get_camera_frame_size")
        if response and response["result"]:
            arr = response["result"]
            return arr[0]
        logger.error(f"{function_name()}: got None from 'get_camera_frame_size'")
        return 0

    @property
    def camera_y_size(self) -> int:
        self.check_connected()
        response = self.call("get_camera_frame_size")
        if response and response["result"]:
            arr = response["result"]
            return arr[1]
        logger.error(f"{function_name()}: got None from 'get_camera_frame_size'")
        return 0

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
        else:
            self.disconnect()

    def log_and_append_error(self, err: str):
        self.errors.append(err)
        logger.error(err)

    def start_exposure_series(self, purpose: str | None = None):
        series_id = super().start_exposure_series(purpose=purpose)
        if self._is_guiding(self.app_state):
            self.stop_guiding()
            self._needs_to_resume_guiding = True
        else:
            self._needs_to_resume_guiding = False
        return series_id

    def end_exposure_series(self, series: ImagerExposureSeries):
        try:
            super().end_exposure_series(series)
        except ValueError as ex:
            self.log_and_append_error(f"end_exposure_series: {series=}: {ex=}")
            return

        if self._needs_to_resume_guiding:
            self.start_guiding()
            self._needs_to_resume_guiding = False

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

        logger.info(f"starting {settings.seconds} exposure")
        self.start_activity(ImagerActivities.Exposing)
        self.start_activity(ImagerActivities.Saving)

        if self._is_guiding(self.app_state):
            # while guiding we use save_image()
            try:
                self.call("save_image", {"path": settings.image_path})
            except PHD2ConnectorError as ex:
                self.log_and_append_error(f"{ex=}")
        else:
            # while not guiding we use capture_single_frame()
            assert settings.roi is not None, "start_exposure: settings.roi is None"

            try:
                self.call(
                    "capture_single_frame",
                    params={
                        "exposure": int(
                            settings.seconds * 1000
                        ),  # convert to milliseconds
                        "gain": settings.gain,
                        "binning": settings.binning.x if settings.binning else 1,
                        "subframe": [
                            settings.roi.x,
                            settings.roi.y,
                            settings.roi.width,
                            settings.roi.height,
                        ],
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
        logger.info("stopping exposure")
        self.call("stop_capture")
        self.end_activity(ImagerActivities.Exposing)
        return CanonicalResponse_Ok

    def abort_exposure(self) -> CanonicalResponse:
        logger.info("aborting exposure")
        self.call("stop_capture")
        self.end_activity(ImagerActivities.Exposing)
        return CanonicalResponse_Ok

    def wait_for_image_ready(self):
        return

    def wait_for_image_saved(self):
        op = function_name()
        if not self.image_was_saved:
            # logger.info(f"{op}: image was not saved, waiting for image_saved_event ...")
            self.image_saved_event.wait()
            logger.info(f"{op}: got image_saved_event")
            self.image_saved_event.clear()

    @property
    def temperature(self) -> float:
        reply = self.call("get_ccd_temperature")
        return reply["result"]["temperature"]

    @property
    def cooler_on(self) -> bool:
        reply = self.call("get_cooler_status")
        return reply["result"]["coolerOn"]

    @cooler_on.setter
    def cooler_on(self, onoff: bool):
        reply = self.call("set_cooler_state", onoff)

    @property
    def cooler_power(self) -> float | None:
        reply = self.call("get_cooler_status")
        if "result" in reply and "power" in reply["result"]:
            return reply["result"]["power"]
        return None

    @property
    def name(self) -> str:
        return "PHD2Imager"

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
        return self.detected

    @property
    def default_settings(self) -> ImagerSettings:
        self.check_connected()
        return ImagerSettings(
            seconds=5,
            base_folder="c:/temp/phd2_images",
            binning=ImagerBinning(x=1, y=1),
            gain=85,
            roi=ImagerRoi(
                x=0, y=0, width=self.camera_x_size, height=self.camera_y_size
            ),
        )

    @property
    def can_send_image_ready_event(self) -> bool:
        return False

    @property
    def can_send_image_saved_event(self) -> bool:
        return True


if __name__ == "__main__":
    cam = PHD2Connector()
    cam.startup()
    cam.start_exposure(
        ImagerSettings.model_validate({"seconds": 5}, context={"imager": cam})
    )
    print(json.dumps(cam.status(capacity="imager").model_dump(), indent=2))
    if cam.can_send_image_ready_event:
        cam.wait_for_image_ready()
        logger.info("got image ready event")

    if cam.can_send_image_saved_event:
        cam.wait_for_image_saved()
        logger.info("got image saved event")
    print(json.dumps(cam.status(capacity="imager").model_dump(), indent=2))
    exit(0)
