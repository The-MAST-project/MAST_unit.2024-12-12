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
import os
import threading
from typing import TYPE_CHECKING, Any

from astropy.io import fits

from common.activities import UnitActivities
from common.canonical import CanonicalResponse
from common.config import Config
from common.config.rois import FcuVersion, SpecRoiConfig
from common.filer import Filer, MoveGuardian
from common.interfaces.imager import ImagerSettings
from common.mast_logging import get_logger
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

        filer.move_ram_to_shared(path)
        return path, data

    def _write_result(self, result: dict) -> None:
        assert self.folder is not None
        path = os.path.join(self.folder, RESULT_FILE)
        with MoveGuardian().protect(path), open(path, "w") as fp:
            json.dump(result, fp, indent=2, default=str)
        filer.move_ram_to_shared(path)

    def _spiral_offset(self) -> dict:
        """Where PWI4 says the spiral currently is."""
        try:
            offset = self.unit.mount.pw.status().mount.spiral_offset  # type: ignore[union-attr]
            return {"x": offset.x, "y": offset.y}
        except Exception:
            logger.exception("could not read PWI4 spiral offset")
            return {"x": None, "y": None}

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

            self.unit.mount.start_tracking()
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

            logger.info(f"{op}: spiral session open in '{self.folder}', steps ({x_step_arcsec}, {y_step_arcsec})″")
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
            if self.save_intermediate_exposures:
                image = "step-" + PathMaker().make_seq(self.folder) + ".fits"
                try:
                    self._expose(image)
                except Exception:
                    # A lost intermediate frame is a nuisance, not a reason to end the
                    # session: the measurement needs only the reference and the final.
                    logger.exception(f"{op}: intermediate exposure '{image}' failed; continuing")
                    image = None

            entry = self._log_step(direction="next" if forward else "previous", image=image)
            return CanonicalResponse(value=entry)

    def end(self) -> CanonicalResponse:
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

            self._log_step(direction="final", image=FINAL_IMAGE)

            shift = measure_shift(
                self.reference,
                final,
                center_x=self.center_x,
                center_y=self.center_y,
                margin_horizontal=self.margin_horizontal,
                margin_vertical=self.margin_vertical,
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

            result = self._result(shift=shift.as_dict(), limit=limit, beyond_limit=beyond_limit, magnitude=magnitude)
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
            "shift_magnitude_pixels": round(float(magnitude), 2),
            "max_reliable_shift_pixels": round(float(extra["limit"]), 1),
            "shift_exceeds_reliable_range": bool(extra["beyond_limit"]),
            # The two independent ways this measurement can be wrong: too little common
            # sky (above), and frames that never correlated in the first place (below).
            "confidence_below_threshold": bool(shift["confidence"] < MIN_CONFIDENCE),
            "min_confidence": MIN_CONFIDENCE,
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
    try:
        roi = Config().get_unit().guiding.rois.get(FcuVersion.v2)
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
