"""Operator-driven spiral search, and the measurement it produces.

The sequence is manual by design:

1. ``start`` fixes the step size and takes a **reference** frame.
2. The operator calls ``step`` repeatedly, watching an independent Ximea camera until
   the star sits on the optical axis. Only the operator can judge that, which is why
   nothing here tries to.
3. ``end`` takes a **final** frame and cross-correlates it against the reference. The
   shift, in pixels, is where the optical axis lies relative to where the field started.

PWI4 owns the spiral itself -- this only says "next" or "previous" and records where PWI4
reports it ended up.

Frames come back from disk, not memory: the configured imager is PHD2, whose
``can_image_to_memory`` is False. Each frame is therefore written, read back, and only
then handed to the mover -- and the read happens inside ``MoveGuardian.protect`` so a
mover cannot take the file mid-read. That protection does double duty, since a protected
path is also a *product*, which is what stops ``release_folder`` discarding it.
"""

from __future__ import annotations

import datetime
import json
import math
import os
import threading
from typing import TYPE_CHECKING, Any

from astropy.io import fits

from common.activities import UnitActivities
from common.canonical import CanonicalResponse
from common.config import Config
from common.config.rois import FcuVersion, SpecRoiConfig
from common.filer import Filer, MoveGuardian
from common.mast_logging import get_logger
from common.models.statuses import ImagerSettings
from common.paths import PathMaker
from common.utils import function_name, isoformat_zulu
from imaging.frame_shift import (
    DEFAULT_MARGIN_HORIZONTAL,
    DEFAULT_MARGIN_VERTICAL,
    MIN_CONFIDENCE,
    margins_from_fraction,
    max_reliable_shift,
    measure_shift,
)
from mount import SettleMode

if TYPE_CHECKING:
    from unit import Unit

logger = get_logger(__name__)
filer = Filer(logger)


class SpiralSearchError(Exception):
    """A spiral session could not do something it needs to do."""


REFERENCE_IMAGE = "reference.fits"
FINAL_IMAGE = "final.fits"
RESULT_FILE = "result.json"

#: An abandoned session holds tracking on and an exposure series open. After this long
#: it is closed WITHOUT a final frame: an hour on, nobody has confirmed the star is
#: centred, so a shift measured then would look like a result without being one.
SESSION_TIMEOUT_SECONDS = 3600.0

#: A frame with more trailing blank rows than this did not read out fully. Two, not zero:
#: a single dark row at the sensor edge is unremarkable, a block of them is a truncated
#: transfer.
MAX_TRAILING_BLANK_ROWS = 2


def _reject_truncated_frame(data, file_name: str) -> None:
    """Refuse a frame whose bottom rows are entirely zero.

    On 2026-08-17 four spiral sessions on mast00 produced frames that stopped dead at row
    4881 of 5640 -- the same row every time, in every frame -- with the remaining 758 rows
    all zero, and the sources in what did arrive were one pixel tall and ~78 wide. PHD2's
    own display showed the same, so this happens at or below the camera; the FITS was
    written at its declared size and only partly filled.

    Nothing noticed. The sessions ran to completion and the last reported a shift of
    (-0.0, 0.01) at confidence 0.93 -- an answer that was wrong and looked healthy, because
    a correlation between two frames sharing the same dead region locks onto it.

    The cause is still unknown, so this checks the symptom rather than any theory about it:
    a truncated readout cannot produce a usable measurement whatever caused it, and failing
    on the first frame turns a night of plausible wrong answers into one clear error --
    with the frame still on the ram disk and the camera's state intact, which is the
    evidence that was missing while diagnosing it.
    """
    if data is None or getattr(data, "ndim", 0) != 2 or data.shape[0] == 0:
        return

    blank_rows = 0
    for row in reversed(data):
        if row.any():
            break
        blank_rows += 1

    if blank_rows > MAX_TRAILING_BLANK_ROWS:
        raise SpiralSearchError(
            f"'{file_name}' is truncated: the last {blank_rows} of {data.shape[0]} rows are "
            f"entirely zero, so the frame did not read out fully. The session is being "
            f"stopped rather than measuring a shift from a partial image."
        )


class SpiralSearch:
    """One spiral session at a time, owned by the Unit."""

    def __init__(self, unit: Unit):
        self.unit = unit
        self._lock = threading.RLock()
        self._clear()

    def _clear(self) -> None:
        self.folder: str | None = None
        self.exposure_series = None
        self.reference: Any = None
        self.x_step_arcsec: float = 0.0
        self.y_step_arcsec: float = 0.0
        self.exposure_seconds: float = 0.0
        self.margin_horizontal: int = DEFAULT_MARGIN_HORIZONTAL
        self.margin_vertical: int = DEFAULT_MARGIN_VERTICAL
        self.center_source: str = ""
        self.margin_source: str = ""
        self.center_x: int = 0
        self.center_y: int = 0
        self.save_intermediate_exposures: bool = False
        self.steps: list[dict] = []
        self.started_at: str | None = None
        self._dec_degrees: float | None = None
        self._timer: threading.Timer | None = None

    @property
    def is_active(self) -> bool:
        return self.folder is not None

    # ---------------------------------------------------------------- frames --

    def _expose(self, file_name: str) -> tuple[str, Any]:
        """Expose, save under `file_name`, read the frame back, then hand it to the mover.

        Returns (path, data). The read is inside protect() so a mover cannot take the
        file while astropy has it open, and protect() also marks the path as a product
        so release_folder keeps it.
        """
        imager, conf = self.unit.imager, self.unit.unit_conf
        assert imager is not None and conf is not None and self.folder is not None
        path = os.path.join(self.folder, file_name)

        with MoveGuardian().protect(path):
            # roi and gain are NOT optional. PHD2's non-guiding path -- which is the one a
            # spiral takes -- does `assert settings.roi` and converts settings.gain, so
            # omitting either raises inside the backend. Full frame at bin 1: the whole
            # point is to compare the same detector area before and after.
            imager.latest_settings = ImagerSettings(
                seconds=self.exposure_seconds,
                save=True,
                image_path=path,
                binning=1,  # always 1: the correlation wants full detector sampling
                roi=imager.full_frame,
                gain=conf.acquisition.gain,
            )
            response = imager.start_exposure(imager.latest_settings)
            # Checked, not ignored. PHD2 returns errors (rather than raising) when it is
            # disconnected or when the requested binning does not match its profile, and
            # in that case no image is ever saved -- so `wait_for_image_saved` would block
            # on an event that will never be set, with no timeout, holding the session
            # lock forever. Failing here turns an indefinite hang into an error.
            if response is not None and response.failed:
                raise SpiralSearchError(f"exposure of '{file_name}' failed: {response.errors}")

            imager.wait_for_image_saved()
            data = fits.getdata(path)
            _reject_truncated_frame(data, file_name)

        filer.move_ram_to_shared(path)
        return path, data

    def _write_result(self, result: dict) -> None:
        assert self.folder is not None
        path = os.path.join(self.folder, RESULT_FILE)
        with MoveGuardian().protect(path), open(path, "w") as fp:
            json.dump(result, fp, indent=2, default=str)
        filer.move_ram_to_shared(path)

    def _spiral_offset(self) -> dict:
        """Where PWI4 says the spiral currently is, as integer GRID CELLS.

        ``spiral_offset.x``/``.y`` are counts of steps along each axis, not angles -- the
        step size is carried separately as ``x_step_arcsec``/``y_step_arcsec``, so the
        commanded offset is the product. Sessions 0001 and 0004 (2026-08-13) walked the
        identical cell sequence (1,0), (1,-1), (0,-1) at 1" and 10" steps respectively,
        which is what settles it: the values do not scale with the angular step.

        The declination is picked up from the same status call, for the pixel estimate in
        the step message. Both reads go through getattr: PWI4Status hangs its sections off
        `Section`, which declares no attributes at all, so nothing here is checkable
        statically -- and a mount that reports one field but not the other should cost the
        message only the part that is missing.

        An absent offset is a BRANCH, not an exception. PWI4 older than 4.0.11 Beta 8 does
        not report it and pwi4_client sets the whole section to None, which is a version
        fact rather than a fault; letting `offset.x` raise into the handler below logged a
        traceback on every single step and called an unsupported feature an error.
        """
        try:
            status = self.unit.mount.pw.status()  # type: ignore[union-attr]
            self._dec_degrees = getattr(status.mount, "dec_j2000_degs", None)
            offset = getattr(status.mount, "spiral_offset", None)
            if offset is None:
                logger.warning("this PWI4 does not report the spiral offset (needs 4.0.11 Beta 8 or later)")
                return {"x": None, "y": None}
            return {"x": offset.x, "y": offset.y}
        except Exception:
            logger.exception("could not read PWI4 spiral offset")
            return {"x": None, "y": None}

    def _pixels_from_reference(self, x: int, y: int) -> float | None:
        """Roughly how far the field has moved, in detector pixels, since the reference.

        Meant to be comparable with the shift ``end`` reports, which is why the RA axis
        carries a cos(dec) factor: ``x_step_arcsec`` is RA COORDINATE arcsec, so the sky
        moves ``x * x_step_arcsec * cos(dec)`` along it (MAST_unit#136, measured at dec
        +41 as 7.44" for 10" commanded). Without it the estimate reads 25% high there and
        invites an operator to distrust a correct measurement.

        None -- so the caller can omit the clause -- when the plate scale is unset or the
        declination is unknown. ``pixel_scale_at_bin1`` is 0.0 in the config DB today
        (MAST_unit#138), so that is the live path, and no estimate at all beats "0 px".
        """
        conf = self.unit.unit_conf
        scale = conf.imager.pixel_scale_at_bin1 if conf is not None else 0.0
        if not scale or scale <= 0.0 or self._dec_degrees is None:
            return None
        dx = x * self.x_step_arcsec * math.cos(math.radians(self._dec_degrees))
        dy = y * self.y_step_arcsec
        return math.hypot(dx, dy) / scale

    def _revisit_clause(self, entry: dict) -> str:
        """Whether this cell has been occupied before in this session, and when.

        A cell IS a position, so a match means the same sky rather than merely similar
        sky -- which is what lets an operator confirm they have returned to the position
        they judged brightest before calling ``end``, the one precondition nothing here
        can check for them.

        Cells PWI4 could not report are skipped, never matched against each other: two
        UNKNOWN positions are not the same position, and saying otherwise would send the
        operator confidently to the wrong place.
        """
        for earlier in reversed(self.steps[:-1]):  # [:-1]: `entry` itself is already appended
            if self._same_cell(earlier, entry):
                return "back at the reference position" if earlier["n"] == 0 else f"back at step#{earlier['n']}"
        return "new position"

    @staticmethod
    def _same_cell(a: dict, b: dict) -> bool:
        """True when two step entries sit on the same spiral cell.

        An unknown cell matches nothing, INCLUDING another unknown cell -- see
        `_revisit_clause`. One rule, one place, because `end` leans on it too.
        """
        first, second = a["spiral_offset"], b["spiral_offset"]
        if None in (first["x"], first["y"], second["x"], second["y"]):
            return False
        return (first["x"], first["y"]) == (second["x"], second["y"])

    def _step_message(self, entry: dict) -> dict:
        """What an operator gets back from a step.

        It has to answer "where am I", not "how many times have I pressed the button".
        The counter is monotonic -- ``previous`` increments it too -- so after
        next/next/previous ``step#`` reads 3 while the mount is back at the cell it
        occupied at step 1. ``cell``, ``ring`` and ``revisit`` are what carry position.
        """
        x, y = entry["spiral_offset"]["x"], entry["spiral_offset"]["y"]
        if x is None or y is None:
            # Said plainly rather than formatted around: with no cell the operator has no
            # way to confirm they are back at the brightest position, and `end` will not
            # catch it for them.
            return {"step#": entry["n"], "error": "PWI4 reported no spiral offset"}

        ret = {
            "step#": entry["n"],
            # (x, y), NOT ({x}, {y}) -- the latter is a pair of one-element SETS, which is
            # what the braces meant back when this line was an f-string.
            "cell": (x, y),
            "ring": max(abs(x), abs(y)),
            "offset": f"{x * self.x_step_arcsec:+.1f}arcsec RA, {y * self.y_step_arcsec:+.1f}arcsec Dec",
            "revisit": self._revisit_clause(entry),
        }
        pixels = self._pixels_from_reference(x, y)
        if pixels is not None:
            ret["offset"] += f" (~{pixels:.0f} px)"
        return ret

    # ----------------------------------------------------------------- verbs --

    def start(
        self,
        x_step_arcsec: float,
        y_step_arcsec: float,
        exposure_seconds: float,
        save_intermediate_exposures: bool = False,
        center_x: int | None = None,
        center_y: int | None = None,
        usable_fraction: float | None = None,
    ) -> CanonicalResponse:
        op = function_name()
        with self._lock:
            if self.is_active:
                logger.warning(f"{op}: a spiral session was still open; closing it before starting a new one")
                self._abandon("superseded by a new spiral session")

            # Deliberately a warning, not a refusal: the operating assumption is that a
            # spiral is never started mid-acquisition. If that ever stops being true,
            # this line is how we find out.
            if self.unit.is_active(UnitActivities.Acquiring) or self.unit.is_active(UnitActivities.Guiding):
                logger.warning(f"{op}: starting a spiral while acquiring/guiding -- the mount is being driven twice")

            assert self.unit.mount is not None and self.unit.mount.pw is not None and self.unit.imager is not None

            self.x_step_arcsec = x_step_arcsec
            self.y_step_arcsec = y_step_arcsec
            self.exposure_seconds = exposure_seconds
            self.save_intermediate_exposures = save_intermediate_exposures
            self.started_at = isoformat_zulu(datetime.datetime.now(datetime.UTC))

            # Checked, not fired and forgotten. The whole method measures how far the sky
            # moved between two frames, which is meaningless if the mount is not holding the
            # field: an untracked exposure trails, and consecutive trailed frames give the
            # correlation nothing coherent to lock onto. On 2026-08-17 three sessions ran to
            # completion against a mount that had never been told to track -- `start_tracking`
            # returned bare because `connected` was false -- and the last of them reported a
            # shift of (-0.0, 0.01) with confidence 0.93, an answer that was wrong and looked
            # healthy. Refusing here is the difference between one clear error and a night's
            # worth of unusable frames.
            response = self.unit.mount.start_tracking()
            if response is not None and response.failed:
                logger.error(f"{op}: cannot start the session -- {response.errors}")
                return response

            self.unit.mount.pw.mount_spiral_offset_new(x_step_arcsec=x_step_arcsec, y_step_arcsec=y_step_arcsec)
            self.folder = PathMaker().make_spirals_folder()
            self.exposure_series = self.unit.imager.start_exposure_series(purpose="spiral")

            try:
                _path, self.reference = self._expose(REFERENCE_IMAGE)
            except Exception as ex:
                # Tracking is already on and a series is already open. Without this the
                # session would be left half-built: no reference frame, so no `end` is
                # possible, but the mount still tracking and the series still open with
                # nothing to close them.
                logger.exception(f"{op}: the reference exposure failed; closing the session down")
                self._close()
                self._clear()
                return CanonicalResponse(errors=[f"{op}: reference exposure failed: {ex}"])

            # Resolved here, not above: the frame-centre fallback needs a frame to exist.
            self.center_x, self.center_y, self.center_source = resolve_center(center_x, center_y, self.reference.shape)
            self.margin_horizontal, self.margin_vertical, self.margin_source = resolve_margins(
                usable_fraction, self.reference.shape
            )
            logger.info(
                f"{op}: usable region centred on ({self.center_x}, {self.center_y}) from {self.center_source}, "
                f"margins ({self.margin_horizontal}, {self.margin_vertical}) from {self.margin_source}"
            )
            self._log_step(direction="reference", image=REFERENCE_IMAGE)

            self._timer = threading.Timer(SESSION_TIMEOUT_SECONDS, self._on_timeout)
            self._timer.daemon = True
            self._timer.start()

            logger.info(f"{op}: spiral session open in '{self.folder}', steps ({x_step_arcsec}, {y_step_arcsec}) arcsec")
            return CanonicalResponse(value={"folder": self.folder, "reference_image": REFERENCE_IMAGE})

    def step(self, forward: bool) -> CanonicalResponse:
        op = function_name()
        with self._lock:
            if not self.is_active:
                return CanonicalResponse(errors=[f"{op}: no spiral session is open; call spiral_new_path first"])

            assert self.unit.mount is not None and self.unit.mount.pw is not None

            if forward:
                self.unit.mount.pw.mount_spiral_offset_next()
            else:
                self.unit.mount.pw.mount_spiral_offset_previous()
            self.unit.mount.wait_until_settled(SettleMode.OFFSET_STEP)

            image = None
            if self.save_intermediate_exposures and self.folder:
                image = "step-" + PathMaker().make_seq(self.folder) + ".fits"
                try:
                    self._expose(image)
                except Exception:
                    # A lost intermediate frame is a nuisance, not a reason to end the
                    # session: the measurement needs only the reference and the final.
                    logger.exception(f"{op}: intermediate exposure '{image}' failed; continuing")
                    image = None

            entry = self._log_step(direction="next" if forward else "previous", image=image)
            # Not the raw entry: the operator is reading this between presses, and needs
            # where they ARE. The full log of every step is in result.json either way.
            return CanonicalResponse(value=self._step_message(entry))

    def end(self) -> CanonicalResponse:
        """Measure the shift from the reference frame to HERE.

        The operator must be sitting at the position they judged brightest when they call
        this: the session is deliberately manual, and nothing here can check the claim.
        The correlation reports how far the sky moved between the two frames, not whether
        the right position was chosen.
        """
        op = function_name()
        with self._lock:
            if not self.is_active:
                return CanonicalResponse(errors=[f"{op}: no spiral session is open"])

            self._cancel_timer()
            try:
                _path, final = self._expose(FINAL_IMAGE)
            except Exception as ex:
                # No final frame means no measurement -- but the session must still close,
                # or tracking stays on and the folder is never released. Recorded as an
                # abort so result.json says why there is no shift, rather than nothing.
                logger.exception(f"{op}: the final exposure failed; closing the session without a measurement")
                self._abandon(f"final exposure failed: {ex}")
                return CanonicalResponse(errors=[f"{op}: final exposure failed: {ex}"])

            final_entry = self._log_step(direction="final", image=FINAL_IMAGE)
            # An operator who judged the spiral origin brightest backtracks to it and ends
            # there, so the mount genuinely did not move and a null shift is the CORRECT
            # answer -- not the fixed-pattern capture `at_origin` normally flags.
            at_reference_position = self._same_cell(self.steps[0], final_entry)

            shift = measure_shift(
                self.reference,
                final,
                center_x=self.center_x,
                center_y=self.center_y,
                margin_horizontal=self.margin_horizontal,
                margin_vertical=self.margin_vertical,
                expect_no_motion=at_reference_position,
            )
            limit = max_reliable_shift(self.reference.shape, self.margin_horizontal)
            magnitude = (shift.dx**2 + shift.dy**2) ** 0.5
            beyond_limit = magnitude > limit
            if beyond_limit:
                logger.warning(
                    f"{op}: measured shift {magnitude:.2f} px exceeds the reliable range of {limit:.0f} px "
                    f"at margins ({self.margin_horizontal}, {self.margin_vertical}) -- too little sky is common "
                    "to both frames, "
                    "so dx/dy should not be trusted"
                )

            result = self._result(
                shift=shift.as_dict(),
                limit=limit,
                beyond_limit=beyond_limit,
                magnitude=magnitude,
                at_reference_position=at_reference_position,
            )
            self._write_result(result)
            self._close()
            logger.info(f"{op}: spiral session closed, dx={shift.dx} dy={shift.dy} px (confidence {shift.confidence})")
            return CanonicalResponse(value=result)

    # -------------------------------------------------------------- internals --

    def _log_step(self, direction: str, image: str | None) -> dict:
        entry = {
            "n": len(self.steps),
            "direction": direction,
            "time": isoformat_zulu(datetime.datetime.now(datetime.UTC)),
            "spiral_offset": self._spiral_offset(),
            "image": image,
        }
        self.steps.append(entry)
        return entry

    def _result(self, **extra) -> dict:
        shift = extra.get("shift")
        magnitude = extra.get("magnitude")
        result: dict = {
            "started_at": self.started_at,
            "ended_at": isoformat_zulu(datetime.datetime.now(datetime.UTC)),
            "x_step_arcsec": self.x_step_arcsec,
            "y_step_arcsec": self.y_step_arcsec,
            "exposure_seconds": self.exposure_seconds,
            "margin_horizontal": self.margin_horizontal,
            "margin_vertical": self.margin_vertical,
            "center_source": self.center_source,
            "margin_source": self.margin_source,
            "center_x": self.center_x,
            "center_y": self.center_y,
            "save_intermediate_exposures": self.save_intermediate_exposures,
            "reference_image": REFERENCE_IMAGE,
            "steps": self.steps,
            "aborted": False,
        }
        if shift is None:
            return result | {"final_image": None, "shift": None, "aborted": True, "abort_reason": extra.get("reason")}

        result |= {
            "final_image": FINAL_IMAGE,
            "shift": shift,
            "shift_magnitude_pixels": round(float(magnitude), 2) if magnitude else None,
            "max_reliable_shift_pixels": round(float(extra["limit"]), 1),
            "shift_exceeds_reliable_range": bool(extra["beyond_limit"]),
            # The two independent ways this measurement can be wrong: too little common
            # sky (above), and frames that never correlated in the first place (below).
            "confidence_below_threshold": bool(shift["confidence"] < MIN_CONFIDENCE),
            "min_confidence": MIN_CONFIDENCE,
            # The session ended on the cell it started from, so a shift near zero is the
            # answer rather than a symptom. Without this, `at_origin` in the shift below
            # reads as a fixed-pattern alarm on a perfectly good measurement.
            "ended_at_reference_position": bool(extra.get("at_reference_position", False)),
        }
        return result

    def _on_timeout(self) -> None:
        with self._lock:
            if not self.is_active:
                return  # end() got here first
            logger.warning(
                f"spiral session in '{self.folder}' abandoned: no spiral_end_path within "
                f"{SESSION_TIMEOUT_SECONDS / 3600:.0f}h. Closing WITHOUT a final frame -- nobody confirmed the "
                "star was centred, so any shift measured now would look like a result without being one."
            )
            self._abandon(f"no spiral_end_path within {SESSION_TIMEOUT_SECONDS / 3600:.0f}h")

    def _abandon(self, reason: str) -> None:
        """Close the session with no measurement, leaving a result that says so.

        Caller holds the lock. The result.json is still written -- an abandoned run that
        leaves no trace is indistinguishable from one that never happened.
        """
        self._cancel_timer()
        try:
            self._write_result(self._result(reason=reason))
        except Exception:
            logger.exception("could not write the aborted spiral result")
        self._close()

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _close(self) -> None:
        """Stop tracking, end the series, reap the folder. Caller holds the lock."""
        if self.unit.imager is not None and self.exposure_series is not None:
            self.unit.imager.end_exposure_series(self.exposure_series)
        if self.unit.mount is not None:
            self.unit.mount.stop_tracking()
        if self.folder is not None:
            # Removed once every protected artifact -- the two frames and result.json --
            # has reached the shared area, and not before.
            MoveGuardian().release_folder(self.folder, logger=logger)
        self._clear()


def guiding_roi() -> SpecRoiConfig | None:
    """``guiding.rois[fcu_v2]``, or None if it is missing or not the expected shape.

    fcu_v1 is deprecated and is never consulted. Returns None -- rather than raising or
    guessing -- so the resolvers below can fall back. A silently wrong window would give
    a confident, wrong measurement; a fallback that says so in the log and in the result
    will not.
    """
    unit_conf = Config().get_unit()
    if unit_conf is None:
        return None
    try:
        roi = unit_conf.guiding.rois.get(FcuVersion.v2)
    except Exception:
        logger.exception("could not read guiding.rois[fcu_v2] from the configuration")
        return None
    if not isinstance(roi, SpecRoiConfig):
        logger.warning(f"guiding.rois[fcu_v2] is {type(roi).__name__}, expected SpecRoiConfig")
        return None
    return roi


def resolve_center(center_x: int | None, center_y: int | None, shape: tuple[int, int]) -> tuple[int, int, str]:
    """Centre of the usable region, by descending precedence. Returns (x, y, source).

    1. the caller's parameters, but only if BOTH were given -- one coordinate on its own
       is ambiguous about what the other should be, so it is not mixed with a fallback;
    2. the configured fibre position, if both fiber_x and fiber_y are defined;
    3. the centre of the frame.

    The frame centre is only knowable once a frame exists, which is why this resolves
    after the reference exposure rather than when the session opens.
    """
    if center_x is not None and center_y is not None:
        return center_x, center_y, "parameters"
    roi = guiding_roi()
    if roi is not None and roi.fiber_x is not None and roi.fiber_y is not None:
        return int(roi.fiber_x), int(roi.fiber_y), "guiding.rois[fcu_v2]"
    ny, nx = shape
    return nx // 2, ny // 2, "frame centre"


def resolve_margins(usable_fraction: float | None, shape: tuple[int, int]) -> tuple[int, int, str]:
    """Size of the usable region, by descending precedence. Returns (horizontal, vertical, source).

    1. the caller's `usable_fraction`, converted to margins;
    2. the configured ROI's own margins;
    3. `DEFAULT_MARGIN_HORIZONTAL` / `DEFAULT_MARGIN_VERTICAL`.

    Resolved independently of the centre, since the operator may want a different area
    around the configured fibre, or the configured area around a different point.
    """
    if usable_fraction is not None:
        horizontal, vertical = margins_from_fraction(shape, usable_fraction)
        return horizontal, vertical, f"usable_fraction={usable_fraction}"
    roi = guiding_roi()
    if roi is not None and roi.margin_horizontal is not None and roi.margin_vertical is not None:
        return int(roi.margin_horizontal), int(roi.margin_vertical), "guiding.rois[fcu_v2]"
    return DEFAULT_MARGIN_HORIZONTAL, DEFAULT_MARGIN_VERTICAL, "default"
