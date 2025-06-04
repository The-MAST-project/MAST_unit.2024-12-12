import copy
import json
import logging
import math
import selectors
import socket
import sys
import threading
import time
from enum import IntFlag, auto

from pydantic import BaseModel

from common.config import Config
from common.mast_logging import init_log
from common.process import WatchedProcess
from guiders.base_guider import GuiderInterface

logger = logging.Logger("mast.unit.phd2_guider")
init_log(logger)


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


class PHD2Status(BaseModel):
    name: str
    activities: int
    activities_verbal: str
    is_guiding: bool
    is_settling: bool
    app_state: str
    avg_dist: float


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


class PHD2GuiderError(Exception):
    """GuiderException is the base class for any excettions raied by the
    Guider methods

    """

    pass


class PHD2Accumulator:
    def __init__(self):
        self.n = 0
        self.a = self.q = self.peak = 0
        self.reset()

    def reset(self):
        self.n = 0
        self.a = self.q = self.peak = 0

    def add(self, x):
        ax = abs(x)
        if ax > self.peak:
            self.peak = ax
        self.n += 1
        d = x - self.a
        self.a += d / self.n
        self.q += (x - self.a) * d

    def mean(self):
        return self.a

    def stdev(self):
        return math.sqrt(self.q / self.n) if self.n >= 1 else 0.0

    def peak(self):
        return self.peak


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
        if self.sel is not None:
            self.sel.unregister(self.sock)
            self.sel = None
        if self.sock is not None:
            self.sock.close()
            self.sock = None

    def is_connected(self):
        return self.sock is not None

    def read_line(self):
        # print(f"DBG: ReadLine enter lines:{len(self.lines)}")
        while not self.lines:
            # print("DBG: begin wait")
            while True:
                if self._terminate:
                    return ""
                events = self.sel.select(0.5)
                if events:
                    break
            # print("DBG: call recv")
            s = self.sock.recv(4096)
            # print(f"DBG: recvd: {len(s)}: {s}")
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
        b = s.encode()
        totsent = 0
        while totsent < len(b):
            sent = self.sock.send(b[totsent:])
            if sent == 0:
                raise RuntimeError("socket connection broken")
            totsent += sent

    def terminate(self):
        self._terminate = True


class PHD2Guider(GuiderInterface):
    """The main class for interacting with PHD2"""

    DEFAULT_STOP_CAPTURE_TIMEOUT = 10

    def __init__(
        self,
        unit: "Unit" = None,  # type: ignore[name]
        hostname="localhost",
        instance=1,
    ):
        GuiderInterface.__init__(self)
        self.unit = unit
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

        d = (
            self.unit.unit_conf["guider"]["phd2"]
            if self.unit
            else Config().get_unit()["guider"]["phd2"]
        )
        self.conf: PHD2Configuration = PHD2Configuration(**d)

        self.activities = PHD2Activities.Idle

        self.watched_process = WatchedProcess(
            command='"C:/Program Files (x86)/PHDGuiding2/phd2.exe"',
            logger=logger,
            shell=True,
        )
        self.watched_process.start()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.disconnect()

    @staticmethod
    def _is_guiding(st):
        return st == "Guiding" or st == "LostLock"

    @staticmethod
    def _get_accumulated_stats(ra, dec):
        stats = PHD2GuideStats()
        stats.rms_ra = ra.stdev()
        stats.rms_dec = dec.stdev()
        stats.peak_ra = ra.peak()
        stats.peak_dec = dec.peak()
        return stats

    def _handle_event(self, ev):
        e = ev["Event"]
        logger.debug(f"DBG: event: {e}")
        if e == "AppState":
            with self.lock:
                self.app_state = ev["State"]
                logger.debug(f"event: {e}, app_state: {self.app_state}")
                if self._is_guiding(self.app_state):
                    self.avg_dist = 0  # until we get a GuideStep event
        elif e == "Version":
            with self.lock:
                self.version = ev["PHDVersion"]
                self.sub_version = ev["PHDSubver"]
                logger.debug(f"event: {e}, {self.version=}, {self.sub_version=}")
        elif e == "StartGuiding":
            self.start_activity(PHD2Activities.Guiding)
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
            self.start_activity(PHD2Activities.Looping)
            with self.lock:
                self.app_state = "Looping"
        elif e == "LoopingExposuresStopped" or e == "GuidingStopped":
            self.end_activity(
                PHD2Activities.Looping
                if e == "LoopingExposuresStopped"
                else PHD2Activities.Guiding
            )
            with self.lock:
                self.app_state = "Stopped"
        elif e == "StarLost":
            with self.lock:
                self.app_state = "LostLock"
                self.avg_dist = ev["AvgDist"]
        else:
            print(f"DBG: todo: handle event {e}")
            pass

    def _worker(self):
        while not self._terminate:
            line = self.conn.read_line()
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
                self.conn.terminate()
                # print("DBG: joining worker")
                self.worker.join()
            self.worker = None
        if self.conn is not None:
            self.conn.disconnect()
            self.conn = None
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
            raise PHD2GuiderError(response["error"]["message"])
        return response

    def check_connected(self):
        if not self.conn.is_connected():
            raise PHD2GuiderError("PHD2 Server disconnected")

    def guide(self, settle_pixels, settle_time, settle_timeout):
        """Start guiding with the given settling parameters. PHD2 takes care
        of looping exposures, guide star selection, and settling. Call
        CheckSettling() periodically to see when settling is complete.

        """
        self.check_connected()
        s = PHD2SettleProgress()
        s.done = False
        s.distance = 0
        s.settle_px = settle_pixels
        s.time = 0
        s.settle_time = settle_time
        s.status = 0
        with self.lock:
            if self.settle and not self.settle.done:
                raise PHD2GuiderError("cannot guide while settling")
            self.settle = s
        try:
            self.call(
                "guide",
                [
                    {
                        "pixels": settle_pixels,
                        "time": settle_time,
                        "timeout": settle_timeout,
                    },
                    False,  # don't force calibration
                ],
            )
            self.settle_px = settle_pixels
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
                raise PHD2GuiderError("cannot dither while settling")
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
        self.check_connected()
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
                raise PHD2GuiderError("not settling")
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
        stats.rms_tot = math.hypot(stats.rms_ra, stats.rms_dec)
        return stats

    def stop_capture(self, timeout_seconds=10):
        """stop looping and guiding"""
        self.call("stop_capture")
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
        raise PHD2GuiderError(
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
        raise PHD2GuiderError("timed-out waiting for guiding to start looping")

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
                raise PHD2GuiderError(
                    f"invalid phd2 profile name: {self.conf.profile}"
                )
            self.stop_capture(self.DEFAULT_STOP_CAPTURE_TIMEOUT)
            self.call("set_connected", False)
            self.call("set_profile", profile_id)
        self.call("set_connected", True)

    def disconnect_equipment(self):
        """disconnect equipment"""
        self.stop_capture(self.DEFAULT_STOP_CAPTURE_TIMEOUT)
        self.call("set_connected", False)

    def get_status(self):
        """get the AppState
        (https://github.com/OpenPHDGuiding/phd2/wiki/EventMonitoring#appstate)
        and current guide error

        """
        self.check_connected()
        with self.lock:
            return self.app_state, self.avg_dist

    def status(self) -> PHD2Status:

        return PHD2Status(
            name="phd2",
            app_state=self.app_state,
            avg_dist=self.avg_dist,
            is_guiding=self.is_guiding(),
            is_settling=self.is_settling(),
            activities=int(self.activities),
            activities_verbal=self.activities.__repr__(),
        )

    def is_guiding(self) -> bool:
        """check if currently guiding"""
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

    def start_guiding(self):
        self.connect()
        self.connect_equipment()
        self.guide(
            settle_pixels=self.conf.settle.pixels,
            settle_time=self.conf.settle.time,
            settle_timeout=self.conf.settle.timeout,
        )

    def stop_guiding(self):
        self.disconnect_equipment()


if __name__ == "__main__":
    default_profile = "PWI4+ASI-native"

    with PHD2Guider() as phd2_guider:

        try:
            phd2_guider.connect()
            phd2_guider.connect_equipment()
            print(f"\nphd2 status: {phd2_guider.status()}")
            phd2_guider.start_guiding()
        except PHD2GuiderError as ex:
            logger.error(
                f"could not connect_equipment('{phd2_guider.profile_name}') error: {ex}"
            )
            phd2_guider.shutdown()
            print("bailing out")
            sys.exit(1)

    print("sleeping")
    while True:
        time.sleep(1)
