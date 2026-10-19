"""One run of `acquire_and_save_spirals`.

A SIBLING of `session.py`'s `acquire_and_find_max_flux`, not a replacement: that module is
untouched and its endpoint behaves exactly as it did. This one shares nothing with it but
read-only helpers (`isoformat_utc`), the status models in `common`, the
`UnitActivities.FluxMetering` flag (so the two refuse to run together) and the hardware.
Its products go to the same `<night>/FluxMetering/<NNNN>` folders as that run's. A spirals
run has no flux curve or arg-max in its `result.json`, so `flux_metering_runs` lists it and
`spiral_correlate_steps` cannot use it.

Acquire a star, put the folding mirror in, and walk a fixed number of spiral rings around
the acquired position, taking imager ("step") frames and ThorCam ("flux") frames at every
cell. That is all: nothing here measures a flux, picks a best cell, solves a frame or
correlates two of them. The products are the frames and `result.json`, and every analysis
is done afterwards, off the unit.

The decisions that are easiest to undo by accident, and why they are what they are:

* **The operator says how many rings.** The walk covers the origin cell and `rings` rings
  around it -- `(2 * rings + 1) ** 2` cells -- and stops. There is no stopping rule and
  no radius cap: the extent of the search is `rings` times the step, chosen up front.
* **Ring membership is READ, not assumed.** PWI4 owns the traversal order, so the ring is
  derived from the offsets it reports -- `max(|x|, |y|)`. The cell count above is the
  backstop for when PWI4 reports no offset, not the primary rule.
* **Frames are named by ring and step within the ring**: `step-R-SSS-MM.fits` and
  `flux-R-SSS-MM.fits`, where `R` is the ring, `SSS` the step within it (from 001) and
  `MM` the exposure within the step. Both numbers are worked out from the step counter
  on the square spiral, not read from PWI4, so a name is defined even when PWI4 reports
  no offset. That is also why `rings` stops at 9: `R` is one digit.
* **The step sizes are arcsec ON THE SKY.** PWI4 steps the RA coordinate, and a step of
  the RA coordinate covers `cos(dec)` of that on the sky (measured, MAST_unit#136: 10"
  commanded at dec +41 moved the star 7.44"). So the walk commands
  `x_step_arcsec / cos(dec)` along RA, with the declination read from the mount once the
  star has been acquired, and the cells come out square on the sky at any declination.
* **Every exposure time is set on its own.** `step_seconds` for the imager's step frames,
  `flux_seconds` for the ThorCam, `acquisition_seconds` for the acquisition. A step's two
  frames are still started together, and each records its own start and end, so how much
  the two windows overlap is checkable afterwards.
* **`result.json` is rewritten after every step**, on the ram disk, and moved to the share
  once at the end. A run that dies part-way leaves a document describing every step it
  finished, which the startup sweep then relocates with the frames.
* Nothing here writes configuration.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
from astropy.io import fits

from common.activities import UnitActivities
from common.canonical import CanonicalResponse
from common.filer import Filer, MoveGuardian
from common.mast_logging import get_logger
from common.models.statuses import (
    FluxMeteringExposure,
    FluxMeteringStatus,
    FluxMeteringStep,
    ImagerSettings,
)
from common.paths import PathMaker
from common.utils import function_name
from flux_metering.flux_meter import FluxMeter, FluxMeterError
from flux_metering.remote import PREFLIGHT_TIMEOUT_S, RemoteFluxMeter
from flux_metering.session import isoformat_utc
from mount import SettleMode
from stage import StagePresetPosition

if TYPE_CHECKING:
    from unit import Unit

logger = get_logger(__name__)
filer = Filer(logger)

#: How long to wait for the folding mirror to reach SPEC. A Sky->Spec traverse was measured
#: at 21-22 seconds on mast02 (2026-09-02), so this is a generous ceiling on a move that
#: normally takes half a minute -- not a tolerance.
STAGE_TIMEOUT_SECONDS = 120.0

#: Below this much free space on the ram disk, a step waits for the mover to catch up before
#: exposing again.
#:
#: Every frame is handed to `move_ram_to_shared` the moment it is written, and that is
#: asynchronous, so in normal running the disk holds only the in-flight backlog -- roughly
#: 10 MB/s of demand against a share that can take far more. This matters when the share
#: STALLS: the backlog then grows instead of draining, and with ~94 MB per imager frame a run
#: would fill the disk mid-exposure and fail somewhere confusing, part-written.
RAM_DISK_MIN_FREE_BYTES = 3 * 1024**3

#: How long to let the mover drain before giving up on a run. Long enough to ride out a brief
#: share hiccup, short enough not to sit through an outage with the mount parked on a cell.
RAM_DISK_DRAIN_TIMEOUT_SECONDS = 120.0


class SpiralsError(Exception):
    """A spirals run could not be set up, or could not do something it needs to do."""


@dataclass
class SpiralsParams:
    """What the operator chose. Carried whole so the result can echo it back."""

    #: Imager exposure for every step frame.
    step_seconds: float = 5.0
    #: Imager exposure for the acquisition's plate solves. Independent of `step_seconds`.
    acquisition_seconds: float = 5.0
    #: ThorCam exposure, for every flux frame. Independent of `step_seconds`.
    flux_seconds: float = 5.0
    ra_j2000_hours: float | None = None
    dec_j2000_degs: float | None = None
    gain: int | None = None
    #: Spiral step along RA, ON THE SKY (great-circle arcsec). PWI4 steps the RA COORDINATE,
    #: so the walk commands `x_step_arcsec / cos(dec)` -- see `_commanded_ra_step`.
    x_step_arcsec: float = 0.5
    #: Spiral step along Dec. Dec arcsec are already arcsec on the sky.
    y_step_arcsec: float = 0.5
    #: Rings walked around the origin cell. The run takes `total_cells` steps and stops.
    rings: int = 2
    flux_gain: int = 0
    flux_black_level: int = 3
    number_of_frames: int = 1
    skip_acquisition: bool = False

    @property
    def flux_exposure_us(self) -> int:
        """The ThorCam is commanded in microseconds."""
        return round(self.flux_seconds * 1_000_000)

    @property
    def total_cells(self) -> int:
        """Cells in a square spiral of `rings` rings: the origin, plus 8n cells on ring n."""
        return (2 * self.rings + 1) ** 2


#: Above this |dec| the RA step is refused rather than commanded: the coordinate step
#: needed to cover `x_step_arcsec` on the sky is 1/cos(dec), ten times it at 84.3 deg and
#: growing without bound towards the pole.
MAX_RA_STEP_FACTOR = 10.0

#: The ring is one digit of the frame names.
MAX_RINGS = 9


def ring_position(index: int) -> tuple[int, int]:
    """(ring, step within the ring) of step `index` of a square spiral.

    The ring counts from 0 and the step within it from 1. Ring 0 is the origin alone, so
    its one step is 1; ring n holds 8n cells and starts at step (2n - 1)**2.
    """
    ring = (math.isqrt(index) + 1) // 2
    first = (2 * ring - 1) ** 2 if ring else 0
    return ring, index - first + 1


def frame_names(index: int, exposure: int) -> tuple[str, str]:
    """(imager frame, flux frame) names for one exposure of step `index`."""
    ring, within = ring_position(index)
    stem = f"{ring:1d}-{within:03d}-{exposure:02d}.fits"
    return f"step-{stem}", f"flux-{stem}"


class SpiralsSession:
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
        self.state = FluxMeteringStatus()
        self.params = SpiralsParams()
        self.steps: list[FluxMeteringStep] = []
        #: The RA step actually commanded to PWI4, in RA-coordinate arcsec, and the
        #: declination it was sized for. Set at the start of the walk.
        self._ra_step_commanded: float | None = None
        self._step_dec: float | None = None

    # ------------------------------------------------------------------- control --

    @property
    def is_active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def has_run(self) -> bool:
        """Whether a run has happened since the service started."""
        return self._thread is not None

    def require_can_start(self) -> str | None:
        """Why a run cannot start, or None.

        A guard rather than an `assert`: asserts are stripped under `python -O`, and the
        response envelope renders `AssertionError` as an anonymous error carrying nothing
        the caller can act on.

        Everything here is checked BEFORE the thread is dispatched -- a route that
        dispatches and answers Ok has already spent the request.
        """
        if self.is_active:
            return "a spirals run is already in progress"
        if self.unit.mount is None or self.unit.imager is None:
            return "the mount and the imager must both be present"
        busy = (
            UnitActivities.Acquiring
            | UnitActivities.Guiding
            | UnitActivities.Autofocusing
            | UnitActivities.StartingUp
            | UnitActivities.ShuttingDown
            # Both runs raise this flag, so this also refuses while an
            # `acquire_and_find_max_flux` run owns the mount and the ThorCam.
            | UnitActivities.FluxMetering
        )
        if self.unit.activities & busy:
            return f"the unit is busy ({self.unit.activities_verbal})"
        if self.unit.acquirer is None:
            return "no acquirer, so the star cannot be put on the fibre"
        # The fibre sees light only with the folding mirror in, so a run without a stage
        # would record darkness on every flux frame.
        if self.unit.stage is None:
            return "no stage, so the folding mirror cannot be put in"

        # Checked here rather than discovered mid-run: a run that starts with the ram disk
        # already near full has nowhere to put its first frames, and the mover cannot help
        # if what filled it was somebody else's stranded products.
        free = self._ram_disk_free_bytes()
        if free is not None and free < RAM_DISK_MIN_FREE_BYTES:
            return (
                f"only {free / 1024**3:.1f} GB free where the frames are written; "
                f"at least {RAM_DISK_MIN_FREE_BYTES / 1024**3:.0f} GB is wanted before starting"
            )

        # Last, being the one check that goes over the network. The camera is on the spec
        # host, so a service that is down would otherwise surface only after
        # the folding mirror had moved, as a run that failed rather than one never started.
        if self._injected_meter is None:
            try:
                RemoteFluxMeter.at_spec_host(timeout_s=PREFLIGHT_TIMEOUT_S)
            except FluxMeterError as ex:
                return f"the flux meter is not available: {ex}"
        return None

    def start(self, params: SpiralsParams):
        """Validate, claim the unit, and dispatch. Returns a refusal or the initial state."""
        with self._lock:
            refusal = self.require_can_start()
            if refusal is not None:
                return refusal

            self.params = params
            self.steps = []
            self._ra_step_commanded = None
            self._step_dec = None
            self._stop.clear()
            folder = PathMaker().make_flux_metering_folder()
            self.state = FluxMeteringStatus(
                active=True,
                phase="acquiring",
                folder=folder,
                started_at=isoformat_utc(),
            )
            # Raised here rather than in the thread: the endpoint declares this flag as its
            # completion signal, so a caller answered Ok must find it already set.
            self.unit.start_activity(UnitActivities.FluxMetering)
            self._thread = threading.Thread(
                name="acquire_and_save_spirals",
                target=self.do_acquire_and_save_spirals,
                daemon=True,
            )
            self._thread.start()
            logger.info(f"spirals started: {params.rings} rings ({params.total_cells} cells), products under '{folder}'")
            return self.status()

    def abort(self) -> None:
        """Ask the run to stop. It finishes the exposure it is inside, then unwinds."""
        if self.is_active:
            logger.info("spirals: abort requested")
            self._stop.set()

    def status(self) -> FluxMeteringStatus:
        """The typed model, not a dict.

        The endpoint contract's one load-bearing exception is that a status returns its bare
        model -- an envelope nested inside the payload would break every consumer silently.
        """
        self.state.active = self.is_active
        self.state.steps = list(self.steps)
        return self.state

    # -------------------------------------------------------------------- the run --

    def do_acquire_and_save_spirals(self) -> None:
        """The whole run, on its own thread. Never raises: the finally clause owns the
        unwind, and an escaping exception would leave the activity flag set and hang every
        caller watching it."""
        op = function_name()
        try:
            self._open_meter()
            if self.params.skip_acquisition:
                # Engineering only. The spiral and the products can then be exercised with
                # the mount wherever it happens to point -- in daylight, in a closed
                # enclosure, with no star and no solve. `skip_acquisition` is echoed into
                # result.json with the rest of the parameters, so such a run can never be
                # mistaken for a real one after the fact.
                logger.warning(
                    "spirals: acquisition SKIPPED by request -- this run is a shakedown, "
                    "and its spiral is not centred on the assumed fibre position"
                )
            elif not self._acquire():
                self._finish("acquisition_failed")
                return

            # After the acquisition, not before: it takes the mirror out to solve and puts it
            # back, so positioning first cost two traverses (MAST_unit#312). Normally the
            # mirror is already in and this returns at once.
            if not self._position_folding_mirror():
                self._finish("failed")
                return

            self.state.phase = "spiral"
            self._finish(self._walk_spiral())
        except Exception as ex:  # the thread owns the mount; it must land it safely
            logger.exception(f"{op}: spirals failed")
            self.state.last_error = str(ex)
            self._finish("failed")

    def _position_folding_mirror(self) -> bool:
        """Put the folding mirror in, and WAIT for it to arrive. True when it is there.

        On the worker thread rather than in the endpoint, because it takes 21-22 seconds
        on this hardware and the route is documented to answer at once. `state.phase` says
        what is happening, so the wait is visible in `save_spirals_status` rather than
        merely long.

        Waits on POSITION, not on `Stage.is_moving`. `is_moving` is a plain attribute
        refreshed by the stage's 2-second `ontimer` poll, and `move_to_preset` does not set
        it -- so for up to one poll period after a move is commanded it still reads False,
        and a `while stage.is_moving` loop falls straight through. On the night of
        2026-09-02 that raced every time: `_await_stage` returned 6 ms into a 133,000-count
        move, and the acquisition went on to expose with the mirror a quarter of the way
        across. `at_preset` is true when the mirror is actually there, whenever the poll
        last ran.
        """
        stage = self.unit.stage
        if stage is None:  # `require_can_start` refuses this; here so the wait cannot lie
            self.state.last_error = "no stage, so the folding mirror cannot be put in"
            return False

        self.state.phase = "positioning"
        if stage.at_preset(StagePresetPosition.Spec):
            return True

        logger.info("spirals: moving the folding mirror to SPEC")
        stage.move_to_preset(StagePresetPosition.Spec)

        deadline = time.monotonic() + STAGE_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if self._stop.is_set():
                self.state.last_error = "aborted while positioning the folding mirror"
                return False
            if stage.at_preset(StagePresetPosition.Spec):
                logger.info(f"spirals: folding mirror at SPEC (position={stage.position})")
                return True
            time.sleep(0.2)

        self.state.last_error = (
            f"the folding mirror did not reach SPEC within {STAGE_TIMEOUT_SECONDS:g}s (position={stage.position})"
        )
        return False

    def _acquire(self) -> bool:
        """Put the star on the ASSUMED fibre position, and wait for it to get there.

        Everything about the acquisition is fixed rather than operator-facing: mastrometry,
        gradual-by-rate, corrections on, sky phase skipped (the fibre only sees light with
        the folding mirror in, so the spec phase is the one that matters), and no handover
        to the guider. Its exposure is `acquisition_seconds`.

        Run on this thread rather than through the endpoint, so the call returns exactly
        when the acquisition ends. Started on another thread, its end could only be read
        from `Acquiring`, which is not yet set for a moment after the start -- and a spiral
        once walked right through that moment (MAST_unit#316). An abort clears `Acquiring`,
        which the acquisition honors, so this call still returns promptly.
        """
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
        """True when there is room for the next step; False when the run should stop.

        Unreadable free space returns True: a run must not be stopped by the inability to
        ask a question, only by a real answer.
        """
        free = self._ram_disk_free_bytes()
        if free is None or free >= RAM_DISK_MIN_FREE_BYTES:
            return True

        logger.warning(
            f"ram disk down to {free / 1024**3:.1f} GB; waiting up to "
            f"{RAM_DISK_DRAIN_TIMEOUT_SECONDS:g}s for the mover to drain"
        )
        filer.flush(timeout=RAM_DISK_DRAIN_TIMEOUT_SECONDS)

        free = self._ram_disk_free_bytes()
        if free is not None and free < RAM_DISK_MIN_FREE_BYTES:
            self.state.last_error = (
                f"ram disk has {free / 1024**3:.1f} GB free and the mover is not draining it; "
                f"the shared area is probably unreachable. Stopping before a part-written frame."
            )
            logger.error(self.state.last_error)
            return False
        return True

    def _walk_spiral(self) -> str:
        """Walk `rings` rings around the origin, exposing at every cell, then stop.

        Returns the terminal state, which the result reports: `completed` when every ring
        was walked, otherwise `aborted` or `disk_full`.

        Two conditions end the walk, whichever comes first. PWI4 reporting a cell beyond the
        last ring is the primary one, because PWI4 owns the traversal. The cell count is the
        backstop for a PWI4 that reports no offset at all, and it is checked BEFORE moving,
        so on a square spiral the mount never steps out onto a ring that will not be exposed.
        """
        mount = self.unit.mount
        if mount is None or mount.pw is None:
            raise SpiralsError("no mount")

        self._step_dec, self._ra_step_commanded = self._commanded_ra_step()
        logger.info(
            f'spirals: RA step {self.params.x_step_arcsec:g}" on the sky at dec {self._step_dec:+.3f} -> '
            f'{self._ra_step_commanded:.4f}" of RA commanded; Dec step {self.params.y_step_arcsec:g}"'
        )
        mount.pw.mount_spiral_offset_new(*self._commanded_steps())

        index = 0
        while True:
            if self._stop.is_set():
                return "aborted"
            if not self._await_disk_space():
                return "disk_full"

            cell, ring, offset = self._read_spiral_offset()
            if ring is not None and ring > self.params.rings:
                return "completed"

            step = self._expose_step(index, cell, ring, offset)
            if step is None:  # aborted before any of its frames was saved
                return "aborted"
            self.steps.append(step)
            self._save_progress()

            if index + 1 >= self.params.total_cells:
                return "completed"

            mount.pw.mount_spiral_offset_next()
            mount.wait_until_settled(SettleMode.OFFSET_STEP)
            index += 1

    def _commanded_ra_step(self) -> tuple[float, float]:
        """(declination, RA step to command) so the RA step covers `x_step_arcsec` on the sky.

        The declination is the mount's own, read once the star is acquired -- where the
        spiral is actually walked -- and the requested target's only when the mount cannot
        be read. One value for the whole walk: a spiral spans arcseconds, over which cos(dec)
        does not change.

        Raises rather than guessing: without a declination the RA step cannot be sized, and
        near the pole the coordinate step it needs grows without bound.
        """
        dec = self._mount_dec()
        if dec is None:
            dec = self.params.dec_j2000_degs
        if dec is None:
            raise SpiralsError("neither the mount nor the request gives a declination to size the RA step for")
        cos_dec = math.cos(math.radians(dec))
        if cos_dec < 1.0 / MAX_RA_STEP_FACTOR:
            raise SpiralsError(
                f'at dec {dec:+.2f} an RA step of {self.params.x_step_arcsec:g}" on the sky needs '
                f'{self.params.x_step_arcsec / max(cos_dec, 1e-9):.1f}" of RA; refusing beyond '
                f"{MAX_RA_STEP_FACTOR:g}x the requested step"
            )
        return dec, self.params.x_step_arcsec / cos_dec

    def _commanded_steps(self) -> tuple[float, float]:
        """(x, y) as PWI4 is to be given them: the RA step sized for the declination once the
        walk has worked it out, the requested one before that (when only the counter is being
        zeroed, and no step is taken)."""
        x = self._ra_step_commanded if self._ra_step_commanded is not None else self.params.x_step_arcsec
        return x, self.params.y_step_arcsec

    def _mount_dec(self) -> float | None:
        try:
            dec = self.unit.mount.status().dec_j2000_degs  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            return None
        return float(dec) if dec is not None else None

    def _read_spiral_offset(self):
        """(cell, ring, offset_arcsec) as PWI4 reports them, or Nones.

        PWI4 owns the traversal, so the ring is derived from what it reports rather than
        assumed from a cell ordering of our own. `spiral_offset` is absent on PWI4 older
        than 4.0.11b8, and a step with no cell is recorded rather than guessed at.
        """
        try:
            spiral = self.unit.pw.status().mount.spiral_offset  # type: ignore[union-attr]
        except Exception as ex:  # noqa: BLE001 -- a telemetry hiccup must not end the run
            logger.error(f"could not read the spiral offset: {ex}")
            return None, None, None
        if spiral is None or spiral.x is None or spiral.y is None:
            return None, None, None
        cell = (int(spiral.x), int(spiral.y))
        # On the sky: the RA step was sized so that a cell IS `x_step_arcsec` there.
        offset = (cell[0] * self.params.x_step_arcsec, cell[1] * self.params.y_step_arcsec)
        return cell, max(abs(cell[0]), abs(cell[1])), offset

    def _pixel_scale(self) -> float | None:
        """The configured plate scale, or None when it is unset.

        Recorded beside the run's products so an analysis months later converts
        arcseconds with the scale THIS run was configured with, not with whatever the
        configuration says by then.
        """
        conf = self.unit.unit_conf
        scale = conf.imager.pixel_scale_at_bin1 if conf is not None else 0.0
        return scale if scale and scale > 0.0 else None

    # ------------------------------------------------------------------ exposures --

    def do_flux_exposure(self, captured: dict[str, Any]) -> None:
        """Take one ThorCam frame on this thread, recording the outcome in `captured`.

        A named target rather than a closure so the dispatch site says what is running
        without the reader opening it -- invariant 9, and the reason `run_acquisition`
        was worth a check.

        The failure is recorded, never raised: a bare thread that raises prints to stderr
        and vanishes, and the parent would then find no frame and carry on as though one
        had been written.
        """
        try:
            captured["flux_started"] = isoformat_utc()
            # Sampled here, beside the timestamps, rather than read when the frame is
            # written. The write follows by milliseconds, so reading it there would
            # usually be right -- but right by luck about timing, and wrong the moment a
            # write is retried or deferred.
            captured["pointing"] = self._pointing()
            captured["flux_frame"] = self._meter.expose()  # type: ignore[union-attr]
            captured["flux_ended"] = isoformat_utc()
        except Exception as ex:  # noqa: BLE001 -- reported through `captured`, not swallowed
            captured["flux_error"] = ex

    def _pointing(self) -> dict[str, float | None]:
        """Where the mount is, in one read.

        One `mount.status()` for all four values: separate reads would sample four
        different instants, and during a spiral step the mount is being offset between
        them. Alt/az are deliberately absent -- they are not on `MountStatus` and would
        cost a PWI4 round trip, and both they and the airmass are derivable afterwards
        from RA/Dec, DATE-OBS and the site coordinates.
        """
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

    def _expose_pair(self, imager_name: str, flux_name: str, step: tuple[int, Any, int, Any]) -> FluxMeteringExposure | None:
        """One imager frame and one ThorCam frame, started together, each with its own
        exposure time.

        Started together so that, with equal exposure times, they cover the same window --
        and each records its own start and end, so the overlap is verifiable afterwards
        rather than assumed. The imager path goes through PHD2 and does not necessarily
        begin the instant it is asked.

        `step` is `(index, cell, ring, offset_arcsec)` and is passed DOWN from
        `_expose_step` rather than read off `self.state`, which does not yet describe this
        step: the state is advanced after the exposures complete, so it still holds the
        previous one here.

        None when an abort cut the imager exposure short: the imager then returns without
        a frame, and the pair is discarded -- its flux frame is not written -- rather than
        recorded under a step frame that does not exist.
        """
        captured: dict[str, Any] = {}
        flux_thread = threading.Thread(name="flux-exposure", target=self.do_flux_exposure, args=(captured,))
        flux_thread.start()

        imager_started = isoformat_utc()
        try:
            saved = self._expose_imager(imager_name, cards=self._step_cards(flux_name, step))
        finally:
            # Joined in `finally` so a failed imager exposure cannot leave the ThorCam
            # thread writing into `captured` while the next step is already using it.
            flux_thread.join()
        imager_ended = isoformat_utc()

        if not saved:
            if self._stop.is_set():
                logger.info(f"spirals: '{imager_name}' was cut short by the abort; pair discarded")
                return None
            raise SpiralsError(f"the imager returned without saving '{imager_name}'")
        if "flux_error" in captured:
            raise SpiralsError(f"the flux exposure failed: {captured['flux_error']}")

        self._write_fits(flux_name, captured["flux_frame"], cards=self._flux_cards(captured, imager_name, step))

        return FluxMeteringExposure(
            imager_frame=imager_name,
            flux_frame=flux_name,
            imager_started_utc=imager_started,
            imager_ended_utc=imager_ended,
            flux_started_utc=captured["flux_started"],
            flux_ended_utc=captured["flux_ended"],
        )

    def _expose_step(self, index: int, cell, ring, offset) -> FluxMeteringStep | None:
        """One step: `number_of_frames` exposure pairs at one pointing, all of them kept.

        An abort is honoured between pairs, not only between steps: `Unit.abort` cuts the
        imager exposure in flight, and without this check the step's remaining pairs would
        then start fresh exposures after the abort had already answered. The pairs saved
        before the abort are kept; None when there were none.
        """
        named_ring, _within = ring_position(index)
        if ring is not None and ring != named_ring:
            # The names assume PWI4 walks the square spiral this module expects. If it does
            # not, the header's RING (PWI4's) is the one to trust, and this says so.
            logger.warning(
                f"step {index}: PWI4 reports ring {ring} at cell {cell}, but the frame names "
                f"say ring {named_ring}; the headers carry PWI4's"
            )
        exposures: list[FluxMeteringExposure] = []
        for n in range(self.params.number_of_frames):
            if self._stop.is_set():
                break
            exposure = self._expose_pair(
                *frame_names(index, n),
                # From the arguments, not self.state: the state still describes the
                # PREVIOUS step here, being advanced only once these exposures are done.
                step=(index, cell, ring, offset),
            )
            if exposure is None:
                break
            exposures.append(exposure)
        if not exposures:
            return None

        self.state.index = index
        self.state.cell = cell
        self.state.ring = ring
        self.state.frames += len(exposures)

        logger.info(f"step {index + 1}/{self.params.total_cells}: cell={cell} ring={ring} ({len(exposures)} frame pairs)")
        return FluxMeteringStep(
            index=index,
            cell=cell,
            ring=ring,
            offset_arcsec=offset,
            exposures=exposures,
            imager_started_utc=exposures[0].imager_started_utc,
            imager_ended_utc=exposures[-1].imager_ended_utc,
            flux_started_utc=exposures[0].flux_started_utc,
            flux_ended_utc=exposures[-1].flux_ended_utc,
        )

    # ------------------------------------------------------------------- plumbing --

    def _open_meter(self) -> None:
        if self._injected_meter is not None:
            self._meter = self._injected_meter
        else:
            self._meter = RemoteFluxMeter.at_spec_host()
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

    def _expose_imager(self, file_name: str, cards: list[tuple[str, Any, str]]) -> bool:
        """Expose full-frame at bin 1, save, stamp `cards` into the header, hand to the mover.

        Full frame and bin 1 so whatever analysis is done afterwards has full detector
        sampling to work with. Nothing is read back: the unit does not look at the pixels.

        False when the imager returned without a frame -- what an aborted exposure does.
        """
        imager, conf = self.unit.imager, self.unit.unit_conf
        if imager is None or conf is None or self.state.folder is None:
            raise SpiralsError("no imager, configuration or folder")

        path = os.path.join(self.state.folder, file_name)
        with MoveGuardian().protect(path):
            imager.latest_settings = ImagerSettings(
                seconds=self.params.step_seconds,
                save=True,
                image_path=path,
                binning=1,
                roi=imager.full_frame,
                # `is not None`, not `or`: 0 is a valid gain and must not become the default.
                gain=self.params.gain if self.params.gain is not None else conf.acquisition.gain,
            )
            response = imager.start_exposure(imager.latest_settings)
            if response is not None and response.failed:
                raise SpiralsError(f"exposure of '{file_name}' failed: {response.errors}")
            imager.wait_for_image_saved()
            if not os.path.exists(path):
                return False
            # Inside `protect()`, so the mover cannot take the file while it is open.
            stamp_header(path, cards)
        filer.move_ram_to_shared(path)
        return True

    def _spiral_cards(self, step: tuple[int, Any, int, Any]) -> list[tuple[str, Any, str]]:
        """Where in the spiral a frame was taken -- the same cards on both frames of a pair."""
        index, cell, ring, offset = step
        date, seq = self._run_labels()
        return [
            ("CREATOR", "MAST save_spirals", "what wrote this frame"),
            # The observing night is NOT derivable from DATE-OBS by a reader who does not
            # know it turns at 12:00 UTC, so it is stated.
            ("RUNDATE", date, "observing night (turns at 12:00 UTC)"),
            ("RUNSEQ", seq, "flux metering run sequence"),
            ("STEPIDX", index, "spiral step index"),
            # The two numbers in the frame's name, worked out from STEPIDX.
            ("NAMERING", ring_position(index)[0], "ring in the frame name"),
            ("RINGSTEP", ring_position(index)[1], "step within that ring, from 1"),
            # Split because a tuple is not a legal FITS card value.
            ("CELLX", cell[0] if cell else None, "spiral cell x"),
            ("CELLY", cell[1] if cell else None, "spiral cell y"),
            ("RING", ring, "spiral ring, as PWI4 reports it"),
            ("OFFRA", offset[0] if offset else None, "RA offset on the sky, arcsec"),
            ("OFFDEC", offset[1] if offset else None, "Dec offset, arcsec"),
            ("RASTEPC", self._ra_step_commanded, "RA step commanded to PWI4, RA-coord arcsec"),
        ]

    def _step_cards(self, flux_name: str, step: tuple[int, Any, int, Any]) -> list[tuple[str, Any, str]]:
        """Added to a step frame's header after the imager has written it.

        Only cards the imager does not write itself: its own exposure, pointing and camera
        cards stay as they are. The spiral position is otherwise only on the flux frame
        and in result.json.
        """
        return [
            *self._spiral_cards(step),
            ("FLXFRAME", flux_name, "flux frame of the same exposure pair"),
            ("FLXEXPT", self.params.flux_exposure_us / 1e6, "flux exposure of that frame, seconds"),
        ]

    def _flux_cards(
        self, captured: dict[str, Any], imager_name: str, step: tuple[int, Any, int, Any]
    ) -> list[tuple[str, Any, str]]:
        """The header for one ThorCam frame: (keyword, value, comment).

        These frames used to carry nothing but the six mandatory structural keywords, so a
        flux frame separated from its run folder was anonymous -- and `flux-00007-00.fits`
        is a name that repeats in every run on the share. Everything here is already known
        at the moment of the exposure; it was simply never written down.

        Deliberately NO WCS. The ThorCam sees only the light emerging from the fibre and
        has no field, so `CRVAL`/`CRPIX`/`CTYPE` would assert that these pixels map to sky.
        A missing card is an absence; a wrong WCS invites a solver to solve it and DS9 to
        overlay catalogues on it.
        """
        meter = self._meter
        pointing = captured.get("pointing") or {}

        cards: list[tuple[str, Any, str]] = [
            ("DATE-OBS", captured.get("flux_started"), "UTC at the start of this exposure"),
            ("DATE-END", captured.get("flux_ended"), "UTC at the end of this exposure"),
            # Seconds, matching imagers/saving.py. Not EXPOSURE, which the PHD2-written
            # imager frames use: writing both would create a third convention rather than
            # settle the two that exist.
            ("EXPTIME", self.params.flux_exposure_us / 1e6, "exposure time in seconds"),
            # The CAMERA, per the FITS standard, and matching what PHD2 writes on the
            # imager frames. This deliberately disagrees with imagers/saving.py, which
            # puts the hostname here; the hostname belongs in TELESCOP, below. Do not
            # "fix" this into agreement with the wrong one.
            ("INSTRUME", meter.model if meter else None, "the flux meter"),
            ("CAMSN", meter.serial_number if meter else None, "flux meter serial number"),
            ("GAIN", self.params.flux_gain, "flux meter gain"),
            ("BLKLEVEL", self.params.flux_black_level, "black level commanded on the camera"),
            ("SATURATE", meter.saturation_level if meter else None, "camera full scale ADU"),
            ("TELESCOP", self.unit.hostname, "the unit"),
            # The card that ties this frame to the imager frame started with it; the
            # imager frame carries FLXFRAME pointing back.
            ("IMGFRAME", imager_name, "imager frame of the same exposure pair"),
            ("IMGEXPT", self.params.step_seconds, "imager exposure of that frame, seconds"),
            *self._spiral_cards(step),
        ]

        ra_hours = pointing.get("ra_j2000_hours")
        cards += [
            # Degrees, so no reader has to guess whether RA is hours or degrees.
            ("RA", ra_hours * 15.0 if ra_hours is not None else None, "mount J2000 RA, degrees"),
            ("DEC", pointing.get("dec_j2000_degs"), "mount J2000 Dec, degrees"),
            ("EQUINOX", 2000.0, "equinox of RA/DEC"),
            ("RADESYS", "ICRS", "reference frame of RA/DEC"),
            ("HA", pointing.get("ha_hours"), "hour angle, hours"),
            ("LMST", pointing.get("lmst_hours"), "local mean sidereal time, hours"),
        ]
        if self.params.ra_j2000_hours is not None and self.params.dec_j2000_degs is not None:
            # Only when one was actually requested. Left absent on a skip_acquisition run
            # so a reader can tell "no target was asked for" from "the target was here".
            cards.append(
                (
                    "OBJECT",
                    f"{self.params.ra_j2000_hours:.6f}h {self.params.dec_j2000_degs:+.6f}d",
                    "requested target (J2000)",
                )
            )
        return cards

    def _run_labels(self) -> tuple[str | None, str | None]:
        """(observing night, sequence) from the run folder, whose shape is
        `<...>/<date>/FluxMetering/<seq>`."""
        if self.state.folder is None:
            return None, None
        parts = Path(self.state.folder).parts
        if len(parts) >= 3 and parts[-2] == "FluxMetering":
            return parts[-3], parts[-1]
        return None, None

    def _write_fits(self, file_name: str, data: np.ndarray, cards: list[tuple[str, Any, str]] | None = None) -> None:
        if self.state.folder is None:
            raise SpiralsError("no folder")
        path = os.path.join(self.state.folder, file_name)
        header = fits.Header()
        for keyword, value, comment in cards or []:
            # A card whose value is unknown is omitted rather than written empty: a header
            # that says nothing about a thing is honest, one that says "" is not.
            if value is not None:
                header[keyword] = (value, comment)
        with MoveGuardian().protect(path):
            fits.PrimaryHDU(data=np.asarray(data), header=header).writeto(path, overwrite=True)
        filer.move_ram_to_shared(path)

    def _finish(self, terminal: str) -> None:
        """Write the result and release the unit. The mount is left on the last cell: the
        spiral is reset when the next one starts, after its acquisition."""
        self.state.terminal_state = terminal
        self.state.ended_at = isoformat_utc()

        try:
            self._write_result(self._result_document(final=True), final=True)
        except Exception:  # a lost result must not also strand the mount
            logger.exception("could not write result.json")

        if self._meter is not None:
            self._meter.close()
            self._meter = None

        self.state.active = False
        self.state.phase = "idle"
        self.unit.end_activity(UnitActivities.FluxMetering)

        # `flush` before saying the run is done: `move_ram_to_shared` is asynchronous, and a
        # run writes gigabytes, so "complete" would otherwise be reported while the products
        # were still queued on a volatile RAM disk.
        if not filer.flush(timeout=300.0):
            logger.error("products were still in flight when the run ended")
        logger.info(f"spirals ended: {terminal}, {len(self.steps)} of {self.params.total_cells} steps")

    def _result_document(self, final: bool) -> dict[str, Any]:
        """The status model plus what only the run itself knows: what was asked for, and
        which camera answered. One document, so a reader is never left joining the products
        against a status they no longer have."""
        document = {
            **self.status().model_dump(),
            "params": asdict(self.params),
            "total_cells": self.params.total_cells,
            "flux_exposure_us": self.params.flux_exposure_us,
            "flux_meter": self._meter.description if self._meter else None,
            "saturation_level": self._meter.saturation_level if self._meter else None,
            "hostname": self.unit.hostname,
            # Frozen at the value this run was configured with: the plate scale can be
            # reconfigured, and an analysis converting the cells' arcsec offsets to pixels
            # later must use this one.
            "pixel_scale_at_bin1": self._pixel_scale(),
            # The RA step as commanded to PWI4, and the declination it was sized for.
            # `params.x_step_arcsec` is the step on the sky.
            "ra_step_commanded_arcsec": self._ra_step_commanded,
            "dec_for_ra_step_degs": self._step_dec,
        }
        if final:
            # `status()` derives `active` from the worker thread being alive, and _finish
            # runs INSIDE that thread -- so the live status is necessarily active=True and
            # mid-phase right here. This document describes a run that is over, and says
            # so; `terminal_state` carries how it ended.
            document["active"] = False
            document["phase"] = "idle"
        return document

    def _save_progress(self) -> None:
        """Rewrite result.json after a step. Never fails the run: the frames are the
        product, and the final write at `_finish` is attempted regardless."""
        try:
            self._write_result(self._result_document(final=False), final=False)
        except Exception as ex:  # noqa: BLE001
            logger.warning(f"spirals: could not update result.json: {ex}")

    def _write_result(self, result: dict[str, Any], final: bool) -> None:
        """Write result.json in the run folder, and move it to the share only when `final`.

        Written to a temporary file and renamed over the old one, so a run that dies
        mid-write leaves the previous step's document rather than half of this one. Kept
        on the ram disk until the end because the share copy would otherwise be overwritten
        after every step; a run that never reaches `_finish` has it relocated by the
        startup sweep, with its frames.
        """
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


def stamp_header(path: str, cards: list[tuple[str, Any, str]]) -> None:
    """Add `cards` to the primary header of the FITS file at `path`. Best effort.

    Cards whose value is unknown are omitted, as in `_write_fits`. A failure is logged and
    swallowed: the frame is already on disk, and losing the stamp must not lose it. The
    same shape as `phd2.fits_header.stamp_cooling`.
    """
    try:
        with fits.open(path, mode="update", memmap=False) as hdul:
            header = hdul[0].header  # type: ignore[union-attr]
            for keyword, value, comment in cards:
                if value is not None:
                    header[keyword] = (value, comment)
    except Exception as ex:  # noqa: BLE001
        logger.warning(f"spirals: could not stamp the spiral cards into '{path}': {ex}")
