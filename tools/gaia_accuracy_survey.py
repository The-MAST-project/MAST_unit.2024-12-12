"""Accuracy of the solved WCS against Gaia, mode against mode, near the fibre: the survey's ``analyse`` step.

Plan: ``mastrometry_gaia_accuracy_survey.md`` in mast-claude-config. The question is whether
``solving.source_selection.mode = selective`` puts the solved position at the fibre closer to the truth than
``original``. Only the fibre and its neighbourhood matter, in the acquisition phase (the fold mirror out of the frame).

What it does, per frame, offline, from what ``tools/source_survey.py --via-solver`` left in a frame's folder:

1. Takes the clean stars of the selection pass (fate ``kept`` or ``spare``) at their fixed-window centroid (``x_full``,
   ``y_full``): the estimator closest to astrometry.net's own extractor, ``image2xy`` (about 0.1 arcsec near the fibre).
2. Drops every star within ``--exclude-px`` binned pixels of a star ANY of the modes' fits used (the ``.corr`` files), so
   no mode is scored on a star it fitted: the **held-out stars**.
3. Puts each held-out star on the sky through each mode's solved WCS (SIP, on the 2x2-binned grid), and matches it to
   Gaia DR3 -- proper motion applied to the frame's epoch -- on the mean of the modes' positions, so none is favoured.
   A match needs exactly one Gaia star within ``--isolate-arcsec``, the nearest within ``--match-arcsec``, and G of
   ``--max-g`` or brighter.
4. The error of a mode is its WCS sky position minus the Gaia position, east and north, in arcsec.

The metric is the median error VECTOR of the held-out stars within a distance of the fibre pixel (a systematic offset),
not a scatter.

Layout it reads: a folder per frame (a single-frame survey run), holding ``*sources.csv`` and, for each mode, a folder
``<tag>-<mode>/`` with the solved FITS ``*,solver=*.fits`` and its ``.corr`` beside it. ``DIR`` is a frame folder, or a
folder of them.

    python tools/gaia_accuracy_survey.py analyse DIR [DIR ...] --fibre-xy X Y --out OUT
        [--modes original,selective] [--bins 300,500,800,1200,1800] [--near-px 600] [--max-g 16.5]
        [--match-arcsec 2.5] [--isolate-arcsec 4] [--exclude-px 1.5] [--catalog-root R] [--catalog-name N]

It writes into OUT ``stars.csv`` (every held-out matched star and its error), ``bins.csv`` (the offset by distance from
the fibre) and ``frames.csv`` (per frame, near the fibre), and prints the same. It
reads the catalog from the share (the unit default: ``\\\\last0\\catalogs\\GAIA\\DR3``), so run it on a unit or on
mast-ns-control. It changes nothing but OUT.
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import sys
import warnings
from collections.abc import Callable, Sequence
from pathlib import Path

import numpy as np
from astropy.io import fits
from astropy.time import Time, TimeDelta
from astropy.wcs import WCS
from scipy.spatial import cKDTree

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from solvers.pixel_grid import orig_to_grid_fits  # noqa: E402

#: The columns of the survey's ``*sources.csv`` that hold a star's fixed-window centroid (full frame, 0-based).
X_COLUMN, Y_COLUMN = "x_full", "y_full"
#: The fates of the selection pass that are clean, isolated point sources.
CLEAN_FATES = ("kept", "spare")
DEFAULT_MODES = ("original", "selective")
DEFAULT_BINS = (300.0, 500.0, 800.0, 1200.0, 1800.0)
BOOTSTRAPS = 1000

#: ``reference(ra_deg, dec_deg, radius_deg, years_since_catalog_epoch)`` -> ``(ra_deg, dec_deg, G)`` arrays.
Reference = Callable[[float, float, float, float], tuple[np.ndarray, np.ndarray, np.ndarray]]


@dataclasses.dataclass
class Params:
    fibre_xy: tuple[float, float]
    max_g: float = 16.5
    match_arcsec: float = 2.5
    isolate_arcsec: float = 4.0
    faintest_g: float = 18.0
    exclude_px: float = 1.5  # binned pixels
    factor: int = 2  # the solve grid's binning
    xylist_origin: int = 1  # the convention of the .corr's field_x / field_y
    catalog_epoch: float = 2016.0  # Julian year of the catalog's positions (Gaia DR3)


@dataclasses.dataclass
class Frame:
    name: str
    jyear: float
    x: np.ndarray  # full-frame 0-based pixels of the clean stars
    y: np.ndarray
    wcs: dict[str, WCS]  # per mode
    used: dict[str, np.ndarray]  # per mode: (n, 2) field_x, field_y of the stars its fit used, FITS 1-based


@dataclasses.dataclass
class StarError:
    frame: str
    mode: str
    x: float
    y: float
    r_fibre: float
    err_e: float
    err_n: float
    g: float


# ------------------------------------------------------------------------------------------------ reading a frame


def find_frame_folders(paths: Sequence[Path]) -> list[Path]:
    """The frame folders under ``paths``: one that holds a ``*sources.csv`` is itself a frame, else its children are."""
    found: list[Path] = []
    for path in paths:
        if any(path.glob("*sources.csv")):
            found.append(path)
        else:
            found.extend(sorted(c for c in path.iterdir() if c.is_dir() and any(c.glob("*sources.csv"))))
    return found


def _solved_fits(mode_folder: Path) -> Path | None:
    return next(iter(sorted(mode_folder.glob("*,solver=*.fits"))), None)


def _mid_exposure_jyear(header: fits.Header) -> float:
    start = Time(header["DATE-OBS"], scale="utc")
    seconds = float(header.get("EXPOSURE", header.get("EXPTIME", 0.0)) or 0.0)
    return float((start + TimeDelta(seconds / 2.0, format="sec")).jyear)


def load_frame(folder: Path, modes: Sequence[str], params: Params) -> Frame | None:
    """One frame's data, or None (with the reason on stderr) when a mode is missing or the frame has no clean stars."""
    sources = next(iter(sorted(folder.glob("*sources.csv"))), None)
    if sources is None:
        return None
    with open(sources, newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r["fate"] in CLEAN_FATES]
    if not rows:
        print(f"{folder.name}: no clean stars", file=sys.stderr)
        return None
    wcs: dict[str, WCS] = {}
    used: dict[str, np.ndarray] = {}
    jyear = float("nan")
    shift = 1 - params.xylist_origin  # corr positions to FITS 1-based
    for mode in modes:
        mode_folder = next(iter(sorted(folder.glob(f"*-{mode}"))), None)
        solved = _solved_fits(mode_folder) if mode_folder else None
        if solved is None:
            print(f"{folder.name}: no solved FITS for '{mode}'", file=sys.stderr)
            return None
        header = fits.getheader(solved, 0)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            wcs[mode] = WCS(header)
        if not wcs[mode].has_celestial:
            print(f"{folder.name}: '{mode}' has no WCS", file=sys.stderr)
            return None
        corr = solved.with_suffix(".corr")
        if not corr.exists():
            print(f"{folder.name}: no .corr for '{mode}'", file=sys.stderr)
            return None
        with fits.open(corr) as hdus:
            table = hdus[1].data  # type: ignore[union-attr]
        used[mode] = np.column_stack([np.asarray(table["field_x"], float), np.asarray(table["field_y"], float)]) + shift
        if np.isnan(jyear):
            jyear = _mid_exposure_jyear(header)
    xs = np.array([float(r[X_COLUMN]) for r in rows])
    ys = np.array([float(r[Y_COLUMN]) for r in rows])
    return Frame(folder.name, jyear, xs, ys, wcs, used)


# ------------------------------------------------------------------------------------------------ scoring


def to_grid(x, y, factor: int) -> tuple[np.ndarray, np.ndarray]:
    """Full-frame 0-based pixels to the solve grid's FITS 1-based pixels."""
    return (
        np.array([orig_to_grid_fits(v + 1.0, factor) for v in np.asarray(x, float)]),
        np.array([orig_to_grid_fits(v + 1.0, factor) for v in np.asarray(y, float)]),
    )


def _unit_vectors(ra_deg, dec_deg) -> np.ndarray:
    ra, dec = np.radians(ra_deg), np.radians(dec_deg)
    return np.column_stack([np.cos(dec) * np.cos(ra), np.cos(dec) * np.sin(ra), np.sin(dec)])


def _angular_radius_deg(ra_deg, dec_deg, ra0_deg: float, dec0_deg: float) -> float:
    """The largest angular distance of (ra, dec) from (ra0, dec0), degrees."""
    cosine = _unit_vectors(ra_deg, dec_deg) @ _unit_vectors(ra0_deg, dec0_deg)[0]
    return float(np.degrees(np.max(np.arccos(np.clip(cosine, -1.0, 1.0)))))


def _chord(arcsec: float) -> float:
    return 2.0 * np.sin(np.radians(arcsec / 3600.0) / 2.0)


def offsets_arcsec(ra_deg, dec_deg, ra0_deg, dec0_deg) -> tuple[np.ndarray, np.ndarray]:
    """East and north of (ra, dec) from (ra0, dec0), arcsec; east carries cos(dec0), RA wraps."""
    dra = (np.asarray(ra_deg, float) - np.asarray(ra0_deg, float) + 180.0) % 360.0 - 180.0
    return dra * np.cos(np.radians(dec0_deg)) * 3600.0, (np.asarray(dec_deg, float) - np.asarray(dec0_deg, float)) * 3600.0


def held_out(gx: np.ndarray, gy: np.ndarray, used: Sequence[np.ndarray], exclude_px: float) -> np.ndarray:
    """True for the stars farther than ``exclude_px`` (binned, on the solve grid) from every star any fit used."""
    free = np.ones(len(gx), bool)
    pts = np.column_stack([gx, gy])
    for u in used:
        if len(u):
            dist, _ = cKDTree(np.asarray(u, float)).query(pts)
            free &= dist > exclude_px
    return free


def match_gaia(
    ra_deg: np.ndarray,
    dec_deg: np.ndarray,
    gaia_ra: np.ndarray,
    gaia_dec: np.ndarray,
    gaia_g: np.ndarray,
    params: Params,
) -> list[tuple[int, int]]:
    """``(star index, Gaia index)`` for the stars with one Gaia neighbour within ``isolate_arcsec``, the nearest within
    ``match_arcsec`` and bright enough (G <= ``max_g``)."""
    if len(gaia_ra) == 0 or len(ra_deg) == 0:
        return []
    tree = cKDTree(_unit_vectors(gaia_ra, gaia_dec))
    pts = _unit_vectors(ra_deg, dec_deg)
    near = tree.query_ball_point(pts, _chord(params.isolate_arcsec))
    pairs = []
    for i, found in enumerate(near):
        if len(found) != 1:
            continue
        j = int(found[0])
        d = float(np.linalg.norm(pts[i] - tree.data[j]))
        if d <= _chord(params.match_arcsec) and gaia_g[j] <= params.max_g:
            pairs.append((i, j))
    return pairs


def score_frame(frame: Frame, reference: Reference, params: Params) -> list[StarError]:
    """The held-out stars of one frame, matched, with each mode's error vector."""
    modes = list(frame.wcs)
    gx, gy = to_grid(frame.x, frame.y, params.factor)
    free = held_out(gx, gy, list(frame.used.values()), params.exclude_px)
    if not free.any():
        return []
    gx, gy = gx[free], gy[free]
    x, y = frame.x[free], frame.y[free]
    sky = {m: frame.wcs[m].all_pix2world(gx, gy, 1) for m in modes}
    # The mean position of the modes, as offsets from the first (no RA wrap trouble): matching favours none of them.
    first_ra, first_dec = sky[modes[0]]
    de = np.mean([offsets_arcsec(sky[m][0], sky[m][1], first_ra, first_dec)[0] for m in modes], axis=0)
    dn = np.mean([offsets_arcsec(sky[m][0], sky[m][1], first_ra, first_dec)[1] for m in modes], axis=0)
    mean_dec = first_dec + dn / 3600.0
    mean_ra = first_ra + de / 3600.0 / np.cos(np.radians(mean_dec))
    centre_ra, centre_dec = float(np.median(mean_ra)), float(np.median(mean_dec))
    radius = _angular_radius_deg(mean_ra, mean_dec, centre_ra, centre_dec) + 0.02
    gaia_ra, gaia_dec, gaia_g = reference(centre_ra, centre_dec, radius, frame.jyear - params.catalog_epoch)
    pairs = match_gaia(mean_ra, mean_dec, np.asarray(gaia_ra), np.asarray(gaia_dec), np.asarray(gaia_g), params)
    out: list[StarError] = []
    for i, j in pairs:
        r_fibre = float(np.hypot(x[i] - params.fibre_xy[0], y[i] - params.fibre_xy[1]))
        for m in modes:
            e, n = offsets_arcsec(sky[m][0][i], sky[m][1][i], gaia_ra[j], gaia_dec[j])
            out.append(StarError(frame.name, m, float(x[i]), float(y[i]), r_fibre, float(e), float(n), float(gaia_g[j])))
    return out


# ------------------------------------------------------------------------------------------------ summaries


def _median_vector(e: np.ndarray, n: np.ndarray) -> tuple[float, float, float]:
    me, mn = float(np.median(e)), float(np.median(n))
    return me, mn, float(np.hypot(me, mn))


def bin_summary(errors: Sequence[StarError], modes: Sequence[str], edges: Sequence[float], seed: int = 0) -> list[dict]:
    """The offset by distance from the fibre, per mode: the median error vector of the pooled stars, its standard error
    treating stars as independent, and its spread over bootstrap draws of FRAMES (stars of one frame move together)."""
    rng = np.random.default_rng(seed)
    rows = []
    bounds = [0.0, *edges]
    for lo, hi in zip(bounds[:-1], bounds[1:], strict=False):
        for mode in modes:
            sel = [s for s in errors if s.mode == mode and lo <= s.r_fibre < hi]
            if len(sel) < 3:
                continue
            e = np.array([s.err_e for s in sel])
            n = np.array([s.err_n for s in sel])
            frames = sorted({s.frame for s in sel})
            me, mn, mag = _median_vector(e, n)
            se = 1.253 * float(np.hypot(e.std(), n.std())) / np.sqrt(2.0) / np.sqrt(len(sel))
            by_frame = {f: (e[[s.frame == f for s in sel]], n[[s.frame == f for s in sel]]) for f in frames}
            mags = []
            for _ in range(BOOTSTRAPS if len(frames) > 1 else 0):
                pick = rng.integers(0, len(frames), len(frames))
                be = np.concatenate([by_frame[frames[k]][0] for k in pick])
                bn = np.concatenate([by_frame[frames[k]][1] for k in pick])
                mags.append(_median_vector(be, bn)[2])
            rows.append(
                {
                    "r_lo_px": lo,
                    "r_hi_px": hi,
                    "mode": mode,
                    "stars": len(sel),
                    "frames": len(frames),
                    "offset_e_arcsec": me,
                    "offset_n_arcsec": mn,
                    "offset_arcsec": mag,
                    "se_stars_arcsec": se,
                    "se_frames_arcsec": float(np.std(mags)) if mags else float("nan"),
                    "median_abs_error_arcsec": float(np.median(np.hypot(e, n))),
                }
            )
    return rows


def frame_summary(errors: Sequence[StarError], modes: Sequence[str], near_px: float, min_stars: int = 4) -> list[dict]:
    """Per frame, the median error vector of the stars within ``near_px`` of the fibre, per mode."""
    rows = []
    for frame in sorted({s.frame for s in errors}):
        row: dict = {"frame": frame}
        ok = True
        for mode in modes:
            sel = [s for s in errors if s.frame == frame and s.mode == mode and s.r_fibre < near_px]
            if len(sel) < min_stars:
                ok = False
                break
            me, mn, mag = _median_vector(np.array([s.err_e for s in sel]), np.array([s.err_n for s in sel]))
            row.update({f"{mode}_stars": len(sel), f"{mode}_e": me, f"{mode}_n": mn, f"{mode}_offset": mag})
        if ok:
            rows.append(row)
    return rows


def wins(rows: Sequence[dict], modes: Sequence[str]) -> tuple[int, int, dict[str, float]]:
    """Frames where the LAST mode is closer to Gaia than the FIRST, the frame count, and each mode's median offset."""
    if not rows:
        return 0, 0, {}
    first, last = modes[0], modes[-1]
    return (
        sum(1 for r in rows if r[f"{last}_offset"] < r[f"{first}_offset"]),
        len(rows),
        {m: float(np.median([r[f"{m}_offset"] for r in rows])) for m in modes},
    )


# ------------------------------------------------------------------------------------------------ reference, output, CLI


def catalog_reference(catalog_root: str | None, catalog_name: str | None, faintest_g: float) -> Reference:
    """Gaia from the catsHTM files, proper motion applied: the same query ``focus_on_star`` makes for its field."""
    from catalogs.catshtm import CatsHtm  # noqa: PLC0415 -- needs h5py and the catalog share
    from common.config.calibration import FocusOnStarSettings  # noqa: PLC0415
    from focus_on_star.star_selection import field_catalog  # noqa: PLC0415

    defaults = FocusOnStarSettings()
    catalog = CatsHtm(catalog_root or defaults.catalog_root, catalog_name or defaults.catalog_name)

    def reference(ra: float, dec: float, radius: float, years: float):
        return field_catalog(
            catalog,
            ra_deg=ra,
            dec_deg=dec,
            radius_deg=radius,
            mag_column=defaults.mag_column,
            faintest_mag=faintest_g,
            years_of_proper_motion=years,
        )

    return reference


def _write_csv(path: Path, rows: Sequence[dict]) -> None:
    if not rows:
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def report(bins: Sequence[dict], frames: Sequence[dict], modes: Sequence[str], near_px: float) -> str:
    lines = [
        "== held-out stars, by distance from the fibre",
        "distance from fibre | stars (frames) | " + " | ".join(f"{m}: offset E,N |.| +-se(frames)" for m in modes),
    ]
    by_bin: dict[tuple[float, float], dict[str, dict]] = {}
    for r in bins:
        by_bin.setdefault((r["r_lo_px"], r["r_hi_px"]), {})[r["mode"]] = r
    for (lo, hi), per_mode in sorted(by_bin.items()):
        if not all(m in per_mode for m in modes):
            continue
        cells = []
        for m in modes:
            r = per_mode[m]
            vector = f"({r['offset_e_arcsec']:+.2f},{r['offset_n_arcsec']:+.2f})"
            cells.append(f"{vector} {r['offset_arcsec']:.2f} +-{r['se_frames_arcsec']:.2f}")
        first = per_mode[modes[0]]
        lines.append(f"{lo:5.0f}-{hi:5.0f} px | {first['stars']:5d} ({first['frames']:2d}) | " + " | ".join(cells))
    won, total, medians = wins(frames, modes)
    if total:
        closer = f"'{modes[-1]}' is closer to Gaia than '{modes[0]}' in {won} of {total} frames"
        lines.append(
            f"stars within {near_px:.0f} px, per frame: {closer}; median offset "
            + ", ".join(f"{m} {v:.2f}" for m, v in medians.items())
        )
    return "\n".join(lines)


def analyse(
    folders: Sequence[Path],
    out: Path,
    params: Params,
    reference: Reference,
    modes: Sequence[str] = DEFAULT_MODES,
    edges: Sequence[float] = DEFAULT_BINS,
    near_px: float = 600.0,
) -> str:
    """Score every frame, write the CSVs into ``out``, and return the printed report."""
    out.mkdir(parents=True, exist_ok=True)
    frames = [f for f in (load_frame(p, modes, params) for p in find_frame_folders(folders)) if f is not None]
    errors: list[StarError] = []
    for frame in frames:
        errors.extend(score_frame(frame, reference, params))
    _write_csv(out / "stars.csv", [dataclasses.asdict(s) for s in errors])
    bins = bin_summary(errors, modes, edges)
    per_frame = frame_summary(errors, modes, near_px)
    _write_csv(out / "bins.csv", bins)
    _write_csv(out / "frames.csv", per_frame)
    return report(bins, per_frame, modes, near_px)


def _parse(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(__doc__ or "").split("\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    a = sub.add_parser("analyse", help="score the solved WCS of each mode against Gaia near the fibre")
    a.add_argument("dirs", nargs="+", type=Path, help="a frame folder, or a folder of them")
    a.add_argument("--out", type=Path, required=True)
    a.add_argument(
        "--fibre-xy", type=float, nargs=2, required=True, metavar=("X", "Y"), help="the fibre pixel, 0-based, full frame"
    )
    a.add_argument("--modes", default=",".join(DEFAULT_MODES), help="comma-separated; the last is compared with the first")
    a.add_argument("--bins", default=",".join(f"{e:g}" for e in DEFAULT_BINS), help="outer edges of the distance bins, px")
    a.add_argument(
        "--near-px", type=float, default=600.0, help="the per-frame comparison uses the stars within this distance"
    )
    a.add_argument("--max-g", type=float, default=16.5)
    a.add_argument("--match-arcsec", type=float, default=2.5)
    a.add_argument("--isolate-arcsec", type=float, default=4.0)
    a.add_argument("--exclude-px", type=float, default=1.5, help="binned pixels around a star a fit used")
    a.add_argument("--faintest-g", type=float, default=18.0)
    a.add_argument("--bin", type=int, default=2, help="the solve grid's binning (default 2)")
    a.add_argument("--xylist-origin", type=int, choices=(0, 1), default=1)
    a.add_argument("--catalog-root")
    a.add_argument("--catalog-name")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None, reference: Reference | None = None) -> int:
    args = _parse(argv)
    modes = tuple(m.strip() for m in args.modes.split(",") if m.strip())
    if len(modes) < 2:
        print("--modes needs at least two modes", file=sys.stderr)
        return 2
    params = Params(
        fibre_xy=(args.fibre_xy[0], args.fibre_xy[1]),
        max_g=args.max_g,
        match_arcsec=args.match_arcsec,
        isolate_arcsec=args.isolate_arcsec,
        faintest_g=args.faintest_g,
        exclude_px=args.exclude_px,
        factor=args.bin,
        xylist_origin=args.xylist_origin,
    )
    ref = reference or catalog_reference(args.catalog_root, args.catalog_name, params.faintest_g)
    text = analyse(
        args.dirs,
        args.out,
        params,
        ref,
        modes=modes,
        edges=tuple(float(e) for e in args.bins.split(",")),
        near_px=args.near_px,
    )
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
