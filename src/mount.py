import math
import time
from enum import StrEnum
from logging import Logger
from typing import TYPE_CHECKING, Annotated

import win32com.client
from astropy.coordinates import Angle
from fastapi import Query
from fastapi.routing import APIRouter

from common.activities import MountActivities
from common.ascom import AscomDispatcher, ascom_run
from common.canonical import CanonicalResponse, CanonicalResponse_Ok
from common.const import Const
from common.dlipowerswitch import OutletDomain, SwitchedOutlet
from common.endpoints import Stability, Tier, add_api_route, endpoint
from common.interfaces.components import Component
from common.mast_logging import get_logger
from common.models.statuses import MountStatus, SpiralSettings
from common.parsers import (
    DEC_PATTERN,
    RA_PATTERN,
    sexagesimal_degrees_to_decimal,
    sexagesimal_hours_to_decimal,
)
from common.utils import RepeatTimer, caller_name, function_name, time_stamp
from PlaneWave import pwi4_client

if TYPE_CHECKING:
    from unit import Unit

logger = get_logger(__name__)

# class SpiralSettings(BaseModel):
#     x: float
#     y: float
#     x_step_arcsec: float
#     y_step_arcsec: float


# class MountStatus(PowerStatus, AscomStatus, ComponentStatus):
#     errors: list[str] | None = None
#     target_verbal: str | None = None
#     tracking: bool = False
#     slewing: bool = False
#     axis0_enabled: bool = False
#     axis1_enabled: bool = False
#     ra_j2000_hours: float | None = None
#     dec_j2000_degs: float | None = None
#     ha_hours: float | None = None
#     lmst_hours: float | None = None
#     fans: bool = False
#     spiral: SpiralSettings | None = None
#     date: str | None = None


class SettleMode(StrEnum):
    SLEW = "slew"  # goto / find_home / park  (large move)
    OFFSET_STEP = "offset_step"  # discrete ra/dec_add_arcsec, spiral steps
    OFFSET_GRADUAL = "offset_gradual"  # ra/dec_add_gradual_offset_*


_OFFSET_CHANNELS = ("ra", "dec", "axis0", "axis1", "path", "transverse")


def _max_dist_to_target_arcsec(st) -> float:
    """Largest absolute axis following-distance, in arcsec."""
    d0 = st.mount.axis0.dist_to_target_arcsec or 0.0
    d1 = st.mount.axis1.dist_to_target_arcsec or 0.0
    return max(abs(d0), abs(d1))


def _offset_channel(st, name: str):
    """Return the mount.offsets.<name>_arcsec section, or None if unavailable."""
    offsets = getattr(st.mount, "offsets", None)
    if offsets is None:  # PWI4 too old (offsets added in 4.0.11b5), or not reported
        return None
    return getattr(offsets, f"{name}_arcsec", None)


def target_as_text(target: str | tuple | None) -> str | None:
    """Render a mount target for the operator's status view.

    A tuple is an equatorial J2000 position, `(ra_hours, dec_DEGREES)`. A string is
    already human-readable and carries its own frame -- "Home", or the alt/az and
    apparent verbs, which must not render as bare RA/Dec.

    A function rather than a few lines inside `status()` so it can be tested: reading
    the declination as ARCSECONDS here displayed a target at -45.5 degrees as
    "-0:00:45.500", 3600x too small and plausible enough to go unnoticed.
    """
    if isinstance(target, str):
        return target
    if isinstance(target, tuple):
        return (
            f"[{Angle(target[0], unit='hour').to_string(unit='hour', sep=':', precision=3)}, "
            f"{Angle(target[1], unit='deg').to_string(unit='deg', sep=':', precision=3)}]"
        )
    return None


def _gradual_ramp_complete(prog) -> bool:
    """True when a gradual offset's ramp has finished.

    PWI4 reports ``gradual_offset_progress`` *signed by the offset direction*:
    a positive offset ramps 0 -> +1.0, a negative offset ramps 0 -> -1.0 (and may
    overshoot past |1| afterward). So completion is |progress| >= 1.0, not
    progress >= 1.0 -- the latter never fires for a negative-direction channel.
    A missing field (None) is treated as complete (nothing to wait for).
    """
    return prog is None or abs(prog) >= 1.0


class Mount(Component, SwitchedOutlet, AscomDispatcher):
    #: Bounds for `goto_alt_az`, enforced by FastAPI/pydantic on the query parameters, so
    #: a bad value is refused before the method runs and the limits appear in the OpenAPI
    #: schema. Referenced unqualified in that signature, which works because they are
    #: defined earlier in this class body and mount.py does not use string annotations.
    #:
    #: Nothing else in this codebase bounds altitude -- not the config, not this class --
    #: so below the floor the only thing between a typo and the mount driving into its
    #: pier or the enclosure is PWI4's axis limits, whose configuration this code cannot
    #: see.
    #:
    #: 15 degrees is a conservative observing floor rather than a mechanical one. If a
    #: pointing model or a horizon test legitimately needs lower, this is the number to
    #: change -- and by then it probably belongs in unit configuration, since the useful
    #: floor depends on the local horizon.
    MIN_ALTITUDE_DEGREES = 15.0
    MAX_ALTITUDE_DEGREES = 90.0

    _instance = None
    _initialized = False

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    @property
    def logger(self) -> Logger:
        return logger

    @property
    def ascom(self) -> win32com.client.Dispatch:  # type: ignore
        return self._ascom

    def __init__(self, unit: "Unit"):  # type: ignore[name]
        if self._initialized:
            return

        self.unit = unit
        assert self.unit and self.unit.unit_conf is not None
        self.conf = self.unit.unit_conf.mount
        SwitchedOutlet.__init__(self, OutletDomain.UnitOutlets, outlet_name="Mount")
        Component.__init__(self, MountActivities)

        if not self.is_on():
            self.power_on()

        self._was_shut_down: bool = False
        self.last_axis0_position_degrees: int = -99999
        self.last_axis1_position_degrees: int = -99999
        self.default_guide_rate_degs_per_second = 0.002083  # degs/sec
        self.guide_rate_degs_per_second: float
        self.guide_rate_degs_per_ms: float

        self.pw: pwi4_client.PWI4 = pwi4_client.PWI4()
        self._ascom = win32com.client.Dispatch(self.conf.ascom_driver)
        #
        # Starting with PWI4 version 4.0.99 beta 22 it will be possible to query the ASCOM driver about
        #  the GuideRate for RightAscension and Declination.  The DriverVersion shows 1.0 (disregarding the PWI4
        #  version) so we need to use the default rate.
        #
        self.guide_rate_degs_per_second = self.default_guide_rate_degs_per_second
        self.guide_rate_degs_per_ms = self.guide_rate_degs_per_second / 1000
        self.timer: RepeatTimer = RepeatTimer(2, function=self.ontimer)
        self.timer.name = "mount-timer-thread"
        self.timer.start()

        self.errors = []
        self.target: str | tuple | None = None

        self.is_moving: bool = False

        self._initialized = True
        logger.info("initialized")

    @endpoint(tier=Tier.OPERATION, stability=Stability.DEPRECATED)
    def connect(self):
        """
        Connects to the MAST mount controller
        :mastapi:
        """
        if not self.is_on():
            self.power_on()
        self.connected = True
        return CanonicalResponse_Ok

    @endpoint(tier=Tier.OPERATION, stability=Stability.DEPRECATED)
    def disconnect(self):
        """
        Disconnects from the MAST mount controller
        :mastapi:
        """
        if self.is_on():
            self.connected = False
        return CanonicalResponse_Ok

    @property
    def connected(self) -> bool:
        st = self.pw.status()
        response = ascom_run(self, "Connected", no_entry_log=True)
        return (
            self.ascom
            and (response.succeeded and response.value)
            and st.mount.is_connected  # type: ignore
            and st.mount.axis0.is_enabled  # type: ignore
            and st.mount.axis1.is_enabled  # type: ignore
        )

    @connected.setter
    def connected(self, value):  # noqa: C901
        self.errors = []
        if not self.is_on():
            self.errors.append("not powered")
            return

        st = self.pw.status()
        try:
            if value:
                response = ascom_run(self, "Connected = True")
                if response.failed:
                    self.errors.append("could not ASCOM connect")
                    logger.error(f"failed to ASCOM connect (failure='{response.failure}')")
                if not st.mount.is_connected:  # type: ignore
                    self.pw.mount_connect()
                if not st.mount.axis0.is_enabled:  # type: ignore
                    self.pw.mount_enable(0)
                if not st.mount.axis1.is_enabled:  # type: ignore
                    self.pw.mount_enable(1)
                logger.info(f"connected = {value}, axes enabled")
            else:
                if st.mount.axis0.is_enabled:  # type: ignore
                    self.pw.mount_disable(0)
                if st.mount.axis1.is_enabled:  # type: ignore
                    self.pw.mount_disable(1)
                self.pw.mount_disconnect()
                response = ascom_run(self, "Connected = False")
                if response.failed:
                    self.errors.append(response.failure)
                logger.info(f"connected = {value}, axes disabled, disconnected")
        except Exception:
            logger.exception("mount connect/disconnect failed")

    @endpoint(tier=Tier.INTERFACE)
    def endpoint_startup(self):
        return self.startup()

    def startup(self):
        """
        Performs the MAST startup routine (power ON, fans on and find home)
        :mastapi:
        """
        if not self.connected:
            self.connect()
        if not self.detected:
            return
        self.start_activity(MountActivities.StartingUp)
        self._was_shut_down = False
        self.pw.request("/fans/on")
        self.find_home()
        return CanonicalResponse_Ok

    @endpoint(tier=Tier.INTERFACE)
    def endpoint_shutdown(self):
        return self.shutdown()

    def shutdown(self):
        """
        Performs the MAST shutdown routine (fans off, park, power OFF)
        :mastapi:
        """
        if self.connected:
            self.disconnect()
        self.start_activity(MountActivities.ShuttingDown)
        self.pw.request("/fans/off")
        self.park()
        self.power_off()
        return CanonicalResponse_Ok

    @property
    def is_shutting_down(self) -> bool:
        return self.is_active(MountActivities.ShuttingDown)

    def powerdown(self):
        if not self._was_shut_down:
            self.shutdown()
        while self.is_shutting_down:
            time.sleep(1)

        self.power_off()

    @endpoint(tier=Tier.OPERATION)
    def park(self):
        """
        Parks the MAST mount
        :mastapi:
        """
        if self.connected:
            self.start_activity(MountActivities.Parking)
            self.pw.mount_park()
        return CanonicalResponse_Ok

    @endpoint(tier=Tier.OPERATION)
    def find_home(self):
        """
        Tells the MAST mount to find it's HOME indexes
        :mastapi:
        """
        if self.connected:
            self.target = "Home"
            self.start_activity(MountActivities.FindingHome)
            self.last_axis0_position_degrees = -99999
            self.last_axis1_position_degrees = -99999
            self.pw.mount_find_home()
        return CanonicalResponse_Ok

    def ontimer(self):
        if self.unit.unit_shutdown_event.is_set():
            self.timer.cancel()
            return

        if not self.connected:
            return

        status = self.pw.status()

        was_moving = self.is_moving
        assert status.mount is not None
        self.is_moving = (
            status.mount.axis0.rms_error_arcsec > 3.0  # type: ignore   # was 1.0, TODO: make it a configurable parameter
            or status.mount.axis1.rms_error_arcsec > 1.0  # type: ignore  # TODO: make it a configurable parameter
        )
        if was_moving and not self.is_moving:
            self.end_activity(MountActivities.Moving)
        elif not was_moving and self.is_moving:
            self.start_activity(
                MountActivities.Moving,
                details=([f"target={self.target}" if self.target else "unsolicited movement"]),
            )

        if self.is_active(MountActivities.FindingHome) and not self.is_moving:
            self.end_activity(MountActivities.FindingHome)
            self.target = None
            if self.is_active(MountActivities.StartingUp):
                self.end_activity(MountActivities.StartingUp)

        if self.is_active(MountActivities.Parking) and not self.is_moving:
            self.end_activity(MountActivities.Parking)
            self.target = None
            if self.is_active(MountActivities.ShuttingDown):
                self.end_activity(MountActivities.ShuttingDown)
                self._was_shut_down = True
                self.power_off()

        if self.is_active(MountActivities.Slewing) and not self.is_moving:
            self.end_activity(MountActivities.Slewing)
            self.target = None

    @property
    def is_tracking(self) -> bool:
        if not self.connected:
            return False
        st = self.pw.status()
        return st.mount.is_tracking  # type: ignore

    def wait_until_settled(
        self,
        mode: SettleMode,
        *,
        channels: tuple[str, ...] = ("ra", "dec"),
        dist_tolerance_arcsec: float = 0.5,
        stable_samples: int = 2,
        start_grace_seconds: float = 3.0,
        poll_seconds: float = 1.0,
        timeout_seconds: float = 120.0,
    ) -> bool:
        """
        Block until the mount finishes the most recently commanded motion of the
        given `mode`, then return True; return False on timeout (logged).

        Replaces the ``while self.is_moving: sleep()`` idiom, which only detects
        slew-class moves (axis rms_error > threshold) and cannot see gentle
        gradual offsets or small discrete offsets -- so it must not be used as a
        settle gate after ``mount_offset(...)``.

        SLEW           -> wait for PWI4 mount.is_slewing to clear, then dist settle.
        OFFSET_STEP    -> wait for dist_to_target to spike then settle (discrete
                          add_arcsec / spiral steps).
        OFFSET_GRADUAL -> wait for the COMMANDED `channels`' gradual_offset_progress
                          to reach 1.0, with a start-of-ramp race guard.
                          (dist_to_target can't see a ramp the servo follows.)

        TODO: source dist_tolerance_arcsec / stable_samples from unit_conf.
        """
        op = "wait_until_settled"
        if not self.connected:
            logger.warning(f"{op}: mount not connected; nothing to wait for")
            return True

        logger.info(
            f"{op}: start mode={mode} channels={channels} "
            f'tol={dist_tolerance_arcsec:.3f}" stable_samples={stable_samples} '
            f"grace={start_grace_seconds:.1f}s poll={poll_seconds:.1f}s "
            f"timeout={timeout_seconds:.0f}s"
        )

        deadline = time.monotonic() + timeout_seconds

        if mode is SettleMode.SLEW:
            return self._wait_slew(
                dist_tolerance_arcsec,
                stable_samples,
                start_grace_seconds,
                poll_seconds,
                deadline,
                op,
            )
        if mode is SettleMode.OFFSET_STEP:
            return self._wait_offset_step(
                dist_tolerance_arcsec,
                stable_samples,
                start_grace_seconds,
                poll_seconds,
                deadline,
                op,
            )
        if mode is SettleMode.OFFSET_GRADUAL:
            return self._wait_offset_gradual(
                channels,
                dist_tolerance_arcsec,
                stable_samples,
                start_grace_seconds,
                poll_seconds,
                deadline,
                op,
            )
        raise ValueError(f"{op}: unknown mode {mode!r}")

    def _deadline_passed(self, deadline: float, op: str) -> bool:
        if time.monotonic() >= deadline:
            logger.error(f"{op}: TIMEOUT")
            return True
        return False

    def _wait_slew(
        self,
        dist_tolerance_arcsec: float,
        stable_samples: int,
        start_grace_seconds: float,
        poll_seconds: float,
        deadline: float,
        op: str,
    ) -> bool:
        # Phase A: confirm the slew actually started (guards stale is_slewing=False).
        grace_end = time.monotonic() + start_grace_seconds
        started = False
        while time.monotonic() < grace_end:
            if self.pw.status().mount.is_slewing:  # type: ignore
                started = True
                break
            time.sleep(poll_seconds)
        # Phase B: wait for the slew to clear.
        if started:
            logger.info(f"{op}(SLEW): slew detected; waiting for it to clear")
            while self.pw.status().mount.is_slewing:  # type: ignore
                if self._deadline_passed(deadline, f"{op}(SLEW)"):
                    return False
                time.sleep(poll_seconds)
            logger.info(f"{op}(SLEW): is_slewing cleared")
        else:
            logger.warning(
                f"{op}(SLEW): slew not observed within {start_grace_seconds:.1f}s grace; proceeding to dist settle"
            )
        # Phase C: settle on following-distance.
        return self._wait_dist_settle(dist_tolerance_arcsec, stable_samples, poll_seconds, deadline, op)

    def _wait_offset_step(
        self,
        dist_tolerance_arcsec: float,
        stable_samples: int,
        start_grace_seconds: float,
        poll_seconds: float,
        deadline: float,
        op: str,
    ) -> bool:
        # Phase A: confirm the step registered (dist spikes); else grace out
        # so a tiny sub-tolerance step does not hang here.
        grace_end = time.monotonic() + start_grace_seconds
        spiked = False
        while time.monotonic() < grace_end:
            dist = _max_dist_to_target_arcsec(self.pw.status())
            if dist > dist_tolerance_arcsec:
                logger.info(f'{op}(OFFSET_STEP): step registered (dist_to_target={dist:.3f}" > tol)')
                spiked = True
                break
            time.sleep(poll_seconds)
        if not spiked:
            logger.info(
                f"{op}(OFFSET_STEP): no dist spike within "
                f"{start_grace_seconds:.1f}s grace (sub-tolerance step); "
                f"proceeding to settle"
            )
        # Phase B: settle.
        return self._wait_dist_settle(dist_tolerance_arcsec, stable_samples, poll_seconds, deadline, op)

    def _wait_offset_gradual(
        self,
        channels: tuple[str, ...],
        dist_tolerance_arcsec: float,
        stable_samples: int,
        start_grace_seconds: float,
        poll_seconds: float,
        deadline: float,
        op: str,
    ) -> bool:
        chans = [c for c in channels if c in _OFFSET_CHANNELS]
        if not chans:
            logger.warning(f"{op}: no valid gradual channels in {channels!r}; not waiting")
            return True

        st0 = self.pw.status()
        if _offset_channel(st0, chans[0]) is None:
            logger.warning(f"{op}: PWI4 does not report mount.offsets; falling back to dist settle")
            return self._wait_dist_settle(dist_tolerance_arcsec, stable_samples, poll_seconds, deadline, op)

        baseline_total = {c: (getattr(_offset_channel(st0, c), "total", 0.0) or 0.0) for c in chans}

        # Phase A: confirm each commanded channel's ramp has started.
        self._wait_gradual_ramp_start(chans, baseline_total, start_grace_seconds, poll_seconds, op)

        # Phase B: wait for every commanded channel's ramp to complete.
        while True:
            if self._deadline_passed(deadline, f"{op}(OFFSET_GRADUAL)"):
                return False
            st = self.pw.status()
            progresses = {c: getattr(_offset_channel(st, c), "gradual_offset_progress", 1.0) for c in chans}
            logger.debug(
                f"{op}(OFFSET_GRADUAL): progress "
                + ", ".join(f"{c}={(p if p is not None else 1.0):.2f}" for c, p in progresses.items())
            )
            done = all(_gradual_ramp_complete(p) for p in progresses.values())
            if done:
                break
            time.sleep(poll_seconds)
        logger.info(f"{op}(OFFSET_GRADUAL): all ramps complete")

        # Phase C: brief following-distance settle (catches residual servo lag).
        return self._wait_dist_settle(dist_tolerance_arcsec, stable_samples, poll_seconds, deadline, op)

    def _wait_gradual_ramp_start(
        self,
        chans: list[str],
        baseline_total: dict[str, float],
        start_grace_seconds: float,
        poll_seconds: float,
        op: str,
    ) -> None:
        """Wait until each channel's gradual ramp starts (|progress| < 1 or total moved).

        Guards the stale "|progress| == 1" race right after commanding the offset.
        Logs a warning for any channel whose ramp was never observed within grace.
        """
        grace_end = time.monotonic() + start_grace_seconds
        started = {c: False for c in chans}
        while not all(started.values()) and time.monotonic() < grace_end:
            st = self.pw.status()
            for c in chans:
                if started[c]:
                    continue
                ch = _offset_channel(st, c)
                prog = getattr(ch, "gradual_offset_progress", 1.0)
                total = getattr(ch, "total", 0.0) or 0.0
                if not _gradual_ramp_complete(prog) or abs(total - baseline_total[c]) > 1e-6:
                    started[c] = True
                    logger.info(f"{op}(OFFSET_GRADUAL): ramp started on '{c}'")
            time.sleep(poll_seconds)
        not_started = [c for c, ok in started.items() if not ok]
        if not_started:
            logger.warning(
                f"{op}(OFFSET_GRADUAL): ramp not observed on {not_started} "
                f"within {start_grace_seconds:.1f}s grace; proceeding anyway"
            )

    def _wait_dist_settle(
        self,
        tol_arcsec: float,
        stable_samples: int,
        poll_seconds: float,
        deadline: float,
        op: str,
    ) -> bool:
        """Wait until max axis dist_to_target stays < tol for `stable_samples` reads."""
        in_tol = 0
        while in_tol < stable_samples:
            if time.monotonic() >= deadline:
                logger.error(f"{op}: TIMEOUT during dist settle")
                return False
            dist = _max_dist_to_target_arcsec(self.pw.status())
            if dist < tol_arcsec:
                in_tol += 1
            else:
                in_tol = 0
            logger.debug(f"{op}: dist_to_target={dist:.3f} arcsec, in_tol={in_tol}/{stable_samples}")
            time.sleep(poll_seconds)
        logger.info(f"{op}: dist_to_target settled (< {tol_arcsec:.3f} arcsec for {stable_samples} samples)")
        return True

    @endpoint(tier=Tier.INTERFACE)
    def endpoint_status(self) -> MountStatus:
        # Enveloped at registration; `status()` stays a bare typed model (MAST_common#70).
        return self.status()

    def status(self) -> MountStatus:
        target_verbal = target_as_text(self.target)

        activities = self.activities  # integrate activities we may have not started
        st = None
        if self.connected:
            st = self.pw.status()
            if st.mount.is_tracking:  # type: ignore
                activities |= MountActivities.Tracking
            else:
                activities &= ~MountActivities.Tracking
            if st.mount.is_slewing:  # type: ignore
                activities |= MountActivities.Slewing
            else:
                activities &= ~MountActivities.Slewing
            if self.is_moving:
                activities |= MountActivities.Moving
            else:
                activities &= ~MountActivities.Moving

        component_status = self.component_status()
        component_status.activities = activities

        spiral = (
            SpiralSettings(
                x=st.mount.spiral_offset.x,  # type: ignore
                y=st.mount.spiral_offset.y,  # type: ignore
                x_step_arcsec=st.mount.spiral_offset.x_step_arcsec,  # type: ignore
                y_step_arcsec=st.mount.spiral_offset.x_step_arcsec,  # type: ignore
            )
            if st
            else None
        )

        return MountStatus(
            **self.power_status().model_dump(),
            **self.ascom_status().model_dump(),
            **component_status.model_dump(),
            errors=self.errors,
            target_verbal=target_verbal,
            axis0_enabled=st.mount.axis0.is_enabled if st else False,  # type: ignore
            axis1_enabled=st.mount.axis1.is_enabled if st else False,  # type: ignore
            ra_j2000_hours=st.mount.ra_j2000_hours if st else None,  # type: ignore
            ha_hours=(st.site.lmst_hours - st.mount.ra_j2000_hours) if st else None,  # type: ignore
            lmst_hours=st.site.lmst_hours if st else None,  # type: ignore
            fans=True,
            spiral=spiral,
            date=time_stamp(),
        )

    @endpoint(tier=Tier.OPERATION)
    def start_tracking(self):
        """
        Tell the ``mount`` to start tracking
        :mastapi:
        """
        if not self.connected:
            # Was a bare `return`, which answered HTTP `null` -- indistinguishable from the
            # success path, which also returned nothing (invariant 4).
            return CanonicalResponse(errors=["not connected"])

        self.pw.mount_tracking_on()
        time.sleep(1)
        st = self.pw.status()
        while not st.mount.is_tracking:  # type: ignore
            time.sleep(1)
            st = self.pw.status()
        logger.info(f"started tracking (from {caller_name()})")
        return CanonicalResponse_Ok

    @endpoint(tier=Tier.OPERATION)
    def stop_tracking(self):
        """
        Tell the ``mount`` to stop tracking
        :mastapi:
        """
        if not self.connected:
            return CanonicalResponse(errors=["not connected"])

        self.pw.mount_tracking_off()
        time.sleep(1)
        st = self.pw.status()
        while st.mount.is_tracking:  # type: ignore
            time.sleep(1)
            st = self.pw.status()
        logger.info(f"stopped tracking (from {caller_name()})")
        return CanonicalResponse_Ok

    def goto_ra_dec_j2000(self, ra: float, dec: float):
        """Slew to an equatorial J2000 position. Internal API: RAISES on failure.

        Four callers (acquirer, autofocusing, stage_geometry, dance) invoke this directly
        and ignore the return value, so a failure reported by returning would become a
        silent no-op -- an acquisition carrying on believing it had slewed.
        `endpoint_goto_ra_dec_j2000` is what converts a failure into a CanonicalResponse.
        """
        self.target = (ra, dec)
        self.start_activity(MountActivities.Slewing, details=[f"target={self.target}"])
        try:
            self.pw.mount_goto_ra_dec_j2000(ra, dec)
        except Exception:
            # `ontimer` only ends Slewing after seeing the mount move and stop; a slew
            # PWI4 never accepted never moves, so without this the activity sticks.
            self.end_activity(MountActivities.Slewing)
            self.target = None
            raise

    @endpoint(tier=Tier.OPERATION)
    def endpoint_goto_ra_dec_j2000(
        self,
        ra_j2000_hours: Annotated[
            str | float,
            Query(
                pattern=RA_PATTERN,
                description=(
                    "### Right Ascension (J2000), as decimal hours (e.g. `12.5`) or "
                    "sexagesimal (e.g. `12:30:45.123`).\n"
                    "- range: `0 <= RA < 24`"
                ),
            ),
        ],
        dec_j2000_degs: Annotated[
            str | float,
            Query(
                pattern=DEC_PATTERN,
                description=(
                    "### Declination (J2000), as decimal degrees (e.g. `-45.5`) or "
                    "sexagesimal (e.g. `-45:30:00.123`).\n"
                    "- range: `-90 <= DEC <= 90`"
                ),
            ),
        ],
    ) -> CanonicalResponse:
        """
        Slew the ``mount`` to an equatorial J2000 position.<br>
        Both sexagesimal and decimal forms are accepted, as `unit.expose` accepts them.<br>
        Tracking is **not** touched -- unlike `goto_alt_az`, an equatorial target is meant
        to be tracked.<br>
        Returns as soon as PWI4 has accepted the slew; the **Slewing** activity, which
        carries the target, is what says when it finishes.
        :mastapi:
        """
        return self._goto_equatorial(self.goto_ra_dec_j2000, ra_j2000_hours, dec_j2000_degs, function_name())

    def goto_ra_dec_apparent(self, ra: float, dec: float):
        """Slew to an equatorial position of DATE. Internal API: RAISES on failure.

        Apparent, not J2000: coordinates already reduced for precession, nutation and
        aberration. Confusing the two mispoints by however far the frames have drifted --
        about 22 arcmin by 2026, which is over half of this telescope's field, and the
        two calls take identical-looking arguments. Hence the frame in `target` below.
        """
        # A string, and one that names the frame: a tuple renders as bare RA/Dec, which
        # would be indistinguishable from a J2000 target in the operator's status view.
        self.target = (
            f"apparent [{Angle(ra, unit='hour').to_string(unit='hour', sep=':', precision=3)}, "
            f"{Angle(dec, unit='deg').to_string(unit='deg', sep=':', precision=3)}]"
        )
        self.start_activity(MountActivities.Slewing, details=[f"target={self.target}"])
        try:
            self.pw.mount_goto_ra_dec_apparent(ra, dec)
        except Exception:
            self.end_activity(MountActivities.Slewing)
            self.target = None
            raise

    @endpoint(tier=Tier.OPERATION)
    def endpoint_goto_ra_dec_apparent(
        self,
        ra_apparent_hours: Annotated[
            str | float,
            Query(
                pattern=RA_PATTERN,
                description=(
                    "### Right Ascension **of date**, as decimal hours (e.g. `12.5`) or "
                    "sexagesimal (e.g. `12:30:45.123`).\n"
                    "- range: `0 <= RA < 24`\n"
                    "- **not J2000** -- already reduced for precession, nutation and aberration"
                ),
            ),
        ],
        dec_apparent_degs: Annotated[
            str | float,
            Query(
                pattern=DEC_PATTERN,
                description=(
                    "### Declination **of date**, as decimal degrees (e.g. `-45.5`) or "
                    "sexagesimal (e.g. `-45:30:00.123`).\n"
                    "- range: `-90 <= DEC <= 90`\n"
                    "- **not J2000** -- see above"
                ),
            ),
        ],
    ) -> CanonicalResponse:
        """
        Slew the ``mount`` to an equatorial position **of date** (apparent).<br>
        Use `goto_ra_dec_j2000` for catalog coordinates -- which is nearly always what you
        have, since plans, plate solves and catalogs are all J2000. This verb is for
        coordinates already reduced to date, such as an ephemeris that hands you apparent
        places directly.<br>
        **Passing J2000 numbers here mispoints by roughly 22 arcmin (2026)** -- over half
        this telescope's field -- and nothing can detect it, since both frames are valid
        coordinates.<br>
        Tracking is not touched. Returns as soon as PWI4 has accepted the slew; the
        **Slewing** activity, which names the frame, is what says when it finishes.
        :mastapi:
        """
        return self._goto_equatorial(self.goto_ra_dec_apparent, ra_apparent_hours, dec_apparent_degs, function_name())

    def _goto_equatorial(self, slew, ra_in, dec_in, op: str) -> CanonicalResponse:
        """Shared body of the two equatorial endpoints: guard, convert, slew, report.

        The frames differ; everything around them does not. `slew` is the internal method,
        which raises -- this is what turns that into a CanonicalResponse for HTTP callers.
        """
        if not self.connected:
            msg = f"{op}: not connected"
            logger.error(msg)
            return CanonicalResponse(errors=[msg])

        # The patterns on the parameters accept the FORM; these enforce the ranges and
        # convert. Both frames share the same bounds -- RA [0, 24), Dec [-90, 90].
        try:
            ra = sexagesimal_hours_to_decimal(ra_in)
            dec = sexagesimal_degrees_to_decimal(dec_in)
        except ValueError as e:
            logger.error(f"{op}: {e}")
            return CanonicalResponse(errors=[f"{op}: {e}"])

        try:
            slew(ra, dec)
        except Exception as e:
            error = f"{op}: {e}"
            logger.exception(error)
            return CanonicalResponse(errors=[error])

        logger.info(f"{op}: slewing to ra={ra}, dec={dec}")
        return CanonicalResponse_Ok

    @endpoint(tier=Tier.OPERATION)
    def goto_alt_az(
        self,
        alt_degs: Annotated[
            float,
            Query(
                ge=MIN_ALTITUDE_DEGREES,
                le=MAX_ALTITUDE_DEGREES,
                description=(
                    "#### Altitude above the horizon, in **degrees as a plain decimal**.\n"
                    f"- range: `{MIN_ALTITUDE_DEGREES:g}` to `{MAX_ALTITUDE_DEGREES:g}`\n"
                    "- the lower bound is an observing floor, not a mechanical one"
                ),
            ),
        ],
        az_degs: Annotated[
            float,
            Query(
                ge=0.0,
                lt=360.0,
                description=(
                    "#### Azimuth, in **degrees as a plain decimal**, measured from North through East.\n"
                    "- range: `0` (inclusive) to `360` (exclusive)"
                ),
            ),
        ],
    ) -> CanonicalResponse:
        """
        Slew the ``mount`` to an altitude/azimuth.<br>
        Tracking is **stopped** first: an alt/az target is a fixed direction -- a flat
        panel, a pointing check, a spot on the horizon -- and sidereal tracking would
        drag the mount straight off it.<br>
        Returns as soon as PWI4 has accepted the slew, like the other goto verbs; the
        **Slewing** activity, which carries the target, is what says when it finishes.
        :mastapi:
        """
        op = function_name()

        # Ranges are enforced by FastAPI/pydantic on the parameters above, so anything
        # arriving here is already a float within bounds. What is left to check is the
        # resource: whether there is a mount to drive at all.
        if not self.connected:
            msg = f"{op}: not connected"
            logger.error(msg)
            return CanonicalResponse(errors=[msg])

        self.stop_tracking()

        # A string, not a tuple: status() renders a tuple target as RA/Dec, so an alt/az
        # pair put there would be displayed as a sky position it is not.
        self.target = f"alt={alt_degs:g}, az={az_degs:g}"
        # Carried on the activity as well as on `target`: the activity is what a client
        # watching /status sees while the slew is in flight, and "Slewing" on its own does
        # not say where to -- which is the first thing anyone asks of a moving telescope.
        self.start_activity(MountActivities.Slewing, details=[f"target={self.target}"])
        try:
            self.pw.mount_goto_alt_az(alt_degs=alt_degs, az_degs=az_degs)
        except Exception as e:
            # Ended here because nothing else will: `ontimer` only ends Slewing after
            # seeing the mount move and stop, and a slew PWI4 never accepted means it
            # never moves -- so the activity would stay set until something unrelated
            # cleared it, with the unit reporting a slew that is not happening.
            self.end_activity(MountActivities.Slewing)
            self.target = None
            error = f"{op}: {e}"
            logger.exception(error)
            return CanonicalResponse(errors=[error])

        logger.info(f"{op}: slewing to alt={alt_degs:g}, az={az_degs:g}")
        return CanonicalResponse_Ok

    @endpoint(tier=Tier.INTERFACE)
    def endpoint_abort(self):
        return self.abort()

    def abort(self):
        """
        Aborts any in-progress mount activities

        :mastapi:
        Returns
        -------

        """
        for activity in (
            MountActivities.FindingHome,
            MountActivities.StartingUp,
            MountActivities.ShuttingDown,
            MountActivities.Dancing,
            MountActivities.Slewing,
        ):
            if self.is_active(activity):
                self.end_activity(activity)
        self.pw.mount_stop()
        self.pw.mount_tracking_off()
        return CanonicalResponse_Ok

    @property
    def operational(self) -> bool:
        st = self.pw.status()
        return all(
            [
                self.is_on(),
                self.detected,
                self.connected,
                not self.was_shut_down,
                self.ascom,
                st.mount.is_connected,  # type: ignore
                st.mount.axis0.is_enabled,  # type: ignore
                st.mount.axis1.is_enabled,  # type: ignore
            ]
        )

    @property
    def is_slewing(self):
        return self.pw.status().mount.is_slewing  # type: ignore

    @property
    def why_not_operational(self) -> list[str]:
        st = self.pw.status()
        label = f"{self.name}"
        ret = []
        if not self.is_on():
            ret.append(f"{label}: not powered")
        elif not self.detected:
            ret.append(f"{label}: (via PWI4) not detected")
        elif self.was_shut_down:
            ret.append(f"{label}: shut down")
        else:
            if self.ascom:
                response = ascom_run(self, "Connected")
                if response.succeeded and not response.value:
                    ret.append(f"{label}: (via ASCOM) - not connected")
            else:
                ret.append(f"{label}: (via ASCOM) - no handle")

            if not st.mount.is_connected:  # type: ignore
                ret.append(f"{label}: (via PWI4) - not connected")
            else:
                if not st.mount.axis0.is_enabled:  # type: ignore
                    ret.append(f"{label}: (via PWI4) - axis0 not enabled")
                if not st.mount.axis1.is_enabled:  # type: ignore
                    ret.append(f"{label}: (via PWI4) - axis1 not enabled")
        return ret

    @property
    def name(self) -> str:
        return "mount"

    @property
    def detected(self) -> bool:
        st = self.pw.status()
        return st.mount.is_connected  # type: ignore

    @property
    def was_shut_down(self) -> bool:
        return self._was_shut_down

    @endpoint(tier=Tier.DEMO)
    def dance(self):
        coordinates = cone_coordinates_generator()
        logger.info("dance: starting to dance")
        self.start_activity(MountActivities.Dancing)
        self.find_home()
        for coord in coordinates:
            logger.info("dance: dancing to {coord=}")
            self.goto_ra_dec_j2000(coord[0], coord[1])
            time.sleep(2)  # let it start moving
            stat = self.pw.status()
            while stat.mount.is_slewing:  # type: ignore
                time.sleep(2)
                stat = self.pw.status()
            logger.info(f"dance: resting 10 seconds at {coord=}")
            time.sleep(10)
        logger.info("dance: done dancing")
        self.find_home()
        self.end_activity(MountActivities.Dancing)
        return CanonicalResponse_Ok

    @property
    def api_router(self) -> APIRouter:
        """
        Returns a FastAPI router with all the mount API endpoints."""
        base_path = Const.BASE_UNIT_PATH + "/mount"
        tag = "Mount"

        router = APIRouter()
        add_api_route(router, base_path + "/startup", tags=[tag], endpoint=self.endpoint_startup, methods=["PUT"])
        add_api_route(router, base_path + "/shutdown", tags=[tag], endpoint=self.endpoint_shutdown, methods=["PUT"])
        add_api_route(router, base_path + "/abort", tags=[tag], endpoint=self.endpoint_abort, methods=["PUT"])
        add_api_route(router, base_path + "/status", tags=[tag], endpoint=self.endpoint_status)
        add_api_route(router, base_path + "/connect", tags=[tag], endpoint=self.connect)
        add_api_route(router, base_path + "/disconnect", tags=[tag], endpoint=self.disconnect)
        add_api_route(router, base_path + "/start_tracking", tags=[tag], endpoint=self.start_tracking, methods=["PUT"])
        add_api_route(router, base_path + "/stop_tracking", tags=[tag], endpoint=self.stop_tracking, methods=["PUT"])
        add_api_route(router, base_path + "/park", tags=[tag], endpoint=self.park, methods=["PUT"])
        add_api_route(router, base_path + "/find_home", tags=[tag], endpoint=self.find_home, methods=["PUT"])
        # PUT per invariant 5 (#48). `/goto` and the divergent `goto()` it pointed at
        # were retired in #37: the equatorial and horizontal slews are separate verbs.
        add_api_route(
            router,
            base_path + "/goto_ra_dec_j2000",
            methods=["PUT"],
            tags=[tag],
            endpoint=self.endpoint_goto_ra_dec_j2000,
        )
        add_api_route(
            router,
            base_path + "/goto_ra_dec_apparent",
            methods=["PUT"],
            tags=[tag],
            endpoint=self.endpoint_goto_ra_dec_apparent,
        )
        # PUT, not GET: invariant 5 of the endpoint contract (#48, decided 2026-07-20) --
        # state-changing routes are PUT so a caching proxy, a link prefetch or a Swagger
        # "try it out" cannot fire a slew. The sibling verbs are still GET pending that
        # sweep; a new route has no reason to be added to the pile.
        add_api_route(router, base_path + "/goto_alt_az", methods=["PUT"], tags=[tag], endpoint=self.goto_alt_az)
        add_api_route(router, base_path + "/dance", tags=[tag], endpoint=self.dance, methods=["PUT"])

        return router


# Function to generate cone coordinates
def cone_coordinates_generator(steps=20, base_radius=30, rotation_axis_ra=0, rotation_axis_dec=60):
    cone_coordinates = []
    for i in range(steps):
        angle = i * 2 * math.pi / steps
        ra = rotation_axis_ra + base_radius * math.cos(angle)
        dec = rotation_axis_dec + base_radius * math.sin(angle)
        cone_coordinates.append((ra, dec))

    # Combine all steps
    return [(rotation_axis_ra, rotation_axis_dec)] + cone_coordinates
