"""One run of `acquire_and_focus_fiber`.

A sibling of the other flux-metering runs, sharing nothing with them but read-only
helpers, the `UnitActivities.FluxMetering` flag (so the flux-metering runs refuse to run
together) and the hardware.

Acquire a star, put the folding mirror in, start guiding, wait for PHD2 to report that
guiding has settled, then step the focuser through a fixed list of positions, taking
ThorCam frames at each. That is all: nothing here measures a flux or picks a best focus.
The products are the frames and `result.json` under `<night>/FiberFocus/<NNNN>`, and every
analysis is done afterwards, off the unit.

The decisions that are easiest to undo by accident, and why they are what they are:

* **Guiding must not influence the run.** The run starts guiding itself, through the same
  `PHD2Connector.start_guiding` that `/start_guiding` reaches, and waits for PHD2's
  `SettleDone`. After that it never talks to PHD2 and ignores guide steps, lost stars and
  PHD2's state: it uses only the focuser and the ThorCam.
* **Settled means `SettleDone`, read in-process.** Not the unit's `Guiding` flag, which
  `start_guiding` raises just BEFORE asking PHD2 to guide, so it only means "asked". The
  connector records `SettleDone` from PHD2's event stream in `settle`, and `guide()`
  replaces that record with a fresh one, so the record read after `start_guiding` returns
  is this run's, never one left over from an earlier guiding session.
* **A settle that fails fails the run**: `SettleDone` with an error status, or no
  `SettleDone` within `guide_settle_timeout_seconds` of the `guide` command. The two are
  reported differently, so PHD2's own refusal is never confused with our backstop.
* **Every run ends the same way**: the focuser goes back to `known_as_good_position`, read
  at the end, and guiding is left running. (`Unit.abort` stops guiding itself, so an
  aborted run is the exception: see `Unit.abort`.)
* **The focuser position in a frame's name is MEASURED**, read once the focuser has
  arrived and settled. The focuser counts as arrived within `Focuser.CLOSE_ENOUGH` of the
  target, so the commanded value is not where it actually was. Both are in the header.
* **`result.json` is rewritten after every position**, on the ram disk, and moved to the
  share once at the end.
* Nothing here writes configuration.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
from astropy.io import fits
from pydantic import BaseModel, Field

from common.activities import FocuserActivities, UnitActivities
from common.canonical import CanonicalResponse
from common.filer import Filer, MoveGuardian
from common.mast_logging import get_logger
from common.paths import PathMaker
from common.utils import function_name
from flux_metering.flux_meter import FluxMeter, FluxMeterError
from flux_metering.remote import PREFLIGHT_TIMEOUT_S, RemoteFluxMeter
from flux_metering.session import isoformat_utc
from phd2.phd2 import PHD2Connector
from stage import StagePresetPosition

if TYPE_CHECKING:
    from unit import Unit

logger = get_logger(__name__)
filer = Filer(logger)

#: How long to wait for the folding mirror to reach SPEC. A Sky->Spec traverse was measured
#: at 21-22 seconds on mast02 (2026-09-02), so this is a generous ceiling on a move that
#: normally takes half a minute -- not a tolerance.
STAGE_TIMEOUT_SECONDS = 120.0

#: How long to wait for one focuser move. A ceiling on a move, not a tolerance: a step of a
#: sweep takes seconds, and the whole 30,000-step travel well under this.
FOCUSER_TIMEOUT_SECONDS = 120.0

#: How often the waits below look at what they are waiting for, and at the abort.
POLL_SECONDS = 0.2

#: Below this much free space on the ram disk, a position waits for the mover to catch up
#: before exposing. A ThorCam frame is ~3.5 MB, so this is hundreds of frames of headroom --
#: room to ride out a share stall, which is the only way the disk fills.
RAM_DISK_MIN_FREE_BYTES = 1024**3

#: How long to let the mover drain before giving up on a run.
RAM_DISK_DRAIN_TIMEOUT_SECONDS = 120.0

#: The position index is three digits of the frame names.
MAX_POSITIONS = 999


class FiberFocusError(Exception):
    """A fiber-focus run could not be set up, or could not do something it needs to do."""


@dataclass
class FiberFocusParams:
    """What the operator chose. Carried whole so the result can echo it back."""

    ra_j2000_hours: float
    dec_j2000_degs: float
    #: The first focuser position. The endpoint defaults it to `known_as_good_position`,
    #: read when the request arrives, so the value recorded here is always a number.
    start: int
    #: Focuser steps between positions. Signed: a negative step sweeps inward.
    step: int
    #: How many positions, `start` included.
    count: int
    #: Imager exposure for the acquisition's plate solves.
    acquisition_seconds: float = 5.0
    #: Imager gain for the acquisition. None takes the configured acquisition gain.
    gain: int | None = None
    #: ThorCam exposure, for every frame.
    flux_seconds: float = 5.0
    flux_gain: int = 0
    flux_black_level: int = 3
    frames_per_position: int = 1
    #: Wait after the focuser has arrived, before the position is read and exposed.
    settle_seconds: float = 2.0
    #: Our own backstop on PHD2's settle, from the `guide` command to `SettleDone`. If PHD2
    #: has to calibrate first, this may expire before it is done.
    guide_settle_timeout_seconds: float = 120.0

    @property
    def positions(self) -> list[int]:
        return [self.start + i * self.step for i in range(self.count)]

    @property
    def flux_exposure_us(self) -> int:
        """The ThorCam is commanded in microseconds."""
        return round(self.flux_seconds * 1_000_000)


def frame_name(index: int, measured: int, frame: int) -> str:
    """`flux-PPP-FFFFF-MM.fits`: position index from 001, MEASURED focuser position, and
    the frame within the position from 00."""
    return f"flux-{index + 1:03d}-{measured:05d}-{frame:02d}.fits"


class FiberFocusFrame(BaseModel):
    name: str
    started_utc: str
    ended_utc: str


class FiberFocusPosition(BaseModel):
    """One focuser position: where it was sent, where it was, and what was taken there."""

    index: int
    commanded: int
    measured: int
    settled_utc: str
    frames: list[FiberFocusFrame] = Field(default_factory=list)


class GuideSettle(BaseModel):
    """How PHD2's settle went, as this run saw it."""

    #: `settled`, `failed` (PHD2 reported an error), `timeout` (no `SettleDone` in time) or
    #: `aborted`.
    outcome: str | None = None
    #: PHD2's `SettleDone` status (0 is success) and error text, when one arrived.
    status: int | None = None
    error: str | None = None
    guide_commanded_utc: str | None = None
    settled_utc: str | None = None
    waited_seconds: float | None = None


class FiberFocusStatus(BaseModel):
    """A fiber-focus run, in progress or finished."""

    active: bool = False
    #: `idle`, `acquiring`, `positioning`, `guiding`, `focusing` or `returning`.
    phase: str = "idle"
    folder: str | None = None
    started_at: str | None = None
    ended_at: str | None = None

    #: Index of the position being (or last) exposed, from 0.
    index: int | None = None
    frames: int = 0
    guide_settle: GuideSettle | None = None

    #: `completed`, `aborted`, `disk_full`, `acquisition_failed`, `guiding_failed` or `failed`.
    terminal_state: str | None = None
    last_error: str | None = None

    #: Where the focuser was sent at the end, and where it was when the run let go of it.
    final_focuser_commanded: int | None = None
    final_focuser_measured: int | None = None

    positions: list[FiberFocusPosition] = Field(default_factory=list)


def phd2_backend(unit: Unit) -> PHD2Connector | None:
    """The unit's guider backend when it is PHD2, the only one that reports `SettleDone`."""
    guider = unit.guider
    backend = guider._backend if guider is not None else None  # noqa: SLF001 -- no public accessor
    return backend if isinstance(backend, PHD2Connector) else None


class FiberFocusSession:
    """One run at a time, owned by the Unit."""

    def __init__(self, unit: Unit, flux_meter: FluxMeter | None = None):
        self.unit = unit
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        #: Injected for tests; `None` means reach the flux-meter service on the site's spec
        #: host when the run starts.
        self._injected_meter = flux_meter
        self._meter: FluxMeter | None = None
        self.state = FiberFocusStatus()
        self.params: FiberFocusParams | None = None
        self.positions: list[FiberFocusPosition] = []

    # ------------------------------------------------------------------- control --

    @property
    def is_active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def require_can_start(self, params: FiberFocusParams) -> str | None:
        """Why a run cannot start, or None. Checked BEFORE the thread is dispatched.

        The unit-busy check is what keeps this run from silently sharing the hardware:
        neither the acquirer, the focuser nor the ThorCam service refuses a second user,
        so without it two runs would produce frames that look right and are not.
        """
        if self.is_active:
            return "a fiber-focus run is already in progress"
        if self.unit.mount is None or self.unit.imager is None:
            return "the mount and the imager must both be present"
        busy = (
            UnitActivities.Acquiring
            | UnitActivities.Guiding
            | UnitActivities.Autofocusing
            | UnitActivities.StartingUp
            | UnitActivities.ShuttingDown
            # Every flux-metering run raises this flag, this one included, so this also
            # refuses while any other flux-metering run is active.
            | UnitActivities.FluxMetering
        )
        if self.unit.activities & busy:
            return f"the unit is busy ({self.unit.activities_verbal})"
        if self.unit.acquirer is None:
            return "no acquirer, so the star cannot be put on the fibre"
        if self.unit.stage is None:
            return "no stage, so the folding mirror cannot be put in"
        if phd2_backend(self.unit) is None:
            return "the guider backend is not PHD2, so there is no SettleDone to wait for"

        focuser = self.unit.focuser
        if focuser is None:
            return "no focuser"
        if focuser.is_active(FocuserActivities.Moving):
            return "the focuser is moving"
        if params.count > 1 and params.step == 0:
            return "a step of 0 would expose the same position again and again"
        if params.count > MAX_POSITIONS:
            return f"at most {MAX_POSITIONS} positions: the index is three digits of the frame names"
        # The same limits, and the same comparisons, as `Focuser.move_relative`.
        outside = [
            p for p in params.positions if p < focuser.lower_limit or (focuser.upper_limit and p >= focuser.upper_limit)
        ]
        if outside:
            return (
                f"positions {outside[0]}..{outside[-1]} are outside the focuser's range "
                f"[{focuser.lower_limit}, {focuser.upper_limit})"
            )

        free = self._ram_disk_free_bytes()
        if free is not None and free < RAM_DISK_MIN_FREE_BYTES:
            return (
                f"only {free / 1024**3:.1f} GB free where the frames are written; "
                f"at least {RAM_DISK_MIN_FREE_BYTES / 1024**3:.0f} GB is wanted before starting"
            )

        # Last, being the one check that goes over the network.
        if self._injected_meter is None:
            try:
                RemoteFluxMeter.at_spec_host(timeout_s=PREFLIGHT_TIMEOUT_S)
            except FluxMeterError as ex:
                return f"the flux meter is not available: {ex}"
        return None

    def start(self, params: FiberFocusParams):
        """Validate, claim the unit, and dispatch. Returns a refusal or the initial state."""
        with self._lock:
            refusal = self.require_can_start(params)
            if refusal is not None:
                return refusal

            self.params = params
            self.positions = []
            self._stop.clear()
            folder = PathMaker().make_fiber_focus_folder()
            self.state = FiberFocusStatus(
                active=True,
                phase="acquiring",
                folder=folder,
                started_at=isoformat_utc(),
            )
            # Raised here rather than in the thread: the endpoint declares this flag as its
            # completion signal, so a caller answered Ok must find it already set.
            self.unit.start_activity(UnitActivities.FluxMetering)
            self._thread = threading.Thread(
                name="acquire_and_focus_fiber",
                target=self.do_acquire_and_focus_fiber,
                daemon=True,
            )
            self._thread.start()
            logger.info(
                f"fiber focus started: {params.count} positions from {params.start} by {params.step}, "
                f"products under '{folder}'"
            )
            return self.status()

    def abort(self) -> None:
        """Ask the run to stop. It finishes the frame it is inside, then unwinds."""
        if self.is_active:
            logger.info("fiber focus: abort requested")
            self._stop.set()

    def status(self) -> FiberFocusStatus:
        """The typed model, bare, as every status endpoint returns it."""
        self.state.active = self.is_active
        self.state.positions = list(self.positions)
        return self.state

    # -------------------------------------------------------------------- the run --

    def do_acquire_and_focus_fiber(self) -> None:
        """The whole run, on its own thread. Never raises: the finally-like `_finish` owns
        the unwind, and an escaping exception would leave the activity flag set and hang
        every caller watching it."""
        op = function_name()
        try:
            self._open_meter()
            if not self._acquire():
                self._finish("acquisition_failed")
                return
            # After the acquisition, which normally leaves the mirror in already.
            if not self._position_folding_mirror():
                self._finish("failed")
                return
            if not self._start_guiding_and_settle():
                self._finish("aborted" if self._stop.is_set() else "guiding_failed")
                return

            self.state.phase = "focusing"
            self._finish(self._sweep())
        except Exception as ex:  # the thread owns the focuser; it must put it back
            logger.exception(f"{op}: fiber focus failed")
            self.state.last_error = str(ex)
            self._finish("failed")

    def _acquire(self) -> bool:
        """Put the star on the ASSUMED fibre position, with no handover to the guider:
        this run starts guiding itself, once the acquisition is over. Otherwise the same
        fixed acquisition: mastrometry, gradual-by-rate, corrections on, sky phase skipped
        (the fibre only sees light with the folding mirror in, so the spec phase is the one
        that matters).

        Run on this thread rather than through the endpoint, so the call returns exactly
        when the acquisition ends. Started on another thread, its end could only be read
        from `Acquiring`, which is not yet set for a moment after the start. An abort clears
        `Acquiring`, which the acquisition honors, so this call still returns promptly."""
        acquirer = self.unit.acquirer
        if acquirer is None:
            self.state.last_error = "no acquirer"
            return False
        if self._stop.is_set():
            self.state.last_error = "aborted before acquisition"
            return False

        acquisition = acquirer.prepare_acquisition(
            seconds=self.params.acquisition_seconds,
            ra_j2000_hours=self.params.ra_j2000_hours,
            dec_j2000_degs=self.params.dec_j2000_degs,
            gain_absolute=self.params.gain,
            skip_sky=True,
            use_set_limit_frame=True,
            handover_automatically_to_guider=False,
        )
        if isinstance(acquisition, CanonicalResponse):
            self.state.last_error = f"acquisition refused: {acquisition.errors}"
            return False

        acquirer.run_acquisition(acquisition)

        if self._stop.is_set():
            self.state.last_error = "aborted during acquisition"
            return False
        if acquisition.failure is not None:
            self.state.last_error = f"acquisition failed: {acquisition.failure}"
            return False
        return True

    def _position_folding_mirror(self) -> bool:
        """Put the folding mirror in and wait for it, on POSITION rather than on
        `Stage.is_moving`: that is a plain attribute refreshed by the stage's 2-second
        `ontimer` poll, and `move_to_preset` does not set it, so for up to one poll period
        after a move is commanded it still reads False and a wait on it falls straight
        through. `at_preset` is true when the mirror is actually there."""
        stage = self.unit.stage
        if stage is None:
            self.state.last_error = "no stage, so the folding mirror cannot be put in"
            return False

        self.state.phase = "positioning"
        if stage.at_preset(StagePresetPosition.Spec):
            return True

        logger.info("fiber focus: moving the folding mirror to SPEC")
        stage.move_to_preset(StagePresetPosition.Spec)

        deadline = time.monotonic() + STAGE_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if self._stop.is_set():
                self.state.last_error = "aborted while positioning the folding mirror"
                return False
            if stage.at_preset(StagePresetPosition.Spec):
                return True
            time.sleep(POLL_SECONDS)

        self.state.last_error = (
            f"the folding mirror did not reach SPEC within {STAGE_TIMEOUT_SECONDS:g}s (position={stage.position})"
        )
        return False

    def _start_guiding_and_settle(self) -> bool:
        """Start guiding and wait for PHD2's `SettleDone`. True when it settled.

        `start_guiding` is called on this thread rather than through `/start_guiding`,
        which dispatches it on a thread of its own and so throws away its refusal. The
        tracking rule is that endpoint's, restated: the mount is left tracking across a
        later stop only if it was tracking before.

        After this returns True the run never reads PHD2 again.
        """
        self.state.phase = "guiding"
        settle = GuideSettle()
        self.state.guide_settle = settle

        backend = phd2_backend(self.unit)
        if backend is None:
            self.state.last_error = "the guider backend is not PHD2"
            return False

        self.unit.was_tracking_before_guiding = self.unit.mount.is_tracking
        if not self.unit.was_tracking_before_guiding:
            self.unit.mount.start_tracking()

        settle.guide_commanded_utc = isoformat_utc()
        commanded = time.monotonic()
        try:
            response = backend.start_guiding()
        except Exception as ex:  # noqa: BLE001 -- e.g. "cannot guide while settling"
            # `start_guiding` raises `Guiding` before `guide()`, so a `guide()` that raised
            # left the flag up over guiding that never started.
            self.unit.end_activity(UnitActivities.Guiding)
            self.state.last_error = f"PHD2 would not start guiding: {ex}"
            return False
        if response is not None and response.failed:
            self.state.last_error = f"PHD2 would not start guiding: {response.errors}"
            return False

        timeout = self.params.guide_settle_timeout_seconds
        while True:
            # Read under the connector's lock, as its event thread writes it under that lock.
            # `guide()` replaced the record before returning, so whatever is here now --
            # its fresh one, a `Settling` progress or a `SettleDone` -- is this run's.
            with backend.lock:
                record = backend.settle
                done = record is not None and record.done
                status, error = (record.status, record.error) if done else (None, None)
            waited = time.monotonic() - commanded
            if done:
                settle.status, settle.error = status, error
                settle.settled_utc = isoformat_utc()
                settle.waited_seconds = round(waited, 1)
                if status == 0:
                    settle.outcome = "settled"
                    logger.info(f"fiber focus: guiding settled after {waited:.1f}s")
                    return True
                settle.outcome = "failed"
                self.state.last_error = f"PHD2 reported that settling failed (status {status}): {error}"
                return False
            if self._stop.is_set():
                settle.outcome = "aborted"
                self.state.last_error = "aborted while waiting for guiding to settle"
                return False
            if waited >= timeout:
                settle.outcome = "timeout"
                settle.waited_seconds = round(waited, 1)
                self.state.last_error = f"no SettleDone from PHD2 within {timeout:g} s"
                return False
            time.sleep(POLL_SECONDS)

    def _sweep(self) -> str:
        """Expose at every position in turn. Returns the terminal state."""
        for index, commanded in enumerate(self.params.positions):
            if self._stop.is_set():
                return "aborted"
            if not self._await_disk_space():
                return "disk_full"
            self.state.index = index

            measured = self._move_focuser(commanded)
            if measured is None:
                return "aborted"
            position = FiberFocusPosition(index=index, commanded=commanded, measured=measured, settled_utc=isoformat_utc())
            self.positions.append(position)

            for n in range(self.params.frames_per_position):
                if self._stop.is_set():
                    break
                position.frames.append(self._expose(position, n))
                self.state.frames += 1
            self._save_progress()
            logger.info(
                f"position {index + 1}/{self.params.count}: commanded {commanded}, measured {measured} "
                f"({len(position.frames)} frames)"
            )
        return "aborted" if self._stop.is_set() else "completed"

    def _move_focuser(self, target: int) -> int | None:
        """Send the focuser to `target`, wait for it and then `settle_seconds`, and return
        the position it reports. None when an abort came first.

        Waits on `FocuserActivities.Moving`, which `goto_position` raises before it returns
        and the focuser's own timer lowers on arrival -- the wait `autofocusing.py` uses.
        """
        focuser = self.unit.focuser
        response = focuser.goto_position(target)
        if response is not None and response.failed:
            raise FiberFocusError(f"the focuser refused {target}: {response.errors}")

        deadline = time.monotonic() + FOCUSER_TIMEOUT_SECONDS
        while focuser.is_active(FocuserActivities.Moving):
            if self._stop.is_set():
                return None
            if time.monotonic() >= deadline:
                raise FiberFocusError(
                    f"the focuser did not reach {target} within {FOCUSER_TIMEOUT_SECONDS:g}s (at {focuser.position})"
                )
            time.sleep(POLL_SECONDS)

        if self._stop.wait(self.params.settle_seconds):
            return None
        return int(focuser.position)

    def _expose(self, position: FiberFocusPosition, n: int) -> FiberFocusFrame:
        """One ThorCam frame at `position`, written and handed to the mover."""
        name = frame_name(position.index, position.measured, n)
        started = isoformat_utc()
        pointing = self._pointing()
        data = self._meter.expose()  # type: ignore[union-attr]
        ended = isoformat_utc()
        self._write_fits(name, data, self._cards(position, n, started, ended, pointing))
        return FiberFocusFrame(name=name, started_utc=started, ended_utc=ended)

    def _pointing(self) -> dict[str, float | None]:
        """Where the mount is, in one `mount.status()`: separate reads would sample
        different instants. Alt/az are absent: they are not on `MountStatus` and would cost
        a PWI4 round trip, and are derivable afterwards from RA/Dec and DATE-OBS."""
        try:
            status = self.unit.mount.status()  # type: ignore[union-attr]
            return {
                "ra_j2000_hours": status.ra_j2000_hours,
                "dec_j2000_degs": status.dec_j2000_degs,
                "ha_hours": status.ha_hours,
                "lmst_hours": status.lmst_hours,
            }
        except Exception:  # noqa: BLE001 -- metadata must never fail an exposure
            logger.warning("could not read the mount's pointing for the frame header")
            return {}

    def _cards(
        self, position: FiberFocusPosition, n: int, started: str, ended: str, pointing: dict[str, Any]
    ) -> list[tuple[str, Any, str]]:
        """The header for one ThorCam frame: (keyword, value, comment).

        Deliberately NO WCS. The ThorCam sees only the light emerging from the fibre and has
        no field, so `CRVAL`/`CRPIX`/`CTYPE` would assert that these pixels map to sky.
        """
        meter = self._meter
        date, seq = self._run_labels()
        ra_hours = pointing.get("ra_j2000_hours")
        return [
            ("DATE-OBS", started, "UTC at the start of this exposure"),
            ("DATE-END", ended, "UTC at the end of this exposure"),
            ("EXPTIME", self.params.flux_exposure_us / 1e6, "exposure time in seconds"),
            ("INSTRUME", meter.model if meter else None, "the flux meter"),
            ("CAMSN", meter.serial_number if meter else None, "flux meter serial number"),
            ("GAIN", self.params.flux_gain, "flux meter gain"),
            ("BLKLEVEL", self.params.flux_black_level, "black level commanded on the camera"),
            ("SATURATE", meter.saturation_level if meter else None, "camera full scale ADU"),
            ("TELESCOP", self.unit.hostname, "the unit"),
            ("CREATOR", "MAST acquire_and_focus_fiber", "what wrote this frame"),
            ("RUNDATE", date, "observing night (turns at 12:00 UTC)"),
            ("RUNSEQ", seq, "fiber focus run sequence"),
            ("POSIDX", position.index + 1, "focuser position index, from 1"),
            ("FRAMEIDX", n, "frame within the position, from 0"),
            ("FOCUSPOS", position.measured, "focuser position, measured after settling"),
            ("FOCUSCMD", position.commanded, "focuser position commanded"),
            ("FOCSETTL", self.params.settle_seconds, "wait after the focuser arrived, seconds"),
            ("RA", ra_hours * 15.0 if ra_hours is not None else None, "mount J2000 RA, degrees"),
            ("DEC", pointing.get("dec_j2000_degs"), "mount J2000 Dec, degrees"),
            ("EQUINOX", 2000.0, "equinox of RA/DEC"),
            ("RADESYS", "ICRS", "reference frame of RA/DEC"),
            ("HA", pointing.get("ha_hours"), "hour angle, hours"),
            ("LMST", pointing.get("lmst_hours"), "local mean sidereal time, hours"),
            (
                "OBJECT",
                f"{self.params.ra_j2000_hours:.6f}h {self.params.dec_j2000_degs:+.6f}d",
                "requested target (J2000)",
            ),
        ]

    # ------------------------------------------------------------------- plumbing --

    def _open_meter(self) -> None:
        self._meter = self._injected_meter if self._injected_meter is not None else RemoteFluxMeter.at_spec_host()
        try:
            self._meter.configure(
                exposure_us=self.params.flux_exposure_us,
                gain=self.params.flux_gain,
                black_level=self.params.flux_black_level,
            )
        except FluxMeterError:
            self._meter.close()
            self._meter = None
            raise

    def _ram_disk_free_bytes(self) -> int | None:
        """Free space where the frames are being written, or None if it cannot be read."""
        folder = self.state.folder or (filer.ram.root if filer.ram else None)
        if folder is None:
            return None
        try:
            return shutil.disk_usage(folder).free
        except OSError as ex:
            logger.error(f"could not read free space on '{folder}': {ex}")
            return None

    def _await_disk_space(self) -> bool:
        """True when there is room for the next position; False when the run should stop.
        Unreadable free space returns True."""
        free = self._ram_disk_free_bytes()
        if free is None or free >= RAM_DISK_MIN_FREE_BYTES:
            return True
        filer.flush(timeout=RAM_DISK_DRAIN_TIMEOUT_SECONDS)
        free = self._ram_disk_free_bytes()
        if free is not None and free < RAM_DISK_MIN_FREE_BYTES:
            self.state.last_error = (
                f"ram disk has {free / 1024**3:.1f} GB free and the mover is not draining it; "
                f"the shared area is probably unreachable"
            )
            logger.error(self.state.last_error)
            return False
        return True

    def _run_labels(self) -> tuple[str | None, str | None]:
        """(observing night, sequence) from the run folder, `<...>/<date>/FiberFocus/<seq>`."""
        if self.state.folder is None:
            return None, None
        parts = Path(self.state.folder).parts
        if len(parts) >= 3 and parts[-2] == "FiberFocus":
            return parts[-3], parts[-1]
        return None, None

    def _write_fits(self, file_name: str, data: np.ndarray, cards: list[tuple[str, Any, str]]) -> None:
        if self.state.folder is None:
            raise FiberFocusError("no folder")
        path = os.path.join(self.state.folder, file_name)
        header = fits.Header()
        for keyword, value, comment in cards:
            # Unknown values are omitted rather than written empty.
            if value is not None:
                header[keyword] = (value, comment)
        with MoveGuardian().protect(path):
            fits.PrimaryHDU(data=np.asarray(data), header=header).writeto(path, overwrite=True)
        filer.move_ram_to_shared(path)

    def _return_focuser(self) -> None:
        """Send the focuser to `known_as_good_position`, read now, and wait for it.

        Waits out a focuser abort first: `Unit.abort` aborts every component right after
        asking this run to stop, and a `focuser_stop` landing after our goto would leave
        the focuser short of where it was sent. Never raises.
        """
        focuser = self.unit.focuser
        if focuser is None:
            return
        self.state.phase = "returning"
        try:
            target = focuser.known_as_good_position
            if target is None:
                logger.error("fiber focus: known_as_good_position is None; the focuser is left where it is")
                return
            self.state.final_focuser_commanded = target

            deadline = time.monotonic() + FOCUSER_TIMEOUT_SECONDS
            while focuser.is_active(FocuserActivities.Aborting) and time.monotonic() < deadline:
                time.sleep(POLL_SECONDS)
            response = focuser.goto_position(target)
            if response is not None and response.failed:
                logger.error(f"fiber focus: the focuser refused known_as_good_position {target}: {response.errors}")
                return
            while focuser.is_active(FocuserActivities.Moving) and time.monotonic() < deadline:
                time.sleep(POLL_SECONDS)
            self.state.final_focuser_measured = int(focuser.position)
            if not focuser.close_enough(target):
                logger.error(f"fiber focus: the focuser is at {focuser.position}, not back at {target}")
        except Exception as ex:  # noqa: BLE001 -- the result and the flag must still be released
            logger.error(f"fiber focus: could not return the focuser: {ex}")

    def _finish(self, terminal: str) -> None:
        """Put the focuser back, write the result, and release the unit. Guiding is left
        running."""
        self._return_focuser()

        self.state.terminal_state = terminal
        self.state.ended_at = isoformat_utc()
        try:
            self._write_result(self._result_document(final=True), final=True)
        except Exception:  # noqa: BLE001
            logger.exception("could not write result.json")

        if self._meter is not None:
            self._meter.close()
            self._meter = None

        self.state.active = False
        self.state.phase = "idle"
        self.unit.end_activity(UnitActivities.FluxMetering)

        if not filer.flush(timeout=300.0):
            logger.error("products were still in flight when the run ended")
        logger.info(f"fiber focus ended: {terminal}, {len(self.positions)} of {self.params.count} positions")

    def _result_document(self, final: bool) -> dict[str, Any]:
        """The status model plus what only the run itself knows."""
        focuser = self.unit.focuser
        document = {
            **self.status().model_dump(),
            "params": asdict(self.params),
            "commanded_positions": self.params.positions,
            "flux_exposure_us": self.params.flux_exposure_us,
            "flux_meter": self._meter.description if self._meter else None,
            "saturation_level": self._meter.saturation_level if self._meter else None,
            "hostname": self.unit.hostname,
            "focuser_limits": [focuser.lower_limit, focuser.upper_limit] if focuser is not None else None,
        }
        if final:
            # `_finish` runs inside the worker thread, so the live status still says active.
            document["active"] = False
            document["phase"] = "idle"
        return document

    def _save_progress(self) -> None:
        """Rewrite result.json after a position. Never fails the run."""
        try:
            self._write_result(self._result_document(final=False), final=False)
        except Exception as ex:  # noqa: BLE001
            logger.warning(f"fiber focus: could not update result.json: {ex}")

    def _write_result(self, result: dict[str, Any], final: bool) -> None:
        """result.json in the run folder, replaced atomically, moved to the share only when
        `final`. Written to a temporary file and renamed over the old one, so a run that
        dies mid-write leaves the previous document rather than half of this one."""
        if self.state.folder is None:
            return
        path = os.path.join(self.state.folder, "result.json")
        with MoveGuardian().protect(path):
            partial = path + ".partial"
            with open(partial, "w") as fp:
                json.dump(result, fp, indent=2, default=str)
            os.replace(partial, path)
        if final:
            filer.move_ram_to_shared(path)
