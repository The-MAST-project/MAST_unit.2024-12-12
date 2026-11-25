"""Compare MastrometryDotNet's original and selective stages on frames that have a stored shadow model.

For each frame whose stage position (``STAGEPOS`` in the FITS header) has a model in the shadow store, the frame is solved
twice by the unit's own solver, whole frame, CRPIX at the fibre:

``original``    the frame at binning 1, downsampled to 2x2, solved as an image: no selection, no shadow handling
``selective``   the stored shadow model, the selection on the 2x2 grid (sep, the window growing from the low-coma radius),
                the xylist solved

A frame without a stored model for its stage position (or without ``STAGEPOS``) is not solved; it is listed in
``compare.csv`` with the reason. Nothing is written beside the frames, and nothing on the share.

Writes, in --out:

- ``compare.csv``   one row per frame: per mode status, seconds, the steps' seconds, stars fed and used, the solution
                    (RA, Dec at the fibre), the residual against the catalogue (rms, median, 3-sigma-clipped rms); and the
                    selective-minus-original position difference (east, north, total; arcsec) and the speed-up
- ``summary.txt``   the run in numbers: frames solved by each mode, position difference (median, rms, max), the
                    residuals, the time per solve and the speed-up. It is also printed.

Accuracy here is two things: how far the selective solution moves from the original's, and how well each fits the
catalogue (the residual of the stars it used). The original's solution is not the truth -- it is the method being replaced --
so the position difference says "how much would change", and the residuals say "which fit is better".

Usage (on a unit, from the unit's venv):
    python tools/compare_solver_modes.py PATH [PATH ...] --out DIR --fibre mast02 [options]

    --fibre UNIT[:FCU] | --fibre-xy X Y   CRPIX at the fibre (as `source_survey.py`)
    --shadow-store FILE      the store (default: this machine's `Calibration/shadow_models.json`)
    --build-store            a stage position with no stored model gets one, detected on the first frame taken there
                             and kept in --out/shadow_models.json (never in the machine's own store), as the unit's
                             `get_shadow` would; that first frame is the warm-up (not counted). Without it such frames
                             are skipped
    --tolerance N            a stored model serves a frame whose STAGEPOS is within N steps of the model's (default 100)
    --hot-pixels FILE|UNIT   fill the hot pixels (selective only; the original is the unit's plain downsample)
    --param NAME=VALUE       override a SelectionParams field for the selective mode (repeatable)
    --repeat N               solve each frame N times per mode and report the median time (default 1)
    --no-warmup              do not solve the first frame once first (the first solve loads the indexes and the modules)
    --limit N                stop after N frames
"""

from __future__ import annotations

import argparse
import csv
import math
import statistics
import sys
import time
from pathlib import Path

from astropy.io import fits

_TOOLS = Path(__file__).resolve().parent
_SRC = _TOOLS.parent / "src"
for _p in (str(_SRC), str(_TOOLS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import source_survey as survey  # noqa: E402

from imaging.shadow_store import ShadowStore, StoredShadow, frame_binning, frame_shape, get_shadow, within  # noqa: E402
from solvers.source_selection import SelectionParams  # noqa: E402
from solvers.survey_solve import SolveOutcome  # noqa: E402

MODES = ("original", "selective")
DEFAULT_TOLERANCE = 100
ARCSEC_PER_DEG = 3600.0


def stage_position(path: Path) -> int | None:
    """The stage position the frame was taken at (``STAGEPOS``), or None if the header has none."""
    try:
        value = fits.getheader(path, 0).get("STAGEPOS")
        return None if value is None else int(value)
    except (OSError, ValueError, TypeError):
        return None


def model_for(store: ShadowStore, path: Path, tolerance: int) -> tuple[StoredShadow | None, str]:
    """The stored model for the frame (shape and stage position), and when there is none, why not."""
    position = stage_position(path)
    if position is None:
        return None, "no STAGEPOS in the header"
    shape = frame_shape(path)
    entry = store.find(position, within(position, tolerance), shape)
    if entry is None:
        return None, f"no stored model for stage position {position} and shape {shape}"
    if frame_binning(path) != entry.binning:
        return None, f"the model is for binning {entry.binning}, the frame's is {frame_binning(path)}"
    return entry, ""


def offset_arcsec(first: tuple[float, float], second: tuple[float, float]) -> tuple[float, float]:
    """``second`` minus ``first`` as (east, north), arcsec; both (RA, Dec) in degrees."""
    d_ra = (second[0] - first[0] + 180.0) % 360.0 - 180.0
    east = d_ra * math.cos(math.radians((first[1] + second[1]) / 2.0)) * ARCSEC_PER_DEG
    north = (second[1] - first[1]) * ARCSEC_PER_DEG
    return east, north


def _solved(outcome: SolveOutcome | None) -> bool:
    return outcome is not None and outcome.status == "ok" and outcome.crval is not None


def _median_run(runs: list[SolveOutcome]) -> SolveOutcome:
    """The run whose time is the median (of the successful ones if any): its solution and its steps go with that time."""
    good = [r for r in runs if r.status == "ok"] or runs
    return sorted(good, key=lambda r: r.seconds)[len(good) // 2]


def solve_modes(path, args, params, out_dir, shadow, repeat) -> dict[str, SolveOutcome]:
    """Both modes on one frame, ``repeat`` times each, the original first in every round."""
    runs: dict[str, list[SolveOutcome]] = {m: [] for m in MODES}
    for _ in range(max(1, repeat)):
        for mode in MODES:
            args.solver_modes = [mode]
            got = survey._solve_via_solver(path, args, params, out_dir, shadow=shadow if mode == "selective" else None)
            runs[mode].append(got[mode])
    return {mode: _median_run(r) for mode, r in runs.items()}


def row_for(path: Path, entry: StoredShadow | None, why: str, outcomes: dict[str, SolveOutcome] | None) -> dict:
    row: dict = {
        "frame": path.name,
        "stage_position": stage_position(path),
        "model_position": entry.stage_position if entry else "",
        "model_derived": entry.derived if entry else "",
        "model_age_days": round(entry.age_days(), 1) if entry else "",
        "skipped": why,
    }
    if outcomes is None:
        return row
    for mode, o in outcomes.items():
        steps = {s: o.timings.get(s, 0.0) for s in survey.TIMED_STEPS}
        row.update(
            {
                f"{mode}_status": o.status,
                f"{mode}_solved_from": o.solved_from,
                f"{mode}_seconds": round(o.seconds, 2),
                **{f"{mode}_t_{s}": round(v, 2) for s, v in steps.items()},
                f"{mode}_t_other": round(o.seconds - sum(steps.values()), 2),
                f"{mode}_fed": o.fed,
                f"{mode}_used": o.used,
                f"{mode}_ra_deg": "" if o.crval is None else round(o.crval[0], 7),
                f"{mode}_dec_deg": "" if o.crval is None else round(o.crval[1], 7),
                f"{mode}_residual_rms_arcsec": "" if o.residual_rms_arcsec is None else round(o.residual_rms_arcsec, 4),
                f"{mode}_residual_median_arcsec": ""
                if o.residual_median_arcsec is None
                else round(o.residual_median_arcsec, 4),
                f"{mode}_residual_clipped_rms_arcsec": ""
                if o.residual_clipped_rms_arcsec is None
                else round(o.residual_clipped_rms_arcsec, 4),
            }
        )
    original, selective = outcomes["original"], outcomes["selective"]
    if _solved(original) and _solved(selective):
        east, north = offset_arcsec(original.crval, selective.crval)  # type: ignore[arg-type]
        row.update(
            {
                "d_east_arcsec": round(east, 4),
                "d_north_arcsec": round(north, 4),
                "d_total_arcsec": round(math.hypot(east, north), 4),
            }
        )
    if original.seconds > 0 and selective.seconds > 0:
        row["speedup"] = round(original.seconds / selective.seconds, 2)
    return row


def _col(rows: list[dict], key: str) -> list[float]:
    return [float(r[key]) for r in rows if r.get(key) not in (None, "")]


def _stat(values: list[float]) -> str:
    if not values:
        return "n/a"
    rms = math.sqrt(sum(v * v for v in values) / len(values))
    return f"median {statistics.median(values):+.4f}  rms {rms:.4f}  max |{max(values, key=abs):+.4f}|  (n={len(values)})"


def summarize(rows: list[dict]) -> str:
    """The run in numbers."""
    tried = [r for r in rows if not r.get("skipped")]
    lines = [f"frames: {len(rows)} offered, {len(tried)} with a stored shadow model, {len(rows) - len(tried)} skipped"]
    for r in rows:
        if r.get("skipped"):
            lines.append(f"  skipped {r['frame']}: {r['skipped']}")
    if not tried:
        return "\n".join(lines)
    solved = {m: [r for r in tried if r.get(f"{m}_status") == "ok"] for m in MODES}
    both = [r for r in tried if "d_total_arcsec" in r]
    lines += [
        "",
        "solved:  " + "   ".join(f"{m} {len(solved[m])}/{len(tried)}" for m in MODES) + f"   both {len(both)}",
        "",
        "selective minus original, the solution at the fibre (arcsec), frames both solved:",
        f"  east   {_stat(_col(both, 'd_east_arcsec'))}",
        f"  north  {_stat(_col(both, 'd_north_arcsec'))}",
        f"  total  median {statistics.median(_col(both, 'd_total_arcsec')):.4f}  max {max(_col(both, 'd_total_arcsec')):.4f}"
        if both
        else "  total  n/a",
        "",
        "fit to the catalogue (arcsec), per mode, frames both solved:",
    ]
    for m in MODES:
        for key, label in (("residual_rms", "rms"), ("residual_clipped_rms", "clipped rms"), ("residual_median", "median")):
            values = _col(both, f"{m}_{key}_arcsec")
            lines.append(
                f"  {m:9s} {label:12s} median {statistics.median(values):.4f}  mean {statistics.fmean(values):.4f}"
                if values
                else f"  {m:9s} {label:12s} n/a"
            )
        used = _col(both, f"{m}_used")
        if used:
            lines.append(
                f"  {m:9s} {'stars used':12s} median {statistics.median(used):.0f}  min {min(used):.0f}  max {max(used):.0f}"
            )
    lines += ["", "time per solve (seconds), frames both solved:"]
    for m in MODES:
        seconds = _col(both, f"{m}_seconds")
        if seconds:
            steps = "  ".join(
                f"{s} {statistics.median(_col(both, f'{m}_t_{s}')):.2f}"
                for s in (*survey.TIMED_STEPS, "other")
                if any(float(r.get(f"{m}_t_{s}", 0) or 0) for r in both)
            )
            lines.append(f"  {m:9s} median {statistics.median(seconds):.2f}  max {max(seconds):.2f}   | {steps}")
    speed = _col(both, "speedup")
    if speed:
        lines.append(
            f"  speed-up (original / selective): median {statistics.median(speed):.2f}x  "
            f"min {min(speed):.2f}x  max {max(speed):.2f}x"
        )
    only_original = [r["frame"] for r in tried if r.get("original_status") == "ok" and r.get("selective_status") != "ok"]
    only_selective = [r["frame"] for r in tried if r.get("selective_status") == "ok" and r.get("original_status") != "ok"]
    if only_original:
        lines += ["", "solved by the original only: " + ", ".join(only_original)]
    if only_selective:
        lines += ["", "solved by the selective only: " + ", ".join(only_selective)]
    return "\n".join(lines)


def write_compare(rows: list[dict], path: Path) -> None:
    """The rows as a CSV whose columns are every key of every row (a skipped frame has few)."""
    names: list[str] = []
    for r in rows:
        names += [k for k in r if k not in names]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=names)
        writer.writeheader()
        writer.writerows(rows)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], fromfile_prefix_chars="@")
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--fibre", metavar="UNIT[:FCU]")
    parser.add_argument("--fibre-xy", nargs=2, type=int, metavar=("X", "Y"))
    parser.add_argument("--shadow-store", type=Path)
    parser.add_argument("--build-store", action="store_true")
    parser.add_argument("--tolerance", type=int, default=DEFAULT_TOLERANCE)
    parser.add_argument("--hot-pixels", metavar="FILE|UNIT")
    parser.add_argument("--param", action="append", metavar="NAME=VALUE")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--no-warmup", action="store_true")
    parser.add_argument("--limit", type=int)
    return parser


def _survey_args(args: argparse.Namespace) -> argparse.Namespace:
    """The `source_survey` arguments `_solve_via_solver` reads, at the survey's own defaults."""
    namespace = survey.build_parser().parse_args(["-", "--out", str(args.out)])
    namespace.hot_pixels = args.hot_pixels
    namespace.fibre = args.fibre
    namespace.fibre_xy = args.fibre_xy
    namespace.hot_pixels_map = survey.resolve_hot_pixels(args.hot_pixels)
    namespace.fibre_pixel = survey.resolve_fibre(namespace)
    return namespace


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    frames = survey.find_frames(args.paths, args.limit)
    if not frames:
        print("no frames found", file=sys.stderr)
        return 1
    if args.shadow_store is not None:
        store = ShadowStore(Path("."))
        store.path = args.shadow_store
    elif args.build_store:
        store = ShadowStore(Path("."))
        store.path = args.out / "shadow_models.json"
    else:
        store = ShadowStore.for_machine()
    if store is None:
        print("no shadow store on this machine: give --shadow-store or --build-store", file=sys.stderr)
        return 2
    if not args.build_store and not store.entries():
        print(f"the shadow store '{store.path}' is empty or unreadable (--build-store makes one)", file=sys.stderr)
        return 2
    try:
        survey_args = _survey_args(args)
        params = SelectionParams(**survey.parse_param_overrides(args.param))
    except (ValueError, OSError, ImportError, TypeError) as e:
        print(f"arguments: {e}", file=sys.stderr)
        return 2
    if survey_args.fibre_pixel is None:
        print("give --fibre or --fibre-xy: the solutions are compared at the fibre", file=sys.stderr)
        return 2
    args.out.mkdir(parents=True, exist_ok=True)
    for frame in frames:
        if args.out.resolve() == frame.resolve().parent:
            print(f"--out must not be the folder of a frame ({frame.parent})", file=sys.stderr)
            return 2

    rows: list[dict] = []
    warmed = args.no_warmup
    for number, frame in enumerate(frames, 1):
        entry, why = model_for(store, frame, args.tolerance)
        if entry is None and args.build_store and stage_position(frame) is not None and "no stored model" in why:
            position = stage_position(frame)
            built = get_shadow(frame, position, within(position, args.tolerance), store=store, phase="comparison")
            print(f"[{number}/{len(frames)}] {frame.name}: shadow model {built.source} for stage position {position}")
            entry, why = model_for(store, frame, args.tolerance)
            if entry is None:
                why = f"no shadow band found at stage position {position}"
        if entry is None:
            print(f"[{number}/{len(frames)}] {frame.name}: skipped ({why})")
            rows.append(row_for(frame, None, why, None))
            continue
        if not warmed:
            started = time.monotonic()
            solve_modes(frame, survey_args, params, args.out / "warmup", entry.model, 1)
            print(f"warm-up solve of {frame.name}: {time.monotonic() - started:.1f} s (not counted)")
            warmed = True
        try:
            outcomes = solve_modes(frame, survey_args, params, args.out, entry.model, args.repeat)
        except Exception as e:  # noqa: BLE001 -- one frame must not end the comparison
            print(f"[{number}/{len(frames)}] {frame.name}: FAILED ({e!r})", file=sys.stderr)
            rows.append(row_for(frame, entry, f"failed: {e!r}", None))
            continue
        row = row_for(frame, entry, "", outcomes)
        rows.append(row)
        print(
            f"[{number}/{len(frames)}] {frame.name}: original {outcomes['original'].status} "
            f"{outcomes['original'].seconds:.1f} s, "
            f"selective {outcomes['selective'].status} {outcomes['selective'].seconds:.1f} s"
            + (f', differ {row["d_total_arcsec"]:.3f}"' if "d_total_arcsec" in row else "")
        )

    write_compare(rows, args.out / "compare.csv")
    text = summarize(rows)
    (args.out / "summary.txt").write_text(text + "\n", encoding="utf-8")
    print("\n" + text)
    return 0 if any(not r.get("skipped") for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
