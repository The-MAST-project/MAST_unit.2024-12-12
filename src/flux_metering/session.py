"""One run of `acquire_and_find_max_flux`.

Acquire a star, walk a spiral, find the pointing where most light reaches the fibre, and
correlate the imager frame taken there against the reference to get `dx, dy` -- the offset
between where acquisition PUT the star and where the fibre actually is.

Design: `mast-claude-config/plans/flux_metering_design.md`. The decisions that are easiest
to undo by accident, and why they are what they are:

* **The ring is the unit of the stopping rule** (section 4). A square spiral circles the
  origin, so flux rises and falls on every ring; "it went up then down" stops at the first
  near-pass. A complete ring with no improvement is the smallest statement about the
  neighbourhood that means anything.
* **Ring membership is READ, not assumed.** PWI4 owns the traversal order, so the ring is
  derived from the offsets it reports -- `max(|x|, |y|)` -- and a ring is finished when the
  reported ring first exceeds it.
* **No backtrack** (section 3.1). The correlation reads the imager frame already taken at the
  arg-max index, which is why every frame is kept at full sampling: which one turns out to be
  the arg-max is unknown until the search ends.
* **Saturation is recorded, never acted on** (section 5.4). `argmax_saturated` is what says
  whether a run's answer is usable.
* Nothing here writes configuration.
"""

from __future__ import annotations

import datetime
import json
import math
import os
import threading
import time
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

import numpy as np
from astropy.io import fits

from common.activities import UnitActivities
from common.filer import Filer, FilerTop, MoveGuardian
from common.mast_logging import get_logger
from common.models.statuses import ImagerSettings
from common.paths import PathMaker
from common.utils import function_name
from flux_metering.flux_meter import FluxMeter, FluxMeterError, frame_flux, saturated_pixels
from imaging.frame_shift import MIN_CONFIDENCE, margins_from_fraction, max_reliable_shift, measure_shift
from mount import SettleMode
from spiral_search import resolve_center

if TYPE_CHECKING:
    from unit import Unit

logger = get_logger(__name__)
filer = Filer(logger)

REFERENCE_IMAGE = "reference.fits"

#: How long to wait for the whole acquisition to reach tolerance before giving up. The
#: procedure is meaningless without a converged acquisition, so this is a ceiling on
#: waiting, not a tolerance of its own.
ACQUISITION_TIMEOUT_SECONDS = 900.0

#: A pixel count, not a boolean. The ThorCam's field is black, so one hot pixel or a cosmic
#: ray would otherwise mark every frame of a 30-minute run as saturated.
SATURATED_PIXELS_ALLOWED = 5


class FluxMeteringError(Exception):
    """A flux-metering run could not be set up, or could not do something it needs to do."""


@dataclass
class FluxMeteringParams:
    """What the operator chose. Carried whole so the result can echo it back."""

    seconds: float = 5.0
    ra_j2000_hours: float | None = None
    dec_j2000_degs: float | None = None
    gain_absolute: int | None = None
    x_step_arcsec: float = 0.5
    y_step_arcsec: float = 0.5
    max_rings: int = 6
    patience_rings: int = 1
    max_radius_arcsec: float = 10.0
    flux_gain: float = 0.0
    flux_black_level: int = 3
    usable_fraction: float = 0.66

    @property
    def flux_exposure_us(self) -> int:
        """The ThorCam exposure follows the imager's.

        Not a convenience: a millisecond exposure samples one instant of a twinkling star,
        so the flux curve would carry scintillation on top of the coupling signal it exists
        to resolve -- and the arg-max is decided exactly where that curve is flattest.
        Seconds of integration smooth it.
        """
        return round(self.seconds * 1_000_000)


@dataclass
class Step:
    """One spiral step: where it was, what the fibre saw, what was written."""

    index: int
    cell: tuple[int, int] | None
    ring: int | None
    offset_arcsec: tuple[float, float] | None
    flux: float
    saturated_pixels: int
    saturated: bool
    imager_frame: str
    flux_frame: str
    imager_started_utc: str
    imager_ended_utc: str
    flux_started_utc: str
    flux_ended_utc: str


@dataclass
class FluxMeteringState:
    """What `find_max_flux_status` reports. A run is 20-40 minutes and unattended between
    glances, so start/stop without this leaves the operator blind."""

    active: bool = False
    phase: str = "idle"
    folder: str | None = None
    started_at: str | None = None
    index: int = 0
    cell: tuple[int, int] | None = None
    ring: int | None = None
    best_flux: float | None = None
    best_index: int | None = None
    best_cell: tuple[int, int] | None = None
    frames: int = 0
    saturated_frames: int = 0
    terminal_state: str | None = None
    last_error: str | None = None


class FluxMeteringSession:
    """One run at a time, owned by the Unit."""

    def __init__(self, unit: Unit, flux_meter: FluxMeter | None = None):
        self.unit = unit
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        #: Injected for tests and for any machine without a Zelux attached; `None` means
        #: open a real ThorCam when the run starts.
        self._injected_meter = flux_meter
        self._meter: FluxMeter | None = None
        self.state = FluxMeteringState()
        self.params = FluxMeteringParams()
        self.steps: list[Step] = []

    # ------------------------------------------------------------------- control --

    @property
    def is_active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def require_can_start(self) -> str | None:
        """Why a run cannot start, or None.

        A guard rather than an `assert`: asserts are stripped under `python -O`, and the
        response envelope renders `AssertionError` as an anonymous error carrying nothing
        the caller can act on.

        Everything here is checked BEFORE the thread is dispatched -- a route that
        dispatches and answers Ok has already spent the request.
        """
        if self.is_active:
            return "a flux-metering run is already in progress"
        if self.unit.mount is None or self.unit.imager is None:
            return "the mount and the imager must both be present"
        busy = (
            UnitActivities.Acquiring
            | UnitActivities.Guiding
            | UnitActivities.Autofocusing
            | UnitActivities.StartingUp
            | UnitActivities.ShuttingDown
        )
        if self.unit.activities & busy:
            return f"the unit is busy ({self.unit.activities_verbal})"
        if self.unit.acquirer is None:
            return "no acquirer, so the star cannot be put on the fibre"
        return None

    def start(self, params: FluxMeteringParams):
        """Validate, claim the unit, and dispatch. Returns a refusal or the initial state."""
        with self._lock:
            refusal = self.require_can_start()
            if refusal is not None:
                return refusal

            self.params = params
            self.steps = []
            self._stop.clear()
            folder = PathMaker().make_flux_metering_folder()
            self.state = FluxMeteringState(
                active=True,
                phase="acquiring",
                folder=folder,
                started_at=isoformat_utc(),
            )
            # Raised here rather than in the thread: the endpoint declares this flag as its
            # completion signal, so a caller answered Ok must find it already set.
            self.unit.start_activity(UnitActivities.FluxMetering)
            self._thread = threading.Thread(
                name="acquire_and_find_max_flux",
                target=self.do_acquire_and_find_max_flux,
                daemon=True,
            )
            self._thread.start()
            logger.info(f"flux metering started, products under '{folder}'")
            return asdict(self.state)

    def abort(self) -> None:
        """Ask the run to stop. It finishes the exposure it is inside, then unwinds."""
        if self.is_active:
            logger.info("flux metering: abort requested")
            self._stop.set()

    def status(self) -> dict:
        state = asdict(self.state)
        state["active"] = self.is_active
        return state

    # -------------------------------------------------------------------- the run --

    def do_acquire_and_find_max_flux(self) -> None:
        """The whole run, on its own thread. Never raises: the finally clause owns the
        unwind, and an escaping exception would leave the activity flag set and hang every
        caller watching it."""
        op = function_name()
        result: dict[str, Any] = {}
        try:
            self._open_meter()
            if not self._acquire():
                self._finish("acquisition_failed", result)
                return

            self.state.phase = "reference"
            reference = self._expose_imager(REFERENCE_IMAGE)

            self.state.phase = "spiral"
            terminal = self._walk_spiral()

            self.state.phase = "correlating"
            result = self._measure(reference, terminal)
            self._finish(terminal, result)
        except Exception as ex:  # the thread owns the mount; it must land it safely
            logger.exception(f"{op}: flux metering failed")
            self.state.last_error = str(ex)
            result["error"] = str(ex)
            self._finish("failed", result)

    def _acquire(self) -> bool:
        """Put the star on the ASSUMED fibre position, and wait for it to get there.

        Everything about the acquisition is fixed rather than operator-facing: mastrometry,
        gradual-by-rate, corrections on, sky phase skipped (the fibre only sees light with
        the folding mirror in, so the spec phase is the one that matters), and no handover
        to the guider.
        """
        acquirer = self.unit.acquirer
        if acquirer is None:
            self.state.last_error = "no acquirer"
            return False

        response = acquirer.endpoint_start_acquisition_and_guiding(
            seconds=self.params.seconds,
            ra_j2000_hours=self.params.ra_j2000_hours,
            dec_j2000_degs=self.params.dec_j2000_degs,
            gain_absolute=self.params.gain_absolute,
            skip_sky=True,
            use_set_limit_frame=True,
            handover_automatically_to_guider=False,
        )
        if response is not None and getattr(response, "failed", False):
            self.state.last_error = f"acquisition refused: {response.errors}"
            return False

        deadline = time.monotonic() + ACQUISITION_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if self._stop.is_set():
                self.state.last_error = "aborted during acquisition"
                return False
            if not self.unit.is_active(UnitActivities.Acquiring):
                break
            time.sleep(1.0)
        else:
            self.state.last_error = f"acquisition did not finish within {ACQUISITION_TIMEOUT_SECONDS:g}s"
            return False

        return True

    def _walk_spiral(self) -> str:
        """Walk until a ring adds nothing, a cap is hit, or the operator aborts.

        Returns the terminal state, which the result reports: `converged`, `max_rings`,
        `max_radius`, or `aborted`. They mean different things -- only `converged` says the
        arg-max is a peak rather than the best of a truncated search.
        """
        mount = self.unit.mount
        if mount is None or mount.pw is None:
            raise FluxMeteringError("no mount")

        mount.pw.mount_spiral_offset_new(self.params.x_step_arcsec, self.params.y_step_arcsec)

        best_flux: float | None = None
        best_ring = 0
        current_ring = 0
        rings_without_improvement = 0
        index = 0

        while True:
            if self._stop.is_set():
                return "aborted"

            cell, ring, offset = self._read_spiral_offset()
            step = self._measure_step(index, cell, ring, offset)
            self.steps.append(step)

            if best_flux is None or step.flux > best_flux:
                best_flux, best_ring = step.flux, ring if ring is not None else 0
                self.state.best_flux = step.flux
                self.state.best_index = index
                self.state.best_cell = cell

            if ring is not None and ring > current_ring:
                # A ring just finished. It is the completed ring, not the step, that the
                # stopping rule can say anything about.
                rings_without_improvement = 0 if best_ring >= current_ring else rings_without_improvement + 1
                current_ring = ring
                if rings_without_improvement >= self.params.patience_rings:
                    return "converged"
                if ring > self.params.max_rings:
                    return "max_rings"

            if offset is not None and self._radius_arcsec(offset) > self.params.max_radius_arcsec:
                return "max_radius"

            mount.pw.mount_spiral_offset_next()
            mount.wait_until_settled(SettleMode.OFFSET_STEP)
            index += 1

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
        offset = (cell[0] * self.params.x_step_arcsec, cell[1] * self.params.y_step_arcsec)
        return cell, max(abs(cell[0]), abs(cell[1])), offset

    def _radius_arcsec(self, offset: tuple[float, float]) -> float:
        """Sky angle from the origin, with cos(dec) on the RA axis.

        `x_step_arcsec` is RA COORDINATE arcsec, so the angle on the sky along that axis is
        the offset times cos(dec). Without it the radius reads 25% high at dec +41 -- the
        same factor `_pixels_from_reference` carries, MAST_unit#136 -- and the cap would
        mean a different thing at every declination.
        """
        dec = self._dec_degrees()
        scale = math.cos(math.radians(dec)) if dec is not None else 1.0
        return math.hypot(offset[0] * scale, offset[1])

    def _dec_degrees(self) -> float | None:
        try:
            return float(self.unit.mount.status().dec_j2000_degs)  # type: ignore[union-attr,arg-type]
        except Exception:  # noqa: BLE001
            return None

    # ---------------------------------------------------------------- measurement --

    def _measure_step(self, index: int, cell, ring, offset) -> Step:
        """One step: an imager frame and a ThorCam frame, exposed in parallel.

        In parallel because they must cover the same window -- see
        `FluxMeteringParams.flux_exposure_us` -- and each records its own start and end so
        the overlap is verifiable afterwards rather than assumed. The imager path goes
        through PHD2 and does not necessarily begin the instant it is asked.
        """
        imager_name = f"step-{index:05d}.fits"
        flux_name = f"flux-{index:05d}.fits"
        captured: dict[str, Any] = {}

        def take_flux():
            captured["flux_started"] = isoformat_utc()
            captured["flux_frame"] = self._meter.expose()  # type: ignore[union-attr]
            captured["flux_ended"] = isoformat_utc()

        flux_thread = threading.Thread(name="flux-exposure", target=_guard(captured, "flux_error", take_flux))
        flux_thread.start()

        imager_started = isoformat_utc()
        try:
            self._expose_imager(imager_name)
        finally:
            flux_thread.join()
        imager_ended = isoformat_utc()

        if "flux_error" in captured:
            raise FluxMeteringError(f"the flux exposure failed: {captured['flux_error']}")

        frame = captured["flux_frame"]
        self._write_fits(flux_name, frame)

        level = self._meter.saturation_level  # type: ignore[union-attr]
        n_saturated = saturated_pixels(frame, level)
        flux = frame_flux(frame, self.params.flux_black_level)
        saturated = n_saturated > SATURATED_PIXELS_ALLOWED

        self.state.index = index
        self.state.cell = cell
        self.state.ring = ring
        self.state.frames += 1
        if saturated:
            self.state.saturated_frames += 1

        logger.info(f"step {index}: cell={cell} ring={ring} flux={flux:.0f} saturated_px={n_saturated}")
        return Step(
            index=index,
            cell=cell,
            ring=ring,
            offset_arcsec=offset,
            flux=flux,
            saturated_pixels=n_saturated,
            saturated=saturated,
            imager_frame=imager_name,
            flux_frame=flux_name,
            imager_started_utc=imager_started,
            imager_ended_utc=imager_ended,
            flux_started_utc=captured["flux_started"],
            flux_ended_utc=captured["flux_ended"],
        )

    def _measure(self, reference: np.ndarray, terminal: str) -> dict[str, Any]:
        """Correlate the reference against the arg-max frame, and say what it means."""
        if not self.steps:
            return {"terminal_state": terminal, "error": "no steps were taken"}

        best = max(self.steps, key=lambda s: s.flux)
        shape = reference.shape
        center_x, center_y, center_source = resolve_center(None, None, shape)
        margin_h, margin_v = margins_from_fraction(shape, self.params.usable_fraction)

        final = self._read_fits(best.imager_frame)
        # The mount genuinely did not move when the arg-max is the origin, so a null shift
        # is the CORRECT answer there rather than the fixed-pattern capture `at_origin`
        # normally flags.
        at_origin = best.cell == (0, 0)
        shift = measure_shift(
            reference,
            final,
            center_x=center_x,
            center_y=center_y,
            margin_horizontal=margin_h,
            margin_vertical=margin_v,
            expect_no_motion=at_origin,
        )

        limit = max_reliable_shift(shape, margin_h)
        magnitude = math.hypot(shift.dx, shift.dy)
        return {
            "terminal_state": terminal,
            "dx": shift.dx,
            "dy": shift.dy,
            "confidence": shift.confidence,
            "at_origin": shift.at_origin,
            "low_confidence": shift.confidence < MIN_CONFIDENCE,
            "magnitude_px": magnitude,
            "max_reliable_shift_px": limit,
            "beyond_limit": magnitude > limit,
            "fiber_x": center_x,
            "fiber_y": center_y,
            "fiber_source": center_source,
            # Stated rather than left as arithmetic for the reader: a sign error is then
            # visible by eye on the first run instead of after five.
            "proposed_fiber_x": center_x + shift.dx,
            "proposed_fiber_y": center_y + shift.dy,
            "argmax_index": best.index,
            "argmax_cell": best.cell,
            "argmax_ring": best.ring,
            "argmax_frame": best.imager_frame,
            "argmax_offset_arcsec": best.offset_arcsec,
            "argmax_saturated": best.saturated,
            "saturated_frame_count": self.state.saturated_frames,
        }

    # ------------------------------------------------------------------- plumbing --

    def _open_meter(self) -> None:
        if self._injected_meter is not None:
            self._meter = self._injected_meter
        else:
            from flux_metering.thorcam import ThorCam

            cam = ThorCam()
            cam.open()
            self._meter = cam
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

    def _expose_imager(self, file_name: str) -> np.ndarray:
        """Expose full-frame at bin 1, save, read back, hand to the mover.

        Full frame and bin 1 because the correlation wants full detector sampling, and
        because any of these frames may turn out to be the arg-max -- which one is not known
        until the search ends.

        The read is inside `protect()` so a mover cannot take the file while astropy has it
        open; the mover runs on its own thread, which is what keeps that claim from
        self-deadlocking against this one.
        """
        imager, conf = self.unit.imager, self.unit.unit_conf
        if imager is None or conf is None or self.state.folder is None:
            raise FluxMeteringError("no imager, configuration or folder")

        path = os.path.join(self.state.folder, file_name)
        with MoveGuardian().protect(path):
            imager.latest_settings = ImagerSettings(
                seconds=self.params.seconds,
                save=True,
                image_path=path,
                binning=1,
                roi=imager.full_frame,
                gain=self.params.gain_absolute or conf.acquisition.gain,
            )
            response = imager.start_exposure(imager.latest_settings)
            if response is not None and response.failed:
                raise FluxMeteringError(f"exposure of '{file_name}' failed: {response.errors}")
            imager.wait_for_image_saved()
            data = np.asarray(fits.getdata(path), dtype=float)
        filer.move_ram_to_shared(path)
        return data

    def _write_fits(self, file_name: str, data: np.ndarray) -> None:
        if self.state.folder is None:
            raise FluxMeteringError("no folder")
        path = os.path.join(self.state.folder, file_name)
        with MoveGuardian().protect(path):
            fits.PrimaryHDU(data=np.asarray(data)).writeto(path, overwrite=True)
        filer.move_ram_to_shared(path)

    def _read_fits(self, file_name: str) -> np.ndarray:
        """Read a frame back, from the share if the mover has already taken it."""
        if self.state.folder is None:
            raise FluxMeteringError("no folder")
        local = os.path.join(self.state.folder, file_name)
        if os.path.exists(local):
            return np.asarray(fits.getdata(local), dtype=float)
        shared_folder = filer.change_top_to(FilerTop.Shared, self.state.folder)
        if shared_folder is None:
            raise FluxMeteringError(f"'{file_name}' is neither on the ram disk nor under a known root")
        return np.asarray(fits.getdata(os.path.join(shared_folder, file_name)), dtype=float)

    def _finish(self, terminal: str, result: dict[str, Any]) -> None:
        """Write the result, put the mount back, and release the unit.

        The spiral offset is reset on EVERY ending, converged included: without a backtrack
        the mount stops wherever the search stopped, which is an arbitrary cell up to a ring
        from the arg-max and of no use to anyone. Returning to the acquired position is the
        one predictable choice.
        """
        self.state.terminal_state = terminal
        result.setdefault("terminal_state", terminal)
        result["params"] = asdict(self.params)
        result["flux_exposure_us"] = self.params.flux_exposure_us
        result["flux_meter"] = self._meter.description if self._meter else None
        result["saturation_level"] = self._meter.saturation_level if self._meter else None
        result["steps"] = [asdict(s) for s in self.steps]
        result["started_at_utc"] = self.state.started_at
        result["ended_at_utc"] = isoformat_utc()
        result["hostname"] = self.unit.hostname

        try:
            self._write_result(result)
        except Exception:  # a lost result must not also strand the mount
            logger.exception("could not write result.json")

        try:
            if self.unit.mount is not None and self.unit.mount.pw is not None:
                self.unit.mount.pw.mount_spiral_offset_new(self.params.x_step_arcsec, self.params.y_step_arcsec)
        except Exception as ex:  # noqa: BLE001
            logger.error(f"could not reset the spiral offset: {ex}")

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
        logger.info(f"flux metering ended: {terminal}, {len(self.steps)} steps")

    def _write_result(self, result: dict[str, Any]) -> None:
        if self.state.folder is None:
            return
        path = os.path.join(self.state.folder, "result.json")
        with MoveGuardian().protect(path), open(path, "w") as fp:
            json.dump(result, fp, indent=2, default=str)
        filer.move_ram_to_shared(path)


def isoformat_utc() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


def _guard(sink: dict, key: str, work):
    """Run `work` on another thread, recording a failure instead of losing it.

    A bare thread that raises prints to stderr and vanishes; the parent then reads a missing
    result as a zero flux, which is a measurement rather than a failure.
    """

    def run():
        try:
            work()
        except Exception as ex:  # noqa: BLE001 -- reported through `sink`, not swallowed
            sink[key] = ex

    return run
