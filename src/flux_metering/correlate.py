"""Correlating two steps of a flux-metering run that has already finished.

A run correlates exactly one pair -- its reference frame against its arg-max -- once, at
the end (`session.py::_correlate`). There is no way to ask what shift lies between any
*other* two steps without re-running the whole spiral, and for a run already on the share
that cannot be done at all: the sky has moved.

`correlate_steps` answers that question from the products alone.

**Everything comes from the run's own `result.json`.** The usable fraction, the fibre
position, the plate scale and the declination are all read back from the file rather than
from the live configuration or the current pointing. That is not fastidiousness: the
declination the session used is `mount.status().dec_j2000_degs` at the time, so an hour
later it belongs to an unrelated part of the sky, and the plate scale is a configuration
value that has already changed once (MAST_unit#138). A re-correlation is either faithful
to the run it describes or it is worthless, so there are deliberately no overrides.
"""

import json
import math
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from astropy.io import fits

from common.filer import Filer
from common.mast_logging import get_logger
from common.models.statuses import SpiralStepCorrelation
from imaging.frame_shift import MIN_CONFIDENCE, margins_from_fraction, max_reliable_shift, measure_shift

logger = get_logger(__name__)

#: `<date>/FluxMetering/<seq>` -- both are folder names on the share, and both reach this
#: module from a URL. Anchored and narrow so nothing can walk out of the products tree:
#: `..` and separators are excluded by construction rather than by a later check.
DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"
SEQ_PATTERN = r"^\d{1,6}$"

RESULT_FILE = "result.json"


class CorrelationError(Exception):
    """A run that cannot be correlated, with the reason a caller should be shown."""


def _run_root() -> Path:
    """This machine's product tree on the share.

    `Filer().machine.root`, which is documented as already carrying the hostname on both
    platforms. NOT `shared.root`, which is the share ROOT on Linux and would put one
    machine's lookups across every machine's folders -- the mistake that filed 89 nights of
    control's logs at the top of the share.
    """
    return Path(Filer().machine.root)


def run_folder(date: str, seq: str) -> Path:
    if not re.match(DATE_PATTERN, date):
        raise CorrelationError(f"'{date}' is not an observing-night folder name (YYYY-MM-DD)")
    if not re.match(SEQ_PATTERN, seq):
        raise CorrelationError(f"'{seq}' is not a run sequence number")
    return _run_root() / date / "FluxMetering" / seq


def list_runs() -> list[dict[str, Any]]:
    """Every flux-metering run on this machine's share, newest night first.

    The discovery half of the endpoint pair. OpenAPI cannot offer these as a dropdown --
    enum members are fixed at import, the share changes with every run, and `seq` depends
    on `date` in a way OpenAPI has no way to express -- so they are served as data instead.
    """
    root = _run_root()
    runs: list[dict[str, Any]] = []
    if not root.is_dir():
        return runs
    for night in sorted((p for p in root.iterdir() if p.is_dir() and re.match(DATE_PATTERN, p.name)), reverse=True):
        folder = night / "FluxMetering"
        if not folder.is_dir():
            continue
        for run in sorted((p for p in folder.iterdir() if p.is_dir() and re.match(SEQ_PATTERN, p.name)), reverse=True):
            document = _maybe_load(run / RESULT_FILE)
            runs.append(
                {
                    "date": night.name,
                    "seq": run.name,
                    # Reported rather than filtered out: a run with no result.json still has
                    # frames, and a caller deserves to see it and be told why it is refused
                    # rather than to wonder why it is missing from the list.
                    "complete": document is not None and bool(document.get("terminal_state")),
                    "terminal_state": (document or {}).get("terminal_state"),
                    "steps": len((document or {}).get("steps") or []),
                    "started_at": (document or {}).get("started_at"),
                    "ended_at": (document or {}).get("ended_at"),
                }
            )
    return runs


def list_run_steps(date: str, seq: str) -> list[dict[str, Any]]:
    """Each step of one run, with what a caller needs in order to choose two of them.

    Cell, ring, offset and flux, not bare indices: nobody wants to pick "step 7" from a
    list of integers -- they want to see that step 7 was the arg-max at cell (0, 1). This
    is the thing a dropdown could never have shown.
    """
    document = _load_document(date, seq)
    best = document.get("best_index")
    steps = []
    for step in document.get("steps") or []:
        steps.append(
            {
                "index": step.get("index"),
                "cell": step.get("cell"),
                "ring": step.get("ring"),
                "offset_arcsec": step.get("offset_arcsec"),
                "flux": step.get("flux"),
                "imager_frame": step.get("imager_frame"),
                "saturated": step.get("saturated"),
                "argmax": step.get("index") == best,
            }
        )
    return steps


def _maybe_load(path: Path) -> dict[str, Any] | None:
    try:
        with open(path, encoding="utf-8") as fp:
            return json.load(fp)
    except (OSError, ValueError):
        return None


def _load_document(date: str, seq: str) -> dict[str, Any]:
    folder = run_folder(date, seq)
    if not folder.is_dir():
        raise CorrelationError(f"no such run: '{date}/FluxMetering/{seq}'")
    path = folder / RESULT_FILE
    if not path.is_file():
        raise CorrelationError(
            f"'{date}/FluxMetering/{seq}' has no {RESULT_FILE}: the run did not finish, so the parameters "
            "it used are unknown and its steps cannot be correlated faithfully"
        )
    document = _maybe_load(path)
    if document is None:
        raise CorrelationError(f"'{date}/FluxMetering/{seq}/{RESULT_FILE}' is unreadable or is not valid JSON")
    terminal = document.get("terminal_state")
    if not terminal:
        raise CorrelationError(f"'{date}/FluxMetering/{seq}' records no terminal state, so the run did not complete")
    if terminal == "aborted":
        # Refused rather than answered on a best-effort basis. An aborted run stopped
        # mid-walk, so its steps need not be consistent with the parameters recorded beside
        # them, and a plausible number from an inconsistent run is worse than no number.
        raise CorrelationError(f"'{date}/FluxMetering/{seq}' was aborted; its steps are not correlated")
    return document


def _step(document: dict[str, Any], index: int, label: str) -> dict[str, Any]:
    steps = document.get("steps") or []
    match = [s for s in steps if s.get("index") == index]
    if not match:
        available = f"0..{len(steps) - 1}" if steps else "none"
        raise CorrelationError(f"{label}={index} is not a step of this run (available: {available})")
    step = match[0]
    if not step.get("imager_frame"):
        raise CorrelationError(f"{label}={index} recorded no imager frame, so there is nothing to correlate")
    return step


def _read_frame(folder: Path, name: str) -> np.ndarray:
    path = folder / name
    if not path.is_file():
        raise CorrelationError(f"'{name}' is not in the run folder; the products may not have reached the share")
    return np.asarray(fits.getdata(str(path)), dtype=float)


def measure_pair(
    reference: np.ndarray,
    final: np.ndarray,
    *,
    center_x: int,
    center_y: int,
    usable_fraction: float,
    expect_no_motion: bool,
) -> tuple[Any, float, float]:
    """The correlation core, shared with the session's own end-of-run measurement.

    Returns `(shift, magnitude_px, max_reliable_shift_px)`. Extracted so that a run's
    reference-vs-arg-max and this module's step-vs-step take exactly the same path: two
    implementations of one measurement would eventually disagree, and the disagreement
    would show up as a fibre position rather than as an error.
    """
    margin_h, margin_v = margins_from_fraction(reference.shape, usable_fraction)
    shift = measure_shift(
        reference,
        final,
        center_x=center_x,
        center_y=center_y,
        margin_horizontal=margin_h,
        margin_vertical=margin_v,
        expect_no_motion=expect_no_motion,
    )
    return shift, math.hypot(shift.dx, shift.dy), max_reliable_shift(reference.shape, margin_h)


def _commanded_difference(
    document: dict[str, Any], step_a: dict[str, Any], step_b: dict[str, Any]
) -> tuple[tuple[float, float] | None, str]:
    """The a->b commanded offset in pixels, or None and the reason there is none.

    The DIFFERENCE of the two steps' commanded offsets, because that is what the measured
    shift should equal. Reporting one step's own offset would look like a check and be one
    only when the other step was the origin.
    """
    scale = document.get("pixel_scale_at_bin1")
    dec = document.get("dec_degrees")
    if not scale or scale <= 0:
        return None, (
            "the run did not record the plate scale it used; it cannot be taken from the current "
            "configuration, which may have changed since"
        )
    offset_a, offset_b = step_a.get("offset_arcsec"), step_b.get("offset_arcsec")
    if offset_a is None or offset_b is None:
        return None, "one of the steps recorded no commanded offset"
    # cos(dec) on the RA axis only, as in the session: the step is RA COORDINATE arcsec and
    # the sky moves by that times cos(dec). The run's declination, never the mount's now.
    ra_scale = math.cos(math.radians(dec)) if dec is not None else 1.0
    source = "the run's recorded plate scale and declination"
    if dec is None:
        source = "the run's recorded plate scale; it recorded no declination, so cos(dec) was taken as 1"
    return (
        ((offset_b[0] - offset_a[0]) * ra_scale / scale, (offset_b[1] - offset_a[1]) / scale),
        source,
    )


def correlate_steps(date: str, seq: str, step_a: int, step_b: int) -> SpiralStepCorrelation:
    """Correlate the imager frames of two steps of one finished run."""
    document = _load_document(date, seq)
    folder = run_folder(date, seq)

    a, b = _step(document, step_a, "step_a"), _step(document, step_b, "step_b")
    frame_a, frame_b = a["imager_frame"], b["imager_frame"]

    result = document.get("result") or {}
    params = document.get("params") or {}
    usable_fraction = params.get("usable_fraction")
    if not usable_fraction:
        raise CorrelationError("the run recorded no usable_fraction, so the correlation window is unknown")
    center_x, center_y = result.get("fiber_x"), result.get("fiber_y")
    if center_x is None or center_y is None:
        raise CorrelationError("the run recorded no fibre position, so the correlation has no centre")

    # Two steps at the same cell are at the same pointing, so a zero shift is the correct
    # answer and `measure_shift`'s fixed-pattern alarm has to be told so -- the general form
    # of the session's `best.cell == (0, 0)` test.
    cell_a, cell_b = a.get("cell"), b.get("cell")
    expect_no_motion = cell_a is not None and cell_a == cell_b

    shift, magnitude, limit = measure_pair(
        _read_frame(folder, frame_a),
        _read_frame(folder, frame_b),
        center_x=int(center_x),
        center_y=int(center_y),
        usable_fraction=float(usable_fraction),
        expect_no_motion=expect_no_motion,
    )
    commanded, commanded_source = _commanded_difference(document, a, b)

    return SpiralStepCorrelation(
        date=date,
        seq=seq,
        hostname=document.get("hostname"),
        created_at=datetime.now(UTC).isoformat(),
        step_a=step_a,
        step_b=step_b,
        frame_a=frame_a,
        frame_b=frame_b,
        cell_a=_pair(cell_a),
        cell_b=_pair(cell_b),
        ring_a=a.get("ring"),
        ring_b=b.get("ring"),
        offset_arcsec_a=_pair(a.get("offset_arcsec")),
        offset_arcsec_b=_pair(b.get("offset_arcsec")),
        flux_a=a.get("flux"),
        flux_b=b.get("flux"),
        dx=shift.dx,
        dy=shift.dy,
        confidence=shift.confidence,
        at_origin=shift.at_origin,
        low_confidence=shift.confidence < MIN_CONFIDENCE,
        magnitude_px=magnitude,
        max_reliable_shift_px=limit,
        beyond_limit=magnitude > limit,
        commanded_offset_px=commanded,
        commanded_offset_source=commanded_source,
        usable_fraction=float(usable_fraction),
        fiber_x=int(center_x),
        fiber_y=int(center_y),
        fiber_source=result.get("fiber_source"),
        pixel_scale_at_bin1=document.get("pixel_scale_at_bin1"),
        dec_degrees=document.get("dec_degrees"),
    )


def _pair(value) -> tuple | None:
    return tuple(value) if value is not None else None


def correlation_file_name(step_a: int, step_b: int) -> str:
    """Five digits each, so these sort beside the `step-NNNNN-MM.fits` they describe."""
    return f"correlate-{step_a:05d}-{step_b:05d}.json"


def write_correlation(correlation: SpiralStepCorrelation) -> str:
    """Write the product beside the run's own `result.json`, and return where it went.

    Straight to the share, with no ram-disk hop and no `MoveGuardian().protect()`. Both
    exist to keep a mover from taking a file the writer is still producing; here the run
    finished long ago, its inputs are already share-resident, and there is no mover
    involved. Re-running a pair overwrites, which is why `created_at` is recorded.
    """
    folder = run_folder(correlation.date, correlation.seq)
    path = folder / correlation_file_name(correlation.step_a, correlation.step_b)
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(correlation.model_dump(), fp, indent=2, default=str)
    logger.info(f"wrote {path}")
    return os.fspath(path)
