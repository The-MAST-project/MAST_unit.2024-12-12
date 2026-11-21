#!/usr/bin/env python
"""Survey the optical-centre finder over many frames and compare it with the fibre centres in the config DB.

For each frame the survey

  * detects the fold-mirror shadow (`imaging.mirror_shadow`),
  * finds the optical centre from the coma of the stars (`calibration.analysis.optical_center`), twice:
      - `production`: the finder as the calibration runs it (global threshold, nothing masked),
      - `masked`:     the shadow (umbra and penumbra) kept out of detection, thresholds from the local noise,
  * writes `<stem>-optical-center.jpg` in the frame's own folder: the frame, the shadow tinted, the DB fibre
    centre, the centre each variant found, and a legend with the deltas.

and for each unit it pools the frames' sources into one fit (`solve_optical_center` of the whole list), with a
bootstrap over the frames, and compares that with the DB.

Coordinates are 0-based pixels of the full unbinned frame, like the DB's `guiding.rois[fcu_v2].fiber_x/y`.
A delta is always *detected minus DB*, so a positive dx means the coma centre lies to the right of the fibre.

Frames are independent, so `--workers N` runs N at a time. No astrometry.net is needed.

    python tools/optical_center_survey.py --manifest pool_oc.tsv --root /Storage/mast-share/MAST --out out-oc \\
        --workers 16 --fibre-from-config

The manifest is a TSV with the columns `unit` and `path` (the frame's path, relative to --root), and optionally
`fibre_x`, `fibre_y`: the DB fibre pixel in force when the frame was taken. Without a
configuration database, give the fibre pixel with `--fibre UNIT X Y` (repeatable).
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import io
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import numpy as np  # noqa: E402
from astropy.io import fits  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

#: arcseconds per pixel of the full-resolution frame: the solver's 0.5245 arcsec on the 2x2 binned image, halved
PIXEL_SCALE_ARCSEC = 0.5245 / 2
VARIANTS = ("production", "masked")
PICTURE_SUFFIX = "-optical-center.jpg"
#: the bootstrap's resamples of the frames
BOOTSTRAP = 300
#: a slim extraction keeps these, which is everything `solve_optical_center` reads
EXTRACTION_KEYS = ("x", "y", "theta", "ellipticity", "area", "flux", "peak_x", "peak_y", "n_detected", "shape")
MARKER = {"fibre": (255, 255, 255), "production": (255, 140, 0), "masked": (86, 220, 120), "frame": (150, 150, 150)}


# ------------------------------------------------------------------------------------------------- one frame


def _slim(extraction: dict) -> dict:
    """The extraction without its frame-sized arrays, which would cost gigabytes to hold for a whole survey."""
    slim = {key: extraction[key] for key in EXTRACTION_KEYS}
    slim["data_sub"] = None  # only the finder's own plot reads it
    return slim


#: the star-size measure: the half-flux diameter of the brightest isolated, unsaturated sources
HFD_STARS = 60
HFD_APERTURE_PX = 40
HFD_ISOLATION_PX = 90
SATURATION_ADU = 60000


def star_size(extraction: dict | None, raw: np.ndarray) -> tuple[float | None, int]:
    """(median half-flux diameter in pixels, number of stars) of the frame's brightest isolated sources.

    A badly focused frame gives the optical-centre finder a different answer (see the autofocus sweeps), so every
    row carries this number and the analysis can drop the frames that were out of focus. `data_sub` is the
    background-subtracted frame; the flux of each star is summed in a disc of 0.75 * HFD_APERTURE_PX, less the median of the ring outside it, and
    the diameter that holds half of it is reported, so a frame whose stars outgrow the disc reads as about its size.
    """
    if extraction is None or extraction.get("data_sub") is None:
        return None, 0
    from scipy.spatial import cKDTree  # noqa: PLC0415

    data_sub = extraction["data_sub"]
    ny, nx = data_sub.shape
    x, y, flux = extraction["x"], extraction["y"], extraction["flux"]
    if len(x) < 5:
        return None, 0
    tree = cKDTree(np.column_stack([x, y]))
    isolated = np.array([len(tree.query_ball_point([xi, yi], HFD_ISOLATION_PX)) == 1 for xi, yi in zip(x, y, strict=True)])
    margin = HFD_APERTURE_PX + 2  # the stamp is 2 * aperture + 1 wide about the rounded centroid
    inside = (x > margin) & (x < nx - margin) & (y > margin) & (y < ny - margin)
    candidates = np.flatnonzero(isolated & inside & np.isfinite(flux))
    candidates = candidates[np.argsort(flux[candidates])[::-1]][: 3 * HFD_STARS]
    sizes = []
    yy, xx = np.mgrid[-HFD_APERTURE_PX : HFD_APERTURE_PX + 1, -HFD_APERTURE_PX : HFD_APERTURE_PX + 1]
    rr = np.hypot(xx, yy)
    order = np.argsort(rr.ravel())
    core = 0.75 * HFD_APERTURE_PX  # the star's disc; the ring outside it measures the local sky
    for i in candidates:
        cx, cy = int(round(x[i])), int(round(y[i]))
        if raw[cy - 2 : cy + 3, cx - 2 : cx + 3].max() >= SATURATION_ADU:
            continue
        stamp = data_sub[cy - HFD_APERTURE_PX : cy + HFD_APERTURE_PX + 1, cx - HFD_APERTURE_PX : cx + HFD_APERTURE_PX + 1]
        flat = stamp.ravel()[order]
        radius = rr.ravel()[order]
        values = flat[radius <= core] - np.median(flat[(radius > core) & (radius <= HFD_APERTURE_PX)])  # local sky
        total = values.sum()
        if total <= 0:
            continue
        half = np.searchsorted(np.cumsum(values), total / 2)
        sizes.append(2 * radius[radius <= core][min(half, len(values) - 1)])
        if len(sizes) >= HFD_STARS:
            break
    return (float(np.median(sizes)), len(sizes)) if sizes else (None, 0)


def _find(extraction: dict | None) -> tuple[object | None, str]:
    """`solve_optical_center` on one extraction: the result, or None and why. It reports failures by printing."""
    from calibration.analysis.optical_center import solve_optical_center  # noqa: PLC0415

    if extraction is None:
        return None, "no sources detected"
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        result = solve_optical_center([extraction])
    return result, ("" if result is not None else out.getvalue().strip() or "no centre")


def survey_frame(job: dict) -> dict:
    """Everything the survey records about one frame; also writes its picture. Never raises: a frame that fails
    is a row with an `error`."""
    started = time.monotonic()
    row: dict = {"unit": job["unit"], "path": job["path"], "frame": Path(job["path"]).name}
    try:
        from calibration.analysis.optical_center import extract_sources  # noqa: PLC0415
        from imaging.mirror_shadow import detect_mirror_shadow  # noqa: PLC0415

        with fits.open(job["abs_path"]) as hdul:
            data = np.asarray(hdul[0].data, dtype=np.float32)  # type: ignore[union-attr]
        row["shape"] = list(data.shape)

        t = time.monotonic()
        shadow = detect_mirror_shadow(data)
        row["t_shadow"] = round(time.monotonic() - t, 2)
        row["shadow"] = shadow.summary()
        row["shadow_text"] = shadow.describe() if shadow.present else "no shadow found"
        mask = shadow.mask(include_penumbra=True) if shadow.present else None

        extractions: dict[str, dict | None] = {}
        for variant in VARIANTS:
            t = time.monotonic()
            if variant == "production":
                extraction = extract_sources(data)
            else:
                extraction = extract_sources(data, exclude_mask=mask, local_noise=True)
            if variant == "production":
                try:
                    row["hfd"], row["hfd_n"] = star_size(extraction, data)
                except Exception as e:  # noqa: BLE001 -- the star size is an extra; it must not cost the centre
                    row["hfd"], row["hfd_n"], row["hfd_error"] = None, 0, f"{type(e).__name__}: {e}"
            extractions[variant] = None if extraction is None else _slim(extraction)
            result, why = _find(extractions[variant])
            row[variant] = {
                "ok": result is not None,
                "why": why,
                "seconds": round(time.monotonic() - t, 2),
                "n_detected": 0 if extraction is None else int(extraction["n_detected"]),
            }
            if result is not None:
                row[variant].update(
                    x=float(result.center_x),  # type: ignore[attr-defined]
                    y=float(result.center_y),  # type: ignore[attr-defined]
                    n_sources=int(result.n_sources),  # type: ignore[attr-defined]
                    residual_rms=float(result.residual_rms),  # type: ignore[attr-defined]
                    radiality=float(result.radiality),  # type: ignore[attr-defined]
                    radiality_spin1=float(result.radiality_spin1),  # type: ignore[attr-defined]
                    # the sources of the fit, for the picture
                    axes=[np.asarray(v, dtype=float).round(2).tolist() for v in (result.x, result.y, result.theta)],  # type: ignore[attr-defined]
                )
        row["extractions"] = extractions
        fibre = job.get("fibre")
        if fibre is not None:
            row["fibre"] = list(fibre)
            for variant in VARIANTS:
                if row[variant]["ok"]:
                    row[variant]["dx"] = round(row[variant]["x"] - fibre[0], 2)
                    row[variant]["dy"] = round(row[variant]["y"] - fibre[1], 2)
                    row[variant]["distance"] = round(float(np.hypot(row[variant]["dx"], row[variant]["dy"])), 2)
        if job.get("picture", True):
            picture = Path(job["abs_path"]).with_name(Path(job["abs_path"]).stem + PICTURE_SUFFIX)
            if job.get("picture_dir"):
                picture = Path(job["picture_dir"]) / picture.name
            write_picture(picture, data, shadow, row)
            row["picture"] = str(picture)
    except Exception as e:  # noqa: BLE001 -- one frame must not stop the survey
        row["error"] = f"{type(e).__name__}: {e}"
    row["seconds"] = round(time.monotonic() - started, 1)
    return row


# ------------------------------------------------------------------------------------------------- picture


def legend_lines(row: dict) -> list[tuple[str, tuple[int, int, int]]]:
    """The legend: what each marker is, and the deltas, as (text, colour) lines."""
    ny, nx = row["shape"]
    centre = ((nx - 1) / 2, (ny - 1) / 2)
    fibre = row.get("fibre")
    lines: list[tuple[str, tuple[int, int, int]]] = []
    if fibre is not None:
        lines.append((f"DB fibre centre  ({fibre[0]:.0f}, {fibre[1]:.0f})", MARKER["fibre"]))
    else:
        lines.append(("DB fibre centre: not available", MARKER["fibre"]))
    for variant in VARIANTS:
        r = row[variant]
        label = f"{variant:<10}"
        if not r["ok"]:
            lines.append((f"{label} no centre: {r['why'][:70]}", MARKER[variant]))
            continue
        text = f"{label} ({r['x']:.0f}, {r['y']:.0f})"
        if "dx" in r:
            text += (
                f"   delta ({r['dx']:+.0f}, {r['dy']:+.0f}) px  |{r['distance']:.0f}| px = "
                f"{r['distance'] * PIXEL_SCALE_ARCSEC:.0f} arcsec"
            )
        lines.append((text, MARKER[variant]))
        lines.append(
            (
                f"{'':<10} {r['n_sources']} of {r['n_detected']} sources   rms {r['residual_rms']:.0f} px   "
                f"radiality {r['radiality']:.2f}",
                (200, 200, 200),
            )
        )
    if row.get("hfd") is not None:
        lines.append((f"star size  half-flux diameter {row['hfd']:.1f} px  ({row['hfd_n']} stars)", (200, 200, 200)))
    lines.append((f"frame centre  ({centre[0]:.0f}, {centre[1]:.0f})", MARKER["frame"]))
    return lines


def write_picture(path: Path, data: np.ndarray, shadow, row: dict, width: int = 1500) -> None:
    """The frame with the shadow tinted, the DB centre and each variant's centre marked, the coma axes of the fit
    drawn as short ticks, and the legend. A centre outside the frame is drawn on the padded border."""
    from solvers.shadow_picture import _panel  # noqa: PLC0415
    from solvers.star_map import font  # noqa: PLC0415

    ny, nx = data.shape
    rgb = _panel(data, shadow, width)
    factor = max(1, -(-nx // width))
    pad = 150
    legend_height = 24 * (len(legend_lines(row)) + 3) + 12
    canvas = Image.new("RGB", (rgb.shape[1] + 2 * pad, rgb.shape[0] + 2 * pad + legend_height), (28, 28, 28))
    canvas.paste(Image.fromarray(rgb), (pad, pad))
    draw = ImageDraw.Draw(canvas)

    def to_canvas(x: float, y: float) -> tuple[float, float]:
        return pad + x / factor, pad + y / factor

    draw.rectangle([pad - 1, pad - 1, pad + rgb.shape[1], pad + rgb.shape[0]], outline=(90, 90, 90))
    cx, cy = to_canvas((nx - 1) / 2, (ny - 1) / 2)
    draw.line([cx - 14, cy, cx + 14, cy], fill=MARKER["frame"], width=1)
    draw.line([cx, cy - 14, cx, cy + 14], fill=MARKER["frame"], width=1)

    for variant in VARIANTS:  # the coma axes of the sources of each fit: they should point at the centre
        r = row[variant]
        if r["ok"]:
            xs, ys, thetas = r["axes"]
            for x, y, theta in zip(xs, ys, thetas, strict=False):
                dx, dy = np.cos(theta) * 90, np.sin(theta) * 90
                a, b = to_canvas(x - dx, y - dy), to_canvas(x + dx, y + dy)
                draw.line([*a, *b], fill={"production": (170, 100, 40), "masked": (60, 150, 90)}[variant], width=1)

    def cross(x: float, y: float, colour, radius: int, ring: bool) -> None:
        px, py = to_canvas(x, y)
        px, py = min(max(px, 4), canvas.width - 4), min(max(py, 4), pad * 2 + rgb.shape[0] - 4)  # on the canvas
        if ring:
            draw.ellipse([px - radius, py - radius, px + radius, py + radius], outline=colour, width=3)
        draw.line([px - radius - 8, py, px + radius + 8, py], fill=colour, width=3)
        draw.line([px, py - radius - 8, px, py + radius + 8], fill=colour, width=3)

    fibre = row.get("fibre")
    for variant in VARIANTS:
        r = row[variant]
        if r["ok"]:
            if fibre is not None:
                a, b = to_canvas(*fibre), to_canvas(r["x"], r["y"])
                draw.line([*a, *b], fill=MARKER[variant], width=2)
            cross(r["x"], r["y"], MARKER[variant], 14, ring=False)
    if fibre is not None:
        cross(fibre[0], fibre[1], MARKER["fibre"], 22, ring=True)

    small, big = font(15), font(18)
    top = rgb.shape[0] + 2 * pad + 8
    draw.text((8, 6), f"{row['frame']}", fill=(235, 235, 235), font=big)
    draw.text((8, 30), f"{row['unit']}   shadow: {row['shadow_text']}", fill=(200, 200, 200), font=small)
    draw.text((8, 52), "umbra purple, penumbra blue; ticks: the coma axes of the stars of each fit", fill=(160, 160, 160), font=small)
    for i, (text, colour) in enumerate(legend_lines(row)):
        draw.text((8, top + i * 24), text, fill=colour, font=big)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(path, "JPEG", quality=85)
    except OSError as e:
        row["picture_error"] = str(e)


# ------------------------------------------------------------------------------------------------- the survey


def read_manifest(path: Path) -> list[dict]:
    with path.open(newline="") as f:
        return [
            {
                "unit": r["unit"],
                "path": r["path"],
                # the DB fibre pixel in force when the frame was taken, if it differs from the current one
                "fibre": (float(r["fibre_x"]), float(r["fibre_y"])) if r.get("fibre_x") and r.get("fibre_y") else None,
            }
            for r in csv.DictReader(f, delimiter="\t")
        ]


def resolve_fibres(units: set[str], overrides: dict[str, tuple[float, float]], from_config: bool) -> dict[str, tuple[float, float]]:
    """The DB fibre centre of each unit (0-based full-frame pixels): `--fibre` overrides, then the configuration."""
    fibres = dict(overrides)
    if from_config:
        from common.config import Config  # noqa: PLC0415
        from common.config.local import load_local_config  # noqa: PLC0415
        from common.config.rois import FcuVersion  # noqa: PLC0415

        site = load_local_config().site
        for unit in sorted(units - set(fibres)):
            try:
                config = Config().get_unit(site, unit)
                roi = config.guiding.rois[FcuVersion("fcu_v2")]  # type: ignore[union-attr,index]
                fibres[unit] = (float(roi.fiber_x), float(roi.fiber_y))  # type: ignore[union-attr]
            except Exception as e:  # noqa: BLE001
                print(f"no fibre centre for {unit} in the configuration: {e}", file=sys.stderr)
    return fibres


def _pooled(rows: list[dict], variant: str):
    from calibration.analysis.optical_center import solve_optical_center  # noqa: PLC0415

    extractions = [r["extractions"][variant] for r in rows if r.get("extractions", {}).get(variant) is not None]
    return extractions, solve_optical_center(extractions) if extractions else None


def summarise(rows: list[dict], fibres: dict[str, tuple[float, float]], seed: int = 1) -> dict:
    """Per unit and variant: the per-frame centres (median, spread), the pooled fit and its bootstrap, vs the DB."""
    summary: dict = {}
    rng = np.random.default_rng(seed)
    for unit in sorted({r["unit"] for r in rows}):
        unit_rows = [r for r in rows if r["unit"] == unit and "error" not in r]
        # the latest DB value, when the frames carry the one in force at their time (rows sort by night)
        fibre = (unit_rows[-1].get("fibre") if unit_rows else None) or fibres.get(unit)
        entry: dict = {"frames": len(unit_rows), "fibre": list(fibre) if fibre else None}
        for variant in VARIANTS:
            ok = [r for r in unit_rows if r[variant]["ok"]]
            v: dict = {"found": len(ok)}
            if ok:
                xs = np.array([r[variant]["x"] for r in ok])
                ys = np.array([r[variant]["y"] for r in ok])
                v["frame_median"] = [float(np.median(xs)), float(np.median(ys))]
                v["frame_mad"] = [float(1.4826 * np.median(abs(xs - np.median(xs)))), float(1.4826 * np.median(abs(ys - np.median(ys))))]
            with contextlib.redirect_stdout(io.StringIO()):  # the finder reports its failures by printing
                extractions, pooled = _pooled(unit_rows, variant)
            if pooled is not None:
                v["pooled"] = [float(pooled.center_x), float(pooled.center_y)]
                v["pooled_sources"] = int(pooled.n_sources)
                v["pooled_radiality"] = float(pooled.radiality)
                boots = []
                for _ in range(BOOTSTRAP):
                    pick = [extractions[i] for i in rng.integers(0, len(extractions), len(extractions))]
                    from calibration.analysis.optical_center import solve_optical_center  # noqa: PLC0415

                    with contextlib.redirect_stdout(io.StringIO()):
                        b = solve_optical_center(pick)
                    if b is not None:
                        boots.append((b.center_x, b.center_y))
                if len(boots) > 20:
                    b = np.array(boots)
                    v["pooled_bootstrap_p16_p84"] = [np.percentile(b, 16, axis=0).tolist(), np.percentile(b, 84, axis=0).tolist()]
                    v["bootstrap_valid"] = len(boots)
                if fibre:
                    v["pooled_minus_fibre"] = [v["pooled"][0] - fibre[0], v["pooled"][1] - fibre[1]]
                    if "frame_median" in v:
                        v["frame_median_minus_fibre"] = [v["frame_median"][0] - fibre[0], v["frame_median"][1] - fibre[1]]
            entry[variant] = v
        summary[unit] = entry
    return summary


def write_scatter(out: Path, rows: list[dict], fibres: dict, summary: dict) -> list[Path]:
    """One picture per unit: every frame's centre (both variants) around the DB centre, and the pooled fit."""
    import matplotlib  # noqa: PLC0415

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt  # noqa: PLC0415

    written = []
    for unit, entry in summary.items():
        fig, ax = plt.subplots(figsize=(8, 7))
        colours = {"production": "#e69f00", "masked": "#009e73"}
        for variant in VARIANTS:
            pts = [(r[variant]["x"], r[variant]["y"]) for r in rows if r["unit"] == unit and "error" not in r and r[variant]["ok"]]
            if pts:
                a = np.array(pts)
                ax.scatter(a[:, 0], a[:, 1], s=14, color=colours[variant], alpha=0.6, label=f"{variant}: per frame ({len(pts)})")
            pooled = entry[variant].get("pooled")
            if pooled:
                ax.scatter(*pooled, marker="*", s=260, color=colours[variant], edgecolor="black", label=f"{variant}: pooled")
        if entry["fibre"]:
            ax.scatter(*entry["fibre"], marker="+", s=400, color="black", linewidths=2.5, label="DB fibre centre")
        shape = next((r["shape"] for r in rows if r["unit"] == unit and "shape" in r), None)
        if shape:
            ax.scatter((shape[1] - 1) / 2, (shape[0] - 1) / 2, marker="x", s=120, color="grey", label="frame centre")
        ax.set_aspect("equal")
        ax.invert_yaxis()
        ax.set_xlabel("x, full-frame pixels")
        ax.set_ylabel("y, full-frame pixels")
        ax.set_title(f"{unit}: optical centre found, frame by frame, against the DB fibre centre")
        ax.legend(fontsize=8)
        ax.grid(alpha=0.3)
        path = out / f"{unit}-optical-center-scatter.png"
        fig.savefig(path, dpi=110, bbox_inches="tight")
        plt.close(fig)
        written.append(path)
    return written


CSV_FIELDS = ["unit", "night", "frame", "shape", "fibre_x", "fibre_y", "shadow_present", "shadow_depth", "shadow_truncated"]
VARIANT_FIELDS = ["ok", "why", "x", "y", "dx", "dy", "distance", "n_sources", "n_detected", "residual_rms", "radiality", "radiality_spin1", "seconds"]


def write_csv(path: Path, rows: list[dict]) -> None:
    fields = CSV_FIELDS + [f"{v}_{f}" for v in VARIANTS for f in VARIANT_FIELDS] + ["hfd", "hfd_n", "t_shadow", "seconds", "error", "path"]
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fields)
        writer.writeheader()
        for r in rows:
            parts = Path(r["path"]).parts
            out = {
                "unit": r["unit"],
                "night": parts[1] if len(parts) > 1 else "",
                "frame": r["frame"],
                "shape": "x".join(str(v) for v in r.get("shape", [])),
                "fibre_x": (r.get("fibre") or ["", ""])[0],
                "fibre_y": (r.get("fibre") or ["", ""])[1],
                "shadow_present": r.get("shadow", {}).get("present", ""),
                "shadow_depth": r.get("shadow", {}).get("depth", ""),
                "shadow_truncated": r.get("shadow", {}).get("truncated", ""),
                "hfd": "" if r.get("hfd") is None else round(r["hfd"], 2),
                "hfd_n": r.get("hfd_n", ""),
                "t_shadow": r.get("t_shadow", ""),
                "seconds": r.get("seconds", ""),
                "error": r.get("error", ""),
                "path": r["path"],
            }
            for v in VARIANTS:
                for field_name in VARIANT_FIELDS:
                    out[f"{v}_{field_name}"] = r.get(v, {}).get(field_name, "")
            writer.writerow(out)


def format_summary(summary: dict) -> str:
    lines = []
    for unit, e in summary.items():
        fibre = e["fibre"]
        lines.append(f"{unit}: {e['frames']} frames; DB fibre centre {fibre}")
        for variant in VARIANTS:
            v = e[variant]
            lines.append(f"  {variant}: centre found on {v['found']} of {e['frames']} frames")
            if "frame_median" in v:
                lines.append(
                    f"    per frame   median ({v['frame_median'][0]:.0f}, {v['frame_median'][1]:.0f})  spread (MAD) "
                    f"({v['frame_mad'][0]:.0f}, {v['frame_mad'][1]:.0f}) px"
                    + (f"   minus DB ({v['frame_median_minus_fibre'][0]:+.0f}, {v['frame_median_minus_fibre'][1]:+.0f})" if "frame_median_minus_fibre" in v else "")
                )
            if "pooled" in v:
                text = f"    pooled      ({v['pooled'][0]:.0f}, {v['pooled'][1]:.0f}) from {v['pooled_sources']} sources, radiality {v['pooled_radiality']:.2f}"
                if "pooled_bootstrap_p16_p84" in v:
                    lo, hi = v["pooled_bootstrap_p16_p84"]
                    text += f"   68% over frames x [{lo[0]:.0f}, {hi[0]:.0f}] y [{lo[1]:.0f}, {hi[1]:.0f}]"
                if "pooled_minus_fibre" in v:
                    text += f"   minus DB ({v['pooled_minus_fibre'][0]:+.0f}, {v['pooled_minus_fibre'][1]:+.0f}) px"
                lines.append(text)
    return "\n".join(lines)


def _init_worker() -> None:
    for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ[name] = "2"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", type=Path, required=True, help="TSV with the columns unit and path")
    parser.add_argument("--root", type=Path, required=True, help="what the manifest's paths are relative to (the share's MAST folder)")
    parser.add_argument("--out", type=Path, required=True, help="the survey's CSV, summary and scatter pictures")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int, default=None, help="only the first N frames of the manifest")
    parser.add_argument("--fibre", nargs=3, action="append", metavar=("UNIT", "X", "Y"), default=[], help="the DB fibre pixel of a unit")
    parser.add_argument("--fibre-from-config", action="store_true", help="read each unit's fibre pixel from the configuration database")
    parser.add_argument("--no-pictures", action="store_true")
    parser.add_argument("--picture-dir", type=Path, default=None, help="write the pictures here instead of beside each frame")
    args = parser.parse_args(argv)

    jobs = read_manifest(args.manifest)[: args.limit]
    overrides = {u: (float(x), float(y)) for u, x, y in args.fibre}
    fibres = resolve_fibres({j["unit"] for j in jobs}, overrides, args.fibre_from_config)
    for job in jobs:
        job["abs_path"] = str(args.root / job["path"])
        job["fibre"] = job["fibre"] or fibres.get(job["unit"])
        job["picture"] = not args.no_pictures
        job["picture_dir"] = str(args.picture_dir) if args.picture_dir else None
    args.out.mkdir(parents=True, exist_ok=True)
    print(f"{len(jobs)} frames, {args.workers} workers; DB fibre centres: {fibres}", flush=True)

    rows: list[dict] = []
    started = time.monotonic()
    with ProcessPoolExecutor(max_workers=args.workers, initializer=_init_worker) as pool:
        futures = {pool.submit(survey_frame, job): job for job in jobs}
        for done, future in enumerate(as_completed(futures), 1):
            row = future.result()
            rows.append(row)
            print(f"[{done}/{len(jobs)}] {row['unit']} {row['frame'][:60]}  {row['seconds']} s  {row.get('error', '')}", flush=True)
    rows.sort(key=lambda r: (r["unit"], r["path"]))

    summary = summarise(rows, fibres)
    write_csv(args.out / "optical-center.csv", rows)
    (args.out / "optical-center-summary.json").write_text(json.dumps(summary, indent=1))
    text = format_summary(summary)
    (args.out / "optical-center-summary.txt").write_text(text + "\n")
    write_scatter(args.out, rows, fibres, summary)
    print(text)
    print(f"done in {time.monotonic() - started:.0f} s; results in {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
