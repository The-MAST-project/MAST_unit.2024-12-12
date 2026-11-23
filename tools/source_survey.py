"""Survey the source selection on saved frames: what it keeps, what it rejects, and why.

Stage 0 of the source-filtering plan (mast-claude-config, `plans/mastrometry_source_selection_design.md`).
It runs `solvers.source_selection.select_sources` on FITS frames -- on a unit, or on
mast-ns-control against the operational share -- and writes, **only into --out**, for each frame:

- `<stem>-survey.jpg`   the frame with the fold-mirror shadow masked (umbra, penumbra), the sources
                        kept (green circles), the survivors not taken (green dots) and the rejected
                        ones (vermillion crosses, labelled with the reason), and the tested
                        exclusion half-widths drawn across the band
- `<stem>-sources.csv`  every detected source with its measurements and its fate
- `<stem>-solve.txt`    with --solve: the solve-field command and its own output, and `<stem>.xyls`,
                        `.wcs`, `.corr`, the files it was given and left

and, for the whole run:

- `survey.csv`          one row per frame: seeing, shadow, counts per reason, whether repeated runs
                        agreed
- `sweep.csv`           per frame and exclusion half-width: how many usable sources it would remove
- `consistency.csv`     with --same-field: per star of the first frame, in how many frames it was
                        detected, kept and rejected

It writes beside the frames only with --beside-frames (see there). It does not solve unless asked to (--solve); it never
touches a unit or a mount.

Usage:
    python tools/source_survey.py PATH [PATH ...] --out DIR [options]

    PATH   a FITS file, or a folder searched recursively (solved frames, `*,solver=*`, and the
           solver's `downsampled_*` scratch frames are skipped)

    --roi X Y W H            crop first (the spec / sky ROI); coordinates in the CSVs are reported
                             both in the crop and in the full frame
    --no-shadow              the fold mirror is out: do not look for a shadow
    --param NAME=VALUE       override a SelectionParams field (repeatable), e.g. --param min_sep_fwhm=1.5
    --hot-pixels FILE|UNIT   fill this camera's hot pixels before searching: a hot_pixels.json, or a unit whose
                             Calibration folder on the share holds one (tools/build_hot_pixels.py builds it)
    --stretch zscale|asinh   the picture's display scale: zscale (as ds9's scale zscale, reduced by block
                             maximum so that faint stars survive; default) or the star map's asinh
    --shadow-two-sided       require the shadow to be a trough lower than both its shoulders (a partial
                             shadow at the frame's edge is not)
    --shadow-max-center-offset PX   look for the shadow only this close to the frame centre (default 0:
                             anywhere -- it is centred when the mirror is in, and has its edge at the
                             frame's edge at the sky position)
    --search-from fibre|frame  with --via-solver: where the star search grows out from: the fibre pixel (default) or the
                             middle of the frame (as the selection did before 2026-11-23)
    --optical-center X Y     the centre of the star search (full-frame pixels); default the fibre pixel when --fibre or
                             --fibre-xy is given (as the solver searches), else the frame centre
    --low-coma-radius R      likewise, pixels
    --fwhm F                 the seeing in pixels; default measured from the stars
    --full-scale N           the ADC full scale, needed to test saturation on a float frame
    --repeat N               run the selection N times on each frame and check they agree (default 2)
    --sweep W1,W2,...        exclusion half-widths in pixels (default: 0.5, 1, 1.5, 2 x the penumbra)
    --same-field             treat the frames as repeated exposures of one field and match their stars
    --match-radius R         pixels, for --same-field (default 5)
    --limit N                stop after N frames
    --solve                  also solve the kept list with solve-field and mark the stars the solver
                             used (sky-blue rings, radius ~ how far the fibre pointing would move
                             without the star). Needs --index-dir and a solve-field.
    --index-dir D            astrometry.net index files (required with --solve)
    --solve-field PATH       default: solve-field on the PATH (the cygwin one on Windows)
    --pixel-scale S          arcsec / pixel of the frames, unbinned (default 0.2616)
    --bin N                  solve on an N x N binned grid, as the unit does (default 2; 1 = the
                             frame's own pixels)
    --xylist-origin 0|1      1 (default) writes FITS 1-based positions; 0 writes 0-based. Changes the
                             solved WCS by a constant offset, not which stars are used
    --ra DEG --dec DEG       a position hint (default: blind; RA / DEC in the FITS header are used)
    --radius DEG             the hint's search radius (default 2)
    --cpulimit S             solve-field's CPU limit (default 60)
    --no-rejected            do not mark the rejected sources on the picture
    --show-faint             mark the faint rejects too (hundreds of noise blobs; hidden by default)
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import hashlib
import sys
import time
from pathlib import Path

import numpy as np
from astropy.io import fits
from PIL import Image, ImageDraw
from scipy.spatial import cKDTree

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from imaging.hot_pixels import HotPixels, calibration_path  # noqa: E402
from imaging.mirror_shadow import ShadowModel, detect_mirror_shadow  # noqa: E402
from solvers.pixel_grid import roi_center_to_crpix  # noqa: E402
from solvers.shadow_picture import tint, zscale_display  # noqa: E402
from solvers.source_selection import (  # noqa: E402
    KEPT,
    REJECT_REASONS,
    SPARE,
    Selection,
    SelectionParams,
    select_sources,
)
from solvers.star_map import font, stretch  # noqa: E402
from solvers.survey_solve import (  # noqa: E402
    DEFAULT_PIXEL_SCALE_ARCSEC,
    SolveOutcome,
    SolveSettings,
    default_solve_field,
    match_used_to_sources,
    solve_filtered_list,
)

#: Okabe-Ito colours, so the picture does not rest on telling red from green.
KEPT_COLOR = (0, 158, 115)  # bluish green
REJECTED_COLOR = (213, 94, 0)  # vermillion
SWEEP_COLOR = (240, 228, 66)  # yellow
USED_COLOR = (86, 180, 233)  # sky blue: the stars the solver used (as in the star map)
UMBRA_TINT = (204, 121, 167)  # reddish purple
PENUMBRA_TINT = (0, 114, 178)  # blue
CAPTION_HEIGHT = 126
REASON_CODES = {
    "shadow": "SH",
    "saturated": "SA",
    "hot_pixel": "HP",
    "blended": "BL",
    "truncated": "TR",
    "faint": "FA",
    "bad_centroid": "BC",
}
SKIPPED_NAMES = (",solver=", "downsampled_")
#: the steps of a solver solve that are timed (`MastrometryDotNet.timings`); `<stage>_t_other` is the rest
TIMED_STEPS = ("hot_pixels", "shadow", "darken", "select", "solve_field")


# ----- frames ----------------------------------------------------------------------------------


def find_frames(paths: list[Path], limit: int | None = None) -> list[Path]:
    """The FITS frames under `paths`, sorted, without the solver's own products."""
    found: list[Path] = []
    for path in paths:
        if path.is_dir():
            found.extend(sorted(p for p in path.rglob("*.fits") if not any(s in p.name for s in SKIPPED_NAMES)))
        elif path.is_file():
            found.append(path)
        else:
            print(f"not found: {path}", file=sys.stderr)
    return found[:limit] if limit else found


def shadow_for_roi(model: ShadowModel, full_shape: tuple[int, int], roi: tuple[int, int, int, int]) -> ShadowModel:
    """`model`, found on the full frame, re-expressed for the crop `roi` = (x, y, w, h).

    `ShadowModel` measures its perpendicular coordinate from the image centre, so cropping moves the
    origin: `s` and the centreline's `offset` shift by the same amount.
    """
    if not model.present:
        return ShadowModel(present=False, image_shape=(roi[3], roi[2]))
    full_ny, full_nx = full_shape
    x0, y0, w, h = roi
    delta = -(x0 + (w - 1) / 2.0 - (full_nx - 1) / 2.0) * np.sin(model.angle) + (
        y0 + (h - 1) / 2.0 - (full_ny - 1) / 2.0
    ) * np.cos(model.angle)
    return ShadowModel(
        present=True,
        image_shape=(h, w),
        angle=model.angle,
        offset=model.offset - delta,
        umbra_half_width=model.umbra_half_width,
        penumbra_half_width=model.penumbra_half_width,
        depth=model.depth,
        prominence=model.prominence,
        baseline=model.baseline,
        s=None if model.s is None else model.s - delta,
        profile=model.profile,
    )


# ----- consistency -----------------------------------------------------------------------------


def runs_agree(first: Selection, second: Selection) -> bool:
    """Whether two runs of the selection on the same frame gave the same result, exactly."""
    return all(
        np.array_equal(getattr(first, name), getattr(second, name), equal_nan=name != "fate" and name != "all_reasons")
        for name in ("x", "y", "flux", "snr", "peak", "fwhm", "sigma", "fate", "all_reasons")
    )


def sweep_counts(selection: Selection, widths: list[float]) -> list[dict]:
    """For each exclusion half-width: how many sources that pass every *other* test it would remove.

    Computed from the measurements, with no second detection: a source within the width of the
    band's centreline is excluded, whatever its deficit.
    """
    other_tests_ok = np.array(
        [not [r for r in reasons.split("|") if r and r != "shadow"] for reasons in selection.all_reasons]
    )
    distance = np.abs(selection.shadow_distance)
    rows = []
    for width in widths:
        excluded = other_tests_ok & (distance <= width)
        rows.append(
            {
                "half_width_px": width,
                "usable_before_exclusion": int(other_tests_ok.sum()),
                "excluded": int(excluded.sum()),
                "remaining": int(other_tests_ok.sum() - excluded.sum()),
            }
        )
    return rows


def _estimate_offset(reference: np.ndarray, moved: np.ndarray, search: float = 50.0) -> tuple[float, float]:
    """The median displacement from `reference` to `moved` positions, by nearest neighbours, twice."""
    if len(reference) == 0 or len(moved) == 0:
        return 0.0, 0.0
    tree = cKDTree(moved)
    dx = dy = 0.0
    for _ in range(2):
        dist, idx = tree.query(reference + (dx, dy), distance_upper_bound=search)
        ok = np.isfinite(dist)
        if not ok.any():
            break
        shift = np.median(moved[idx[ok]] - reference[ok], axis=0)
        dx, dy = float(shift[0]), float(shift[1])
    return dx, dy


def consistency(selections: list[Selection], radius: float) -> list[dict]:
    """Per source of the first frame: in how many frames it was detected, kept and rejected.

    Frames are aligned to the first by the median displacement of their stars before matching, so a
    small pointing drift between exposures does not read as every star vanishing.
    """
    reference = selections[0]
    ref_xy = np.column_stack([reference.x, reference.y])
    n = len(ref_xy)
    detected = np.zeros(n, int)
    kept = np.zeros(n, int)
    passed = np.zeros(n, int)
    rejected_for: list[dict[str, int]] = [{} for _ in range(n)]
    shifts: list[list[tuple[float, float]]] = [[] for _ in range(n)]
    for selection in selections:
        xy = np.column_stack([selection.x, selection.y])
        if len(xy) == 0:
            continue
        ox, oy = _estimate_offset(ref_xy, xy)
        dist, idx = cKDTree(xy).query(ref_xy + (ox, oy), distance_upper_bound=radius)
        for i in np.flatnonzero(np.isfinite(dist)):
            j = int(idx[i])
            detected[i] += 1
            fate = selection.fate[j]
            kept[i] += fate == KEPT
            passed[i] += fate in (KEPT, SPARE)
            if fate not in (KEPT, SPARE):
                rejected_for[i][fate] = rejected_for[i].get(fate, 0) + 1
            shifts[i].append((xy[j, 0] - ox - ref_xy[i, 0], xy[j, 1] - oy - ref_xy[i, 1]))
    rows = []
    for i in range(n):
        s = np.array(shifts[i]) if shifts[i] else np.zeros((0, 2))
        rows.append(
            {
                "x": round(float(ref_xy[i, 0]), 2),
                "y": round(float(ref_xy[i, 1]), 2),
                "frames": len(selections),
                "detected": int(detected[i]),
                "not_rejected": int(passed[i]),
                "kept": int(kept[i]),
                "rejected_for": ";".join(f"{k}={v}" for k, v in sorted(rejected_for[i].items())),
                "position_scatter_px": round(float(np.hypot(*s.std(axis=0))), 3) if len(s) > 1 else "",
            }
        )
    return rows


# ----- the picture -----------------------------------------------------------------------------


def draw_overlay(
    image: np.ndarray,
    selection: Selection,
    shadow: ShadowModel | None,
    sweep_widths: list[float],
    caption: list[str],
    out_path: Path,
    mark_rejected: bool = True,
    show_faint: bool = False,
    solve: SolveOutcome | None = None,
    display: str = "zscale",
) -> None:
    """The frame with the shadow masked and the kept, spare and rejected sources marked."""
    gray, factor = zscale_display(image) if display == "zscale" else stretch(image)
    rgb = np.stack([gray, gray, gray], axis=-1)
    ny, nx = image.shape

    if shadow is not None and shadow.present:
        # at display resolution: the display pixel (i, j) stands for the full-resolution pixel at its centre
        xs = (np.arange(rgb.shape[1]) * factor + (factor - 1) / 2.0)[None, :]
        ys = (np.arange(rgb.shape[0]) * factor + (factor - 1) / 2.0)[:, None]
        s = -(xs - (nx - 1) / 2.0) * np.sin(shadow.angle) + (ys - (ny - 1) / 2.0) * np.cos(shadow.angle)
        umbra, penumbra = shadow.bands(s)
        tint(rgb, penumbra, PENUMBRA_TINT, 0.30)
        tint(rgb, umbra, UMBRA_TINT, 0.40)

    canvas = Image.new("RGB", (rgb.shape[1], rgb.shape[0] + CAPTION_HEIGHT), (0, 0, 0))
    canvas.paste(Image.fromarray(rgb), (0, CAPTION_HEIGHT))
    draw = ImageDraw.Draw(canvas)

    def to_display(x: float, y: float) -> tuple[float, float]:
        # 0-based pixel centres; the display grid is `factor` times coarser
        return (x + 0.5) / factor, (y + 0.5) / factor + CAPTION_HEIGHT

    if shadow is not None and shadow.present and sweep_widths:
        label_font = font(13)
        sin_a, cos_a = np.sin(shadow.angle), np.cos(shadow.angle)
        normal = np.array([-sin_a, cos_a])  # the perpendicular coordinate runs along this
        along = np.array([np.cos(shadow.angle), np.sin(shadow.angle)])
        centre = np.array([(nx - 1) / 2.0, (ny - 1) / 2.0])
        reach = float(np.hypot(nx, ny))
        for width in sweep_widths:
            for sign in (-1, 1):
                point = centre + (shadow.offset + sign * width) * normal
                a, b = point - reach * along, point + reach * along
                draw.line([to_display(*a), to_display(*b)], fill=SWEEP_COLOR, width=1)
            label_at = centre + (shadow.offset + width) * normal - 0.45 * ny * along
            draw.text(to_display(*label_at), f"{width:.0f}", fill=SWEEP_COLOR, font=label_font)

    code_font = font(12)
    for i in range(selection.n_detected):
        if not (np.isfinite(selection.x[i]) and np.isfinite(selection.y[i])):
            continue
        cx, cy = to_display(selection.x[i], selection.y[i])
        fate = selection.fate[i]
        if fate == KEPT:
            draw.ellipse([cx - 11, cy - 11, cx + 11, cy + 11], outline=KEPT_COLOR, width=2)
        elif fate == SPARE:
            draw.ellipse([cx - 3, cy - 3, cx + 3, cy + 3], fill=KEPT_COLOR)
        elif mark_rejected and (fate != "faint" or show_faint):
            draw.line([(cx - 6, cy - 6), (cx + 6, cy + 6)], fill=REJECTED_COLOR, width=2)
            draw.line([(cx - 6, cy + 6), (cx + 6, cy - 6)], fill=REJECTED_COLOR, width=2)
            draw.text((cx + 8, cy - 7), REASON_CODES.get(fate, fate[:2].upper()), fill=REJECTED_COLOR, font=code_font)

    if solve is not None and solve.used:
        shifts = solve.fibre_shift_arcsec
        relative = (
            shifts / float(np.max(shifts)) if shifts is not None and float(np.max(shifts)) > 0 else np.full(solve.used, 0.3)
        )
        for k in range(solve.used):
            cx, cy = to_display(solve.used_x[k], solve.used_y[k])
            radius = 15 + 12 * relative[k]
            draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius], outline=USED_COLOR, width=2)
        if shifts is not None:
            label_font = font(13)
            for k in np.argsort(shifts)[::-1][:3]:
                cx, cy = to_display(solve.used_x[k], solve.used_y[k])
                draw.text((cx + 15 + 12 * relative[k] + 3, cy - 8), f'{shifts[k]:.3f}"', fill=USED_COLOR, font=label_font)

    caption_font = font(15)
    for row, line in enumerate(caption):
        draw.text((8, 4 + row * 17), line, fill=(235, 235, 235), font=caption_font)
    canvas.save(out_path, "JPEG", quality=85)


# ----- one frame, and the run ------------------------------------------------------------------

SOURCE_COLUMNS = (
    "index",
    "fate",
    "reasons",
    "x",
    "y",
    "x_full",
    "y_full",
    "x_first_moment",
    "y_first_moment",
    "flux",
    "snr",
    "peak",
    "fwhm",
    "area",
    "radius",
    "shadow_deficit",
    "shadow_distance",
    "sigma",
    "used_by_solver",
    "solver_weight",
    "solver_fibre_shift_arcsec",
)


def _round(value: float, digits: int = 3):
    return "" if not np.isfinite(value) else round(float(value), digits)


def write_sources(selection: Selection, origin: tuple[int, int], path: Path, solve: SolveOutcome | None = None) -> None:
    used_index = (
        match_used_to_sources(solve.used_x, solve.used_y, selection.x, selection.y)
        if solve is not None and solve.used
        else np.full(selection.n_detected, -1)
    )
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(SOURCE_COLUMNS)
        for i in range(selection.n_detected):
            writer.writerow(
                [
                    i,
                    selection.fate[i],
                    selection.all_reasons[i],
                    _round(selection.x[i]),
                    _round(selection.y[i]),
                    _round(selection.x[i] + origin[0]),
                    _round(selection.y[i] + origin[1]),
                    _round(selection.x_first_moment[i]),
                    _round(selection.y_first_moment[i]),
                    _round(selection.flux[i], 1),
                    _round(selection.snr[i], 1),
                    _round(selection.peak[i], 1),
                    _round(selection.fwhm[i], 2),
                    int(selection.area[i]),
                    _round(selection.radius[i], 1),
                    _round(selection.shadow_deficit[i], 4),
                    _round(selection.shadow_distance[i], 1),
                    _round(selection.sigma[i], 4),
                    ("" if solve is None else (used_index[i] >= 0)),
                    ""
                    if solve is None or used_index[i] < 0 or solve.weight is None
                    else _round(solve.weight[used_index[i]], 4),
                    ""
                    if solve is None or used_index[i] < 0 or solve.fibre_shift_arcsec is None
                    else _round(solve.fibre_shift_arcsec[used_index[i]], 4),
                ]
            )


def write_rows(rows: list[dict], path: Path) -> None:
    if not rows:
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def survey_frame(
    path: Path, args: argparse.Namespace, params: SelectionParams, out_dir: Path
) -> tuple[dict, Selection, list[dict]]:
    """Run the selection on one frame, and write its picture and its sources. Returns the summary row,
    the selection, and the sweep rows."""
    full = np.asarray(fits.getdata(path))
    header = fits.getheader(path)
    hot = args.hot_pixels_map
    hot_filled = 0
    if hot is not None:
        if hot.matches(full.shape):
            full, hot_filled = hot.fill(full), len(hot)
        else:
            print(
                f"{path.name}: hot pixel map is for {hot.image_shape}, the frame is {full.shape}: not applied",
                file=sys.stderr,
            )
    full_shape = full.shape
    roi = tuple(args.roi) if args.roi else None
    x0, y0 = (roi[0], roi[1]) if roi else (0, 0)
    data = full[y0 : y0 + roi[3], x0 : x0 + roi[2]] if roi else full

    shadow = None
    if not args.no_shadow:
        found = detect_mirror_shadow(
            full,
            max_center_offset=args.shadow_max_center_offset or None,
            require_two_sided=args.shadow_two_sided,
        )
        shadow = shadow_for_roi(found, full_shape, roi) if roi else found

    center = None
    if args.optical_center:
        center = (args.optical_center[0] - x0, args.optical_center[1] - y0)
    elif args.fibre_pixel is not None:
        center = (args.fibre_pixel[0] - x0, args.fibre_pixel[1] - y0)  # the search grows out from the fibre pixel

    runs = [
        select_sources(
            data,
            shadow=shadow,
            optical_center=center,
            low_coma_radius=args.low_coma_radius,
            params=params,
            header=header,
        )
        for _ in range(max(1, args.repeat))
    ]
    selection = runs[0]
    identical = all(runs_agree(selection, other) for other in runs[1:])

    widths = args.sweep
    if widths is None and shadow is not None and shadow.present:
        widths = [round(f * shadow.penumbra_half_width) for f in (0.5, 1.0, 1.5, 2.0)]
    widths = widths or []
    sweep = (
        [{"frame": path.name, **row} for row in sweep_counts(selection, widths)]
        if shadow is not None and shadow.present
        else []
    )

    solve: SolveOutcome | None = None
    via: dict[str, SolveOutcome] = {}
    if args.via_solver:
        via = _solve_via_solver(path, args, params, out_dir)
        solve = via.get("selective")  # the picture's second set of ticks: what the xylist solve used
    elif args.solve:
        solve = _solve(selection, data.shape, path.stem, args, out_dir, (x0, y0))

    counts = selection.counts()
    shadow_text = (
        f"shadow: depth {shadow.depth:.2f}, {shadow.describe()}"
        if shadow is not None and shadow.present
        else ("shadow: none found" if shadow is not None else "shadow: not looked for (mirror out)")
    )
    rejects = "  ".join(f"{REASON_CODES[r]} {counts[r]}" for r in REJECT_REASONS if counts[r])
    caption = [
        f"{path.name}   {data.shape[1]}x{data.shape[0]}" + (f"   roi {roi}" if roi else ""),
        f"detected {counts['detected']}   kept {counts['kept']} (green o)   spare {counts['spare']} (green .)   "
        f"fit order {selection.fit_order}   {selection.status}",
        f"rejected (red x, faint ones not drawn): {rejects or 'none'}",
        f"{shadow_text}   seeing {selection.fwhm_seeing_px:.1f} px   noise {selection.noise_px:.1f}",
        f"repeat x{len(runs)}: {'identical' if identical else 'DIFFERENT'}   "
        f"snr cut {'relaxed' if selection.snr_relaxed else selection.params.min_snr}   "
        f"saturation {'tested' if selection.saturation_tested else 'NOT tested (no full scale)'}",
    ]
    if solve is not None:
        caption.append(_solve_caption(solve))
    stem = path.stem
    draw_overlay(
        data,
        selection,
        shadow,
        widths,
        caption,
        out_dir / f"{stem}-survey.jpg",
        mark_rejected=not args.no_rejected,
        show_faint=args.show_faint,
        solve=solve,
        display=args.stretch,
    )
    write_sources(selection, (x0, y0), out_dir / f"{stem}-sources.csv", solve)
    if args.beside_frames:
        place_beside_frame(path, out_dir, args.solver_modes if args.via_solver else [], out_dir / f"{stem}-survey.jpg")

    row = {
        "frame": path.name,
        "width": data.shape[1],
        "height": data.shape[0],
        "shadow_present": bool(shadow is not None and shadow.present),
        "shadow_depth": _round(shadow.depth) if shadow is not None and shadow.present else "",
        "shadow_offset_px": _round(shadow.offset, 1) if shadow is not None and shadow.present else "",
        "shadow_umbra_half_px": _round(shadow.umbra_half_width, 1) if shadow is not None and shadow.present else "",
        "shadow_penumbra_half_px": _round(shadow.penumbra_half_width, 1) if shadow is not None and shadow.present else "",
        "shadow_truncated": bool(shadow is not None and shadow.present and shadow.truncated),
        "seeing_fwhm_px": _round(selection.fwhm_seeing_px, 2),
        "noise": _round(selection.noise_px, 2),
        **counts,
        "status": selection.status,
        "fit_order": selection.fit_order if selection.fit_order is not None else "",
        "radius_used_px": _round(selection.radius_used_px, 1),
        "snr_relaxed": selection.snr_relaxed,
        "saturation_tested": selection.saturation_tested,
        "repeat_runs": len(runs),
        "repeat_identical": identical,
        **({"fibre_x": args.fibre_pixel[0], "fibre_y": args.fibre_pixel[1]} if args.fibre_pixel is not None else {}),
        **({"hot_pixels_filled": hot_filled} if args.hot_pixels_map is not None else {}),
    }
    if solve is not None:
        row.update(
            {
                "solve_status": solve.status,
                "solve_seconds": _round(solve.seconds, 2),
                "fed": solve.fed,
                "used": solve.used,
                "solve_residual_rms_arcsec": ""
                if solve.residual_rms_arcsec is None
                else _round(solve.residual_rms_arcsec, 4),
                "solve_residual_median_arcsec": ""
                if solve.residual_median_arcsec is None
                else _round(solve.residual_median_arcsec, 4),
                "solve_crval_ra_deg": "" if solve.crval is None else _round(solve.crval[0], 7),
                "solve_crval_dec_deg": "" if solve.crval is None else _round(solve.crval[1], 7),
            }
        )
    for mode, outcome in via.items():
        row.update(
            {
                f"{mode}_status": outcome.status,
                f"{mode}_solved_from": outcome.solved_from,
                f"{mode}_seconds": _round(outcome.seconds, 2),
                **{f"{mode}_t_{step}": _round(outcome.timings.get(step, 0.0), 2) for step in TIMED_STEPS},
                f"{mode}_t_other": _round(outcome.seconds - sum(outcome.timings.get(step, 0.0) for step in TIMED_STEPS), 2),
                f"{mode}_used": outcome.used,
                f"{mode}_crval_ra_deg": "" if outcome.crval is None else _round(outcome.crval[0], 7),
                f"{mode}_crval_dec_deg": "" if outcome.crval is None else _round(outcome.crval[1], 7),
                f"{mode}_residual_rms_arcsec": ""
                if outcome.residual_rms_arcsec is None
                else _round(outcome.residual_rms_arcsec, 4),
                f"{mode}_residual_median_arcsec": ""
                if outcome.residual_median_arcsec is None
                else _round(outcome.residual_median_arcsec, 4),
                f"{mode}_residual_clipped_rms_arcsec": ""
                if outcome.residual_clipped_rms_arcsec is None
                else _round(outcome.residual_clipped_rms_arcsec, 4),
            }
        )
    return row, selection, sweep


_SOLVER = None  # one MastrometryDotNet for the run: it checks the RAM disk and the indexes once


#: the small artifacts of a stage's solve that go beside the frame: the star map, its data, the result text, the shadow
#: picture. Not the solved FITS (as large as the frame), the xylist, WCS or correspondences: those stay in --out.
BESIDE_FRAME_SUFFIXES = ("-stars.jpg", "-stars.json", "-result.txt", "-shadow.jpg")


def frame_folder_tag(path: Path) -> str:
    """The short name a frame's per-stage folders in --out carry."""
    return hashlib.sha1(path.name.encode()).hexdigest()[:8]


def place_beside_frame(path: Path, out_dir: Path, modes: list[str], survey_jpg: Path) -> list[Path]:
    """Copy a frame's pictures to the frame's own folder (on the share), and return what was placed.

    The survey's picture goes as it is (`<stem>-survey.jpg`). A stage's artifacts carry the stage in their name,
    `<stem>,solver=mastrometry.net,stage=<stage>-stars.jpg`, so the three stages do not overwrite one another, nor the
    production solver's `<stem>,solver=mastrometry.net...` files already there. All contain `,solver=` or `-survey`,
    so a survey of the folder does not take them for frames. A copy that fails is reported and does not stop the run.
    """
    import re  # noqa: PLC0415
    import shutil  # noqa: PLC0415

    placed: list[Path] = []
    jobs: list[tuple[Path, str]] = [(survey_jpg, survey_jpg.name)]
    tag = frame_folder_tag(path)
    for mode in modes:
        for source in sorted((out_dir / f"{tag}-{mode}").glob("*")):
            if source.name.endswith(BESIDE_FRAME_SUFFIXES):
                jobs.append(
                    (source, re.sub(r"(,solver=[^,]*?)(-(?:stars|result|shadow)\.)", rf"\1,stage={mode}\2", source.name))
                )
    for source, name in jobs:
        try:
            if source.exists():
                shutil.copyfile(source, path.parent / name)
                placed.append(path.parent / name)
        except OSError as e:
            print(f"could not place {name} beside {path.name}: {e}", file=sys.stderr)
    return placed


def _solve_via_solver(
    path: Path, args: argparse.Namespace, params: SelectionParams, out_dir: Path
) -> dict[str, SolveOutcome]:
    """Solve the frame with the unit's own `MastrometryDotNet`, once per mastrometry stage in `--solver-modes`
    (original, shadowed, selective).

    The frame is only read; each mode's artifacts (result text, solved FITS, correspondences, xylist,
    WCS, star map) are put in `<out>/<hash of the frame name>-<mode>/`, never beside the frame and never on the share.
    The frame is solved whole: the phase is neither `sky` nor `spec`, so the solver does not crop.
    """
    from solvers.selection_stage import SourceSelectionSettings  # noqa: PLC0415 -- needs the unit's configuration

    global _SOLVER
    if _SOLVER is None:
        from solvers.mastrometry import MastrometryDotNet  # noqa: PLC0415

        _SOLVER = MastrometryDotNet()
    # A short folder name: the solver's artifacts are named after the (long) frame, and Windows without
    # long paths refuses a destination past 260 characters. `<hash of the frame name>-<mode>`.
    tag = frame_folder_tag(path)
    outcomes: dict[str, SolveOutcome] = {}
    for mode in args.solver_modes:
        folder = out_dir / f"{tag}-{mode}"
        settings = SourceSelectionSettings(
            mode=mode,  # type: ignore[arg-type]
            params=params,
            xylist_origin=args.xylist_origin,
            shadow_max_center_offset=args.shadow_max_center_offset or None,
            shadow_require_two_sided=args.shadow_two_sided,
            hot_pixels=args.hot_pixels_map,
            search_from=args.search_from,
        )
        outcome = SolveOutcome(status="ok")
        outcome.solved_from = "image"
        start = time.monotonic()
        try:
            result = _SOLVER.solve(
                unit=None,
                phase="full",  # type: ignore[arg-type]
                full_frame_input_image_path=str(path),
                source_selection=settings,
                artifacts_dir=folder,
                fibre_xy=args.fibre_pixel,
            )
        except Exception as e:  # noqa: BLE001 -- one mode failing must not end the survey of the frame
            outcome.status = f"failed: {e!r}"
            result = None
        outcome.seconds = time.monotonic() - start
        outcome.timings = dict(getattr(_SOLVER, "timings", {}))
        if result is not None:
            if result.selection:
                outcome.solved_from = result.selection.get("solved_from", "image")
                outcome.fed = (result.selection.get("counts") or {}).get("kept", 0)
            if not result.succeeded:
                outcome.status = "failed: " + "; ".join(result.errors or ["not solved"])
            else:
                _read_back_artifacts(outcome, folder, args)
        outcomes[mode] = outcome
    return outcomes


def _read_back_artifacts(outcome: SolveOutcome, folder: Path, args: argparse.Namespace) -> None:
    """The used stars and the solution of a `MastrometryDotNet` solve, from the files it left in `folder`."""
    from solvers.star_map import read_matched  # noqa: PLC0415
    from solvers.survey_solve import SolveSettings as _Settings  # noqa: PLC0415
    from solvers.survey_solve import _read_back  # noqa: PLC0415

    solved = next(iter(sorted(folder.glob("*,solver=*.fits"))), None)
    corr = solved.with_suffix(".corr") if solved is not None else None
    matched = read_matched(corr) if corr is not None else None
    if solved is None or matched is None or len(matched.x) == 0:
        outcome.status = "failed: the solve left no solved FITS or correspondences"
        return
    _read_back(outcome, matched, solved, _Settings(index_dir="", factor=args.bin, origin=args.xylist_origin))


def _solve(
    selection: Selection,
    shape: tuple[int, int],
    stem: str,
    args: argparse.Namespace,
    out_dir: Path,
    origin: tuple[int, int] = (0, 0),
) -> SolveOutcome:
    """Solve the kept list of one frame, apart from the picture: the files it leaves are the survey's."""
    log_path = out_dir / f"{stem}-solve.txt"
    if selection.status != "ok" or selection.fit_order is None:
        outcome = SolveOutcome(status=f"skipped: {selection.status}", fed=int(selection.kept.sum()), log_path=log_path)
        log_path.write_text(f"not solved: {selection.status} ({int(selection.kept.sum())} kept)\n", encoding="utf-8")
        return outcome
    settings = SolveSettings(
        index_dir=args.index_dir,
        solve_field=args.solve_field or default_solve_field(),
        pixel_scale_arcsec=args.pixel_scale,
        factor=args.bin,
        origin=args.xylist_origin,
        cpulimit=args.cpulimit,
        ra_deg=args.ra,
        dec_deg=args.dec,
        radius_deg=args.radius,
        # CRPIX at the fibre: its 0-based pixel in the full frame, less the crop's origin, on the binned grid
        crpix=None
        if args.fibre_pixel is None
        else (
            roi_center_to_crpix(args.fibre_pixel[0], origin[0], args.bin),
            roi_center_to_crpix(args.fibre_pixel[1], origin[1], args.bin),
        ),
    )
    return solve_filtered_list(
        selection.xylist(), shape, selection.fit_order, settings, out_dir / f"{stem}-solve-files", log_path, stem
    )


def _solve_caption(solve: SolveOutcome) -> str:
    if solve.status != "ok":
        return f"solve: {solve.status}"
    rms = (
        ""
        if solve.residual_rms_arcsec is None
        else f'   residual rms {solve.residual_rms_arcsec:.3f}" median {solve.residual_median_arcsec or 0.0:.3f}"'
    )
    return f"solve: fed {solve.fed} (green o)   used {solve.used} (blue o){rms}   {solve.seconds:.1f} s"


def resolve_fibre(args: argparse.Namespace) -> tuple[int, int] | None:
    """The fibre's pixel (0-based, full unbinned frame), from --fibre-xy or from the unit's configuration.

    The FITS headers do not hold it (they carry the mount's RA/Dec, the camera and its temperatures); the unit's
    `guiding.rois[<fcu version>]` does. `--fibre mast01` reads it from the configuration database, so it needs a
    MAST machine; `--fibre mast01:fcu_v1` picks the coupling version (default fcu_v2, as the solver assumes).
    """
    if args.fibre_xy:
        return int(args.fibre_xy[0]), int(args.fibre_xy[1])
    if not args.fibre:
        return None
    unit, _, version = args.fibre.partition(":")
    from common.config import Config  # noqa: PLC0415 -- the unit's configuration, on a MAST machine
    from common.config.local import load_local_config  # noqa: PLC0415
    from common.config.rois import FcuVersion  # noqa: PLC0415

    config = Config().get_unit(load_local_config().site, unit)
    if config is None:
        raise ValueError(f"no configuration for unit '{unit}'")
    rois = config.guiding.rois
    key = FcuVersion(version or "fcu_v2")
    if key not in rois:
        raise ValueError(f"unit '{unit}' has no guiding ROI for {key} (it has {[str(k) for k in rois]})")
    return int(rois[key].fiber_x), int(rois[key].fiber_y)  # type: ignore[union-attr]


def resolve_hot_pixels(value: str | None) -> HotPixels | None:
    """The hot pixel map named by `--hot-pixels`: a `.json` file, or a unit whose `Calibration` folder on the share
    holds `hot_pixels.json` (the share is this machine's `Filer().share_root`)."""
    if not value:
        return None
    if value.lower().endswith(".json") or "/" in value or "\\" in value:
        return HotPixels.load(Path(value))
    from common.filer import Filer  # noqa: PLC0415 -- this machine's view of the share

    root = Filer().share_root
    if root is None:
        raise ValueError("no share root on this machine: give --hot-pixels a .json file")
    return HotPixels.load(calibration_path(Path(root.root) / value))


def parse_param_overrides(items: list[str] | None) -> dict[str, object]:
    """`--param name=value` overrides of `SelectionParams`, each value read as the field's own type."""
    valid = {f.name: f for f in dataclasses.fields(SelectionParams)}
    out: dict[str, object] = {}
    for item in items or []:
        name, sep, text = item.partition("=")
        if not sep or name not in valid:
            raise ValueError(f"'{item}' is not name=value of a SelectionParams field ({', '.join(valid)})")
        default = getattr(SelectionParams(), name)
        if isinstance(default, bool):
            out[name] = text.lower() in ("1", "true", "yes", "on")
        elif isinstance(default, tuple):
            out[name] = tuple(int(v) for v in text.split(","))
        elif isinstance(default, int):
            out[name] = int(text)
        else:
            out[name] = None if text.lower() == "none" else float(text)
    return out


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--roi", nargs=4, type=int, metavar=("X", "Y", "W", "H"))
    parser.set_defaults(hot_pixels_map=None)  # set by main() from --hot-pixels
    parser.set_defaults(fibre_pixel=None)  # set by main() from --fibre / --fibre-xy
    parser.add_argument("--no-shadow", action="store_true")
    parser.add_argument(
        "--hot-pixels",
        metavar="FILE|UNIT",
        help="fill this camera's hot pixels before searching: a hot_pixels.json file, or a unit whose "
        "Calibration folder on the share holds one (see tools/build_hot_pixels.py)",
    )
    parser.add_argument(
        "--fibre",
        metavar="UNIT[:FCU]",
        help="solve with CRPIX at this unit's fibre pixel, read from the configuration database (a MAST machine); "
        "FCU defaults to fcu_v2. The solved RA/Dec is then the sky at the fibre",
    )
    parser.add_argument(
        "--fibre-xy",
        nargs=2,
        type=int,
        metavar=("X", "Y"),
        help="solve with CRPIX at this pixel (0-based, full unbinned frame, as in the unit's guiding ROI)",
    )
    parser.add_argument(
        "--param",
        action="append",
        metavar="NAME=VALUE",
        help="override a SelectionParams field, e.g. --param min_sep_fwhm=1.5 (repeatable)",
    )
    parser.add_argument(
        "--stretch",
        choices=("zscale", "asinh"),
        default="zscale",
        help="the picture's display scale: zscale (as ds9, block maximum; default) or the star map's asinh",
    )
    parser.add_argument(
        "--shadow-two-sided",
        action="store_true",
        help="require the shadow to be a trough lower than both its shoulders (a partial shadow at the edge fails)",
    )
    parser.add_argument(
        "--shadow-max-center-offset",
        type=float,
        default=0.0,
        help="look for the shadow only this close to the frame centre, pixels (default 0: anywhere; it is centred "
        "when the mirror is in, and has its edge at the frame's edge at the sky position)",
    )
    parser.add_argument("--optical-center", nargs=2, type=float, metavar=("X", "Y"))
    parser.add_argument("--search-from", choices=("fibre", "frame"), default="fibre")
    parser.add_argument("--low-coma-radius", type=float)
    parser.add_argument("--fwhm", type=float)
    parser.add_argument("--full-scale", type=float)
    parser.add_argument("--repeat", type=int, default=2)
    parser.add_argument("--sweep", type=lambda s: [float(v) for v in s.split(",")])
    parser.add_argument("--same-field", action="store_true")
    parser.add_argument("--match-radius", type=float, default=5.0)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--solve", action="store_true")
    parser.add_argument(
        "--via-solver",
        action="store_true",
        help="solve with the unit's MastrometryDotNet (needs the unit's configuration and RAM disk) instead of "
        "--solve's own solve-field; see --solver-modes",
    )
    parser.add_argument(
        "--solver-modes",
        type=lambda s: s.split(","),
        default=["original", "shadowed", "selective"],
        help="with --via-solver: the mastrometry stages to solve each frame in (default: original,shadowed,selective)",
    )
    parser.add_argument("--index-dir")
    parser.add_argument("--solve-field")
    parser.add_argument("--pixel-scale", type=float, default=DEFAULT_PIXEL_SCALE_ARCSEC)
    parser.add_argument("--bin", type=int, default=2)
    parser.add_argument("--xylist-origin", type=int, choices=(0, 1), default=1)
    parser.add_argument("--ra", type=float)
    parser.add_argument("--dec", type=float)
    parser.add_argument("--radius", type=float, default=2.0)
    parser.add_argument("--cpulimit", type=int, default=60)
    parser.add_argument("--no-rejected", action="store_true")
    parser.add_argument(
        "--beside-frames",
        action="store_true",
        help="also copy each frame's pictures (the survey picture, and per stage the star map, its data, the result "
        "text and the shadow picture) to the frame's own folder, named `...,solver=mastrometry.net,stage=<stage>-...`. "
        "This writes on the share, where the frames are; the big files stay in --out",
    )
    parser.add_argument("--show-faint", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    frames = find_frames(args.paths, args.limit)
    if not frames:
        print("no frames found", file=sys.stderr)
        return 1
    if args.via_solver and args.roi:
        print("--via-solver solves the whole frame: it cannot be combined with --roi", file=sys.stderr)
        return 2
    if args.solve and not args.via_solver and not args.index_dir:
        print("--solve needs --index-dir (the astrometry.net index files)", file=sys.stderr)
        return 2
    out_dir: Path = args.out
    for frame in frames:
        if not args.beside_frames and out_dir.resolve() == frame.resolve().parent:
            print(
                f"--out must not be the folder of a frame ({frame.parent}): the survey writes nothing beside the frames",
                file=sys.stderr,
            )
            return 2
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        args.hot_pixels_map = resolve_hot_pixels(args.hot_pixels)
    except (ValueError, OSError, ImportError) as e:
        print(f"--hot-pixels: {e}", file=sys.stderr)
        return 2
    if args.hot_pixels_map is not None:
        print(f"hot pixels: {len(args.hot_pixels_map)} will be filled (map for {args.hot_pixels_map.image_shape})")
    try:
        args.fibre_pixel = resolve_fibre(args)
    except (ValueError, OSError, ImportError) as e:
        print(f"--fibre: {e}", file=sys.stderr)
        return 2
    if args.fibre_pixel is not None:
        print(f"solving with CRPIX at the fibre pixel {args.fibre_pixel} (0-based, full frame)")
    try:
        params = SelectionParams(seeing_fwhm_px=args.fwhm, full_scale=args.full_scale, **parse_param_overrides(args.param))
    except (TypeError, ValueError) as e:
        print(f"--param: {e}", file=sys.stderr)
        return 2
    summary: list[dict] = []
    sweeps: list[dict] = []
    selections: list[Selection] = []
    for number, frame in enumerate(frames, 1):
        try:
            row, selection, sweep = survey_frame(frame, args, params, out_dir)
        except Exception as e:  # noqa: BLE001 -- one bad frame must not end a survey of a night
            print(f"[{number}/{len(frames)}] {frame.name}: FAILED ({e!r})", file=sys.stderr)
            continue
        summary.append(row)
        sweeps.extend(sweep)
        selections.append(selection)
        print(
            f"[{number}/{len(frames)}] {frame.name}: detected {row['detected']}, kept {row['kept']}, "
            f"spare {row['spare']}, rejected {row['detected'] - row['kept'] - row['spare']}"
            f"{'' if row['repeat_identical'] else '  REPEATS DIFFER'}"
        )

    write_rows(summary, out_dir / "survey.csv")
    write_rows(sweeps, out_dir / "sweep.csv")
    if args.same_field and len(selections) > 1:
        write_rows(consistency(selections, args.match_radius), out_dir / "consistency.csv")
    print(f"wrote {len(summary)} frame(s) to {out_dir}")
    return 0 if summary else 1


if __name__ == "__main__":
    raise SystemExit(main())
