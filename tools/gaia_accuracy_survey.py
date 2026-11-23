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

``plan`` (the survey's first step, before any telescope time) chooses the targets for a night from the Gaia catalog:

    python tools/gaia_accuracy_survey.py plan --out OUT [--n 20] [--when 2026-11-24T20:00:00Z] [--lat D --lon D]
        [--min-g 8 --max-g 11] [--max-pm 10] [--isolation-arcsec 30] [--min-alt 35 --max-alt 80] [--search-deg 4]

It spreads the targets over altitude and azimuth at ``--when`` (farthest-point sampling of the sky cells, so the
first targets are the most different), and in each cell takes the Gaia DR3 star that is G within ``--min-g`` to
``--max-g``, has a measured proper motion no larger than ``--max-pm``, has **no catalog star of any brightness down to
G 18 within ``--isolation-arcsec``**, and lies nearest the cell's centre. Positions are Gaia's with proper motion
applied to ``--when``. It writes ``targets.csv`` into OUT (in azimuth order, one sweep) and prints it. The site is the
``--lat``/``--lon`` given, else this machine's configured site. It reads the same catalog share as ``analyse``.

``acquire`` (on the control machine or any host that can reach the unit) runs one mastrometry acquisition per target:

    python tools/gaia_accuracy_survey.py acquire TARGETS_CSV --unit mast02 --out OUT
        [--seconds 5] [--timeout 900] [--only T03,T04] [--redo] [--mode LABEL]

For each row of ``targets.csv`` it calls the unit's ``start_acquisition_and_guiding`` (target = the Gaia position,
``handover_automatically_to_guider`` off, so the unit is left unguided), waits for the unit's
``latest.acquisition`` to end, and writes ``OUT/<name>-<mode>/acquire.json``: the target row, the unit and mode, the
request, how the acquisition ended (state, errors, each phase's last offset against tolerance), where its products are
on the share, and the mount's own RA/Dec afterwards. A target that already has its ``acquire.json`` is skipped
(``--redo`` runs it again), so an interrupted night resumes. A target that fails is recorded and the run goes on.

There is **one acquisition per target**, in whatever mode the unit is configured for (``--mode`` is only a label for
the sidecar). Both modes are scored afterwards, offline, on that acquisition's last frame -- ``solve`` then ``pointing``
below -- so only the solver differs between them.

``solve`` runs ``tools/source_survey.py --via-solver`` on each acquired target's last mirror-out frame, in both modes:

    python tools/gaia_accuracy_survey.py solve OUT --unit mast02 --fibre-xy X Y [--modes original,selective]
        [--share-root /Storage/mast-share] [--source-survey PATH] [--redo]

It leaves ``OUT/<name>/solve/`` in the layout ``analyse`` reads. It needs ``source_survey.py`` (MAST_unit branch
``solver-source-selection``), the astrometry.net indexes and the solver of the machine it runs on: a unit, or the
machine the pilot used (mast08). ``--share-root`` maps the sidecar's ``//<controller>/mast-share`` to where the share is
mounted on this machine.

``pointing`` scores the target stars:

    python tools/gaia_accuracy_survey.py pointing OUT --fibre-xy X Y [--modes original,selective] [--search-px 25]

For each target it finds the target star in the frame (the brightest source within ``--search-px`` full-frame pixels of
the fibre pixel, at its fixed-window centroid), puts it on the sky through each mode's solved WCS, and compares that
with the star's Gaia position from ``targets.csv``: the error of the pointing, east and north in arcsec. It writes
``pointing.csv`` (per target and mode) and prints the median vector per mode, the paired difference between the last
mode and the first (over targets, with its standard error) and the decision criterion of the plan. A mode whose fit
used the target star itself is flagged: its error is then not independent.

Layout ``analyse`` reads: a folder per frame (a single-frame survey run), holding ``*sources.csv`` and, for each mode, a
folder
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
import datetime
import json
import subprocess
import sys
import time
import warnings
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol

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


def load_modes(
    folder: Path, modes: Sequence[str], params: Params
) -> tuple[dict[str, WCS], dict[str, np.ndarray], float] | None:
    """Each mode's solved WCS and the stars its fit used (``.corr``, FITS 1-based), and the frame's mid-exposure epoch.
    None (with the reason on stderr) when a mode is missing."""
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
    return wcs, used, jyear


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
    loaded = load_modes(folder, modes, params)
    if loaded is None:
        return None
    wcs, used, jyear = loaded
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


# ------------------------------------------------------------------------------------------------ plan: choosing targets

#: ``cone(ra_deg, dec_deg, radius_deg)`` -> ``(ra_deg, dec_deg, G, pmra_mas_yr, pmdec_mas_yr)`` arrays, catalog epoch.
Cone = Callable[[float, float, float], tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]]

_MAS_PER_DEG = 3.6e6
_J2000_JD = 2451545.0


@dataclasses.dataclass
class PlanParams:
    n: int = 20
    min_g: float = 8.0  # brightest: below the saturation margin
    max_g: float = 11.0
    max_pm: float = 10.0  # mas/yr, total; a measured proper motion is required
    isolation_arcsec: float = 30.0  # no catalog star of any brightness (to faintest_g) this close
    faintest_g: float = 18.0
    min_alt: float = 35.0
    max_alt: float = 80.0
    n_alt: int = 4  # sky cells: altitude bands x azimuth sectors
    n_az: int = 8
    search_deg: float = 4.0  # a cell's star is looked for within this of its centre
    catalog_epoch: float = 2016.0


@dataclasses.dataclass
class Target:
    name: str
    ra_deg: float  # Gaia position, proper motion applied to ``when``
    dec_deg: float
    g: float
    pm_mas_yr: float
    alt_deg: float
    az_deg: float
    cell: str
    density_per_deg2: float  # catalog stars to faintest_g around the cell centre: a crowding measure


def _gmst_deg(when: datetime.datetime) -> float:
    """Greenwich mean sidereal time, degrees: precise enough to place a sky cell (about 0.1 arcmin), no IERS needed."""
    jd = 2440587.5 + when.timestamp() / 86400.0
    return (280.46061837 + 360.98564736629 * (jd - _J2000_JD)) % 360.0


def radec_to_altaz(ra_deg, dec_deg, lat_deg: float, lon_deg: float, when: datetime.datetime):
    """Altitude and azimuth (east of north), degrees, of (ra, dec) for a site at ``when``."""
    ha = np.radians(_gmst_deg(when) + lon_deg - np.asarray(ra_deg, float))
    dec = np.radians(np.asarray(dec_deg, float))
    lat = np.radians(lat_deg)
    sin_alt = np.sin(dec) * np.sin(lat) + np.cos(dec) * np.cos(lat) * np.cos(ha)
    alt = np.arcsin(np.clip(sin_alt, -1.0, 1.0))
    az = np.arctan2(-np.sin(ha) * np.cos(dec), np.sin(dec) * np.cos(lat) - np.cos(dec) * np.sin(lat) * np.cos(ha))
    return np.degrees(alt), np.degrees(az) % 360.0


def altaz_to_radec(alt_deg: float, az_deg: float, lat_deg: float, lon_deg: float, when: datetime.datetime):
    """The inverse of :func:`radec_to_altaz`: (ra, dec) in degrees."""
    alt, az, lat = np.radians(alt_deg), np.radians(az_deg), np.radians(lat_deg)
    sin_dec = np.sin(alt) * np.sin(lat) + np.cos(alt) * np.cos(lat) * np.cos(az)
    dec = np.arcsin(np.clip(sin_dec, -1.0, 1.0))
    ha = np.arctan2(-np.sin(az) * np.cos(alt), np.sin(alt) * np.cos(lat) - np.cos(alt) * np.sin(lat) * np.cos(az))
    return float((_gmst_deg(when) + lon_deg - np.degrees(ha)) % 360.0), float(np.degrees(dec))


def sky_cells(params: PlanParams) -> list[tuple[str, float, float]]:
    """``(label, alt, az)`` of every cell's centre, ordered so each is as far as possible from those before it.

    Farthest-point sampling on the sphere: the first ``n`` cells are the best spread over the observable sky that
    ``n_alt`` x ``n_az`` cells allow, and the rest are the fallbacks when a cell has no suitable star.
    """
    alts = [params.min_alt + (i + 0.5) * (params.max_alt - params.min_alt) / params.n_alt for i in range(params.n_alt)]
    azs = [(j + 0.5) * 360.0 / params.n_az for j in range(params.n_az)]
    cells = [(f"alt{a:.0f}-az{z:.0f}", a, z) for a in alts for z in azs]
    vec = np.array([_alt_az_vector(a, z) for _, a, z in cells])
    order = [int(np.argmax(vec[:, 2]))]  # start at the highest
    best = vec @ vec[order[0]]  # cosine to the nearest chosen cell
    while len(order) < len(cells):
        order.append(int(np.argmin(np.where(np.isin(range(len(cells)), order), np.inf, best))))
        best = np.maximum(best, vec @ vec[order[-1]])
    return [cells[i] for i in order]


def _alt_az_vector(alt: float, az: float) -> np.ndarray:
    a, z = np.radians(alt), np.radians(az)
    return np.array([np.cos(a) * np.sin(z), np.cos(a) * np.cos(z), np.sin(a)])


def _years_since(epoch_jyear: float, when: datetime.datetime) -> float:
    return (
        2000.0
        + (when - datetime.datetime(2000, 1, 1, 12, tzinfo=datetime.UTC)).total_seconds() / (86400.0 * 365.25)
        - epoch_jyear
    )


def pick_in_cell(
    cone: Cone,
    centre: tuple[float, float],
    params: PlanParams,
    years: float,
    lat_deg: float,
    lon_deg: float,
    when: datetime.datetime,
) -> tuple[Target | None, float]:
    """The suitable star nearest the cell's centre (and the catalog density there), or None. ``Target.cell`` is
    left for the caller."""
    ra0, dec0 = centre
    ra, dec, g, pmra, pmdec = (
        np.asarray(a, float) for a in cone(ra0, dec0, params.search_deg + params.isolation_arcsec / 3600.0)
    )
    ok = np.isfinite(ra) & np.isfinite(dec) & np.isfinite(g) & (g <= params.faintest_g)
    ra, dec, g, pmra, pmdec = ra[ok], dec[ok], g[ok], pmra[ok], pmdec[ok]
    density = len(ra) / (np.pi * params.search_deg**2)
    cosd = np.maximum(np.cos(np.radians(dec)), 1e-9)
    # Positions at ``when``. A star with no proper motion keeps its catalog position but cannot be a target.
    ra_now = (ra + np.nan_to_num(pmra) * years / _MAS_PER_DEG / cosd) % 360.0
    dec_now = dec + np.nan_to_num(pmdec) * years / _MAS_PER_DEG
    pm = np.hypot(pmra, pmdec)
    east, north = offsets_arcsec(ra_now, dec_now, ra0, dec0)
    sep_deg = np.hypot(east, north) / 3600.0
    alt, az = radec_to_altaz(ra_now, dec_now, lat_deg, lon_deg, when)
    candidate = np.flatnonzero(
        (g >= params.min_g)
        & (g <= params.max_g)
        & np.isfinite(pm)
        & (pm <= params.max_pm)
        & (sep_deg <= params.search_deg)
        & (alt >= params.min_alt)
        & (alt <= params.max_alt)
    )
    if not len(candidate):
        return None, density
    tree = cKDTree(_unit_vectors(ra_now, dec_now))
    for i in candidate[np.argsort(sep_deg[candidate])]:
        # itself plus anything else inside the isolation radius, whatever its brightness
        if len(tree.query_ball_point(tree.data[i], _chord(params.isolation_arcsec))) == 1:
            return Target(
                "", float(ra_now[i]), float(dec_now[i]), float(g[i]), float(pm[i]), float(alt[i]), float(az[i]), "", density
            ), density
    return None, density


def plan_targets(
    cone: Cone, lat_deg: float, lon_deg: float, when: datetime.datetime, params: PlanParams
) -> tuple[list[Target], list[str]]:
    """Up to ``params.n`` targets spread over the sky at ``when``, in azimuth order, and the cells that gave none."""
    years = _years_since(params.catalog_epoch, when)
    chosen: list[Target] = []
    empty: list[str] = []
    for label, alt, az in sky_cells(params):
        if len(chosen) >= params.n:
            break
        target, density = pick_in_cell(
            cone, altaz_to_radec(alt, az, lat_deg, lon_deg, when), params, years, lat_deg, lon_deg, when
        )
        if target is None:
            empty.append(label)
            continue
        chosen.append(dataclasses.replace(target, cell=label, density_per_deg2=density))
    chosen.sort(key=lambda t: t.az_deg)  # one sweep round the sky
    for k, t in enumerate(chosen, 1):
        t.name = f"T{k:02d}"
    return chosen, empty


def catalog_cone(catalog_root: str | None, catalog_name: str | None) -> Cone:
    """Gaia DR3 from the catsHTM files, as ``(ra, dec, G, pmra, pmdec)`` in degrees, mag and mas/yr."""
    from catalogs.catshtm import CatsHtm  # noqa: PLC0415 -- needs h5py and the catalog share
    from common.config.calibration import FocusOnStarSettings  # noqa: PLC0415
    from focus_on_star.star_selection import DEC, PMDEC, PMRA, RA  # noqa: PLC0415

    defaults = FocusOnStarSettings()
    catalog = CatsHtm(catalog_root or defaults.catalog_root, catalog_name or defaults.catalog_name)

    def cone(ra: float, dec: float, radius: float):
        data = catalog.cone_search(ra, dec, radius, [RA, DEC, defaults.mag_column, PMRA, PMDEC])
        return np.degrees(data[RA]), np.degrees(data[DEC]), data[defaults.mag_column], data[PMRA], data[PMDEC]

    return cone


def site_location() -> tuple[float, float]:
    """This machine's configured site: ``(latitude, longitude)``, degrees."""
    from common.config import Config  # noqa: PLC0415

    site = Config().local_site
    loc = site.location if site else None
    if loc is None or loc.latitude is None or loc.longitude is None:
        raise SystemExit("no configured site location: give --lat and --lon")
    return float(loc.latitude), float(loc.longitude)


def format_targets(targets: Sequence[Target], empty: Sequence[str], when: datetime.datetime) -> str:
    lines = [f"{len(targets)} targets for {when:%Y-%m-%d %H:%M} UTC (Gaia DR3, proper motion applied to then)"]
    lines.append(f"  {'name':<5}{'RA':>10}{'Dec':>9}{'G':>6}{'PM':>6}{'alt':>6}{'az':>6}  {'stars/deg2':>10}  cell")
    for t in targets:
        lines.append(
            f"  {t.name:<5}{t.ra_deg:>10.5f}{t.dec_deg:>9.4f}{t.g:>6.2f}{t.pm_mas_yr:>6.1f}{t.alt_deg:>6.1f}{t.az_deg:>6.1f}"
            f"  {t.density_per_deg2:>10.0f}  {t.cell}"
        )
    if empty:
        lines.append(f"  cells with no suitable star: {', '.join(empty)}")
    return "\n".join(lines)


def plan(out: Path, cone: Cone, lat_deg: float, lon_deg: float, when: datetime.datetime, params: PlanParams) -> str:
    """Choose the targets, write ``targets.csv`` into ``out`` and return the report."""
    targets, empty = plan_targets(cone, lat_deg, lon_deg, when, params)
    out.mkdir(parents=True, exist_ok=True)
    _write_csv(out / "targets.csv", [dataclasses.asdict(t) for t in targets])
    return format_targets(targets, empty, when)


# ------------------------------------------------------------------------------------------------ acquire: running them

POLL_SECONDS = 2.0


class UnitClient(Protocol):
    """What ``acquire`` needs of a unit: its status (as a dict) and one acquisition request."""

    def status(self) -> dict: ...

    def start_acquisition(self, params: dict) -> tuple[bool, list[str]]:
        """``(accepted, errors)``."""
        ...


class HttpUnit:
    """A unit over its HTTP API (``common.api.UnitApi``)."""

    def __init__(self, hostname: str):
        from common.api import UnitApi  # noqa: PLC0415

        self._api = UnitApi(hostname=hostname)

    def status(self) -> dict:
        import asyncio  # noqa: PLC0415

        response = asyncio.run(self._api.get(method="status"))
        if response.failed or not isinstance(response.value, dict):
            raise RuntimeError(f"no status from the unit: {response.errors}")
        return response.value

    def start_acquisition(self, params: dict) -> tuple[bool, list[str]]:
        import asyncio  # noqa: PLC0415

        response = asyncio.run(self._api.put(method="start_acquisition_and_guiding", params=params))
        return (not response.failed, list(response.errors or []))


def _latest_acquisition(status: dict) -> dict | None:
    return ((status.get("latest") or {}).get("acquisition")) or None


def acquisition_request(target: dict, seconds: float) -> dict:
    """The ``start_acquisition_and_guiding`` parameters for one target row: Gaia's position, unguided afterwards."""
    return {
        "seconds": seconds,
        "ra_j2000_hours": float(target["ra_deg"]) / 15.0,
        "dec_j2000_degs": float(target["dec_deg"]),
        "handover_automatically_to_guider": False,
    }


def sidecar_folder(out: Path, target: dict) -> Path:
    return out / target["name"]


def read_targets(path: Path, only: Sequence[str] = ()) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if only:
        wanted = set(only)
        rows = [r for r in rows if r["name"] in wanted]
        missing = wanted - {r["name"] for r in rows}
        if missing:
            raise SystemExit(f"not in {path}: {', '.join(sorted(missing))}")
    return rows


def acquire_target(
    unit: UnitClient,
    target: dict,
    out: Path,
    *,
    unit_name: str,
    mode: str,
    seconds: float,
    timeout: float,
    poll: float = POLL_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """Run one acquisition and write its sidecar. Returns the sidecar."""
    request = acquisition_request(target, seconds)
    before = _latest_acquisition(unit.status())
    before_started = before.get("started") if before else None
    sidecar: dict = {
        "target": target,
        "unit": unit_name,
        "mode": mode,
        "request": request,
        "requested_at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
    }
    accepted, errors = unit.start_acquisition(request)
    if not accepted:
        sidecar.update(outcome="refused", errors=errors)
    else:
        deadline = clock() + timeout
        latest = None
        while clock() < deadline:
            latest = _latest_acquisition(unit.status())
            # a record of a run that started after our request: the first status read after the PUT is already ours
            if latest and latest.get("started") != before_started and latest.get("ended"):
                break
            latest = None
            sleep(poll)
        if latest is None:
            sidecar.update(outcome="timeout", errors=[f"no end of the acquisition within {timeout:g} s"])
        else:
            status = unit.status()
            mount = status.get("mount") or {}
            sidecar.update(
                outcome="succeeded" if latest.get("succeeded") else "failed",
                errors=list(latest.get("errors") or []),
                started=latest.get("started"),
                ended=latest.get("ended"),
                state=latest.get("state"),
                phases=latest.get("phases") or {},
                shared_products_path=latest.get("shared_products_path"),
                local_products_path=latest.get("local_products_path"),
                solver=latest.get("solver"),
                mount_ra_j2000_hours=mount.get("ra_j2000_hours"),
                mount_dec_j2000_degs=mount.get("dec_j2000_degs"),
            )
    folder = sidecar_folder(out, target)
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / "acquire.json", "w", encoding="utf-8") as f:
        json.dump(sidecar, f, indent=2)
    return sidecar


def acquire(
    unit: UnitClient,
    targets: Sequence[dict],
    out: Path,
    *,
    unit_name: str,
    mode: str,
    seconds: float = 5.0,
    timeout: float = 900.0,
    redo: bool = False,
    poll: float = POLL_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = print,
) -> list[dict]:
    """Every target in turn. A target that has its ``acquire.json`` is skipped unless ``redo``."""
    done: list[dict] = []
    for target in targets:
        sidecar_path = sidecar_folder(out, target) / "acquire.json"
        if sidecar_path.exists() and not redo:
            log(f"{target['name']}: already acquired, skipped")
            continue
        log(f"{target['name']}: acquiring RA {float(target['ra_deg']):.5f} Dec {float(target['dec_deg']):.4f} ({mode})")
        sidecar = acquire_target(
            unit,
            target,
            out,
            unit_name=unit_name,
            mode=mode,
            seconds=seconds,
            timeout=timeout,
            poll=poll,
            clock=clock,
            sleep=sleep,
        )
        log(f"{target['name']}: {sidecar['outcome']}" + (f" -- {'; '.join(sidecar['errors'])}" if sidecar["errors"] else ""))
        done.append(sidecar)
    return done


# ------------------------------------------------------------------------------------------------ solve: both modes, offline

#: The selection parameters the pilot's frames were surveyed with (tighter centroids, a stricter neighbour rule).
SURVEY_PARAMS = (
    "centroid_tolerance=0.002",
    "centroid_max_iterations=32",
    "min_sep_fwhm=1.5",
    "max_fwhm_ratio=2.5",
)
#: The folders of an acquisition that hold the frames taken with the fold mirror out, in order of preference.
MIRROR_OUT_PHASES = ("spec-mirror-out", "spec")
DEFAULT_SOURCE_SURVEY = Path(__file__).resolve().parent / "source_survey.py"


def map_share(path: str, share_root: Path | None) -> Path:
    """``//<controller>/mast-share/MAST/...`` (as the unit reports it) as a path on this machine: under ``share_root``
    when one is given, else unchanged."""
    text = path.replace("\\", "/")
    if share_root is not None and text.startswith("//"):
        parts = text[2:].split("/", 2)  # host, share, the rest
        if len(parts) == 3:
            return share_root / parts[2]
    return Path(path)


def final_frame(products: Path) -> Path | None:
    """The last raw frame of the acquisition's mirror-out phase: the one taken at the pointing it ended with."""
    for phase in MIRROR_OUT_PHASES:
        folder = products / phase
        if folder.is_dir():
            frames = sorted(f for f in folder.glob("*.fits") if ",solver=" not in f.name and "downsampled" not in f.name)
            if frames:
                return frames[-1]
    return None


def solve_command(
    frame: Path, dest: Path, *, unit: str, fibre_xy: tuple[float, float], modes: Sequence[str], source_survey: Path
) -> list[str]:
    return [
        sys.executable,
        str(source_survey),
        str(frame),
        "--out",
        str(dest),
        "--via-solver",
        "--solver-modes",
        ",".join(modes),
        "--fibre-xy",
        f"{fibre_xy[0]:g}",
        f"{fibre_xy[1]:g}",
        "--hot-pixels",
        unit,
        *[a for p in SURVEY_PARAMS for a in ("--param", p)],
    ]


def solve(
    out: Path,
    *,
    unit: str,
    fibre_xy: tuple[float, float],
    modes: Sequence[str],
    share_root: Path | None = None,
    source_survey: Path = DEFAULT_SOURCE_SURVEY,
    redo: bool = False,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    log: Callable[[str], None] = print,
) -> dict[str, str]:
    """Solve each acquired target's last mirror-out frame in every mode. Returns ``{target: outcome}``."""
    if not source_survey.exists():
        raise SystemExit(f"{source_survey} not found: source_survey.py is on MAST_unit's solver-source-selection branch")
    outcomes: dict[str, str] = {}
    for sidecar_path in sorted(out.glob("*/acquire.json")):
        sidecar = json.loads(sidecar_path.read_text())
        name = sidecar["target"]["name"]
        dest = sidecar_path.parent / "solve"
        if sidecar.get("outcome") != "succeeded":
            outcomes[name] = f"skipped: acquisition {sidecar.get('outcome')}"
        elif (dest / "survey.csv").exists() and not redo:
            outcomes[name] = "already solved"
        else:
            products = map_share(sidecar.get("shared_products_path") or "", share_root)
            frame = final_frame(products)
            if frame is None:
                outcomes[name] = f"skipped: no mirror-out frame under {products}"
            else:
                result = run(
                    solve_command(frame, dest, unit=unit, fibre_xy=fibre_xy, modes=modes, source_survey=source_survey),
                    capture_output=True,
                    text=True,
                )
                (sidecar_path.parent / "solve.log").write_text(
                    (result.stdout or "") + (result.stderr or ""), encoding="utf-8"
                )
                outcomes[name] = "solved" if result.returncode == 0 else f"failed (exit {result.returncode}), see solve.log"
        log(f"{name}: {outcomes[name]}")
    return outcomes


# ------------------------------------------------------------------------------------------------ pointing: the target stars


@dataclasses.dataclass
class PointingError:
    target: str
    mode: str
    x: float  # the target star's fixed-window centroid, full frame, 0-based
    y: float
    offset_px: float  # from the fibre pixel
    err_e: float  # arcsec, WCS sky position of the star minus its Gaia position
    err_n: float
    fitted: bool  # the mode's own fit used this star, so its error is not independent
    n_in_window: int  # sources found inside the search window: more than one is an ambiguous identification
    g: float

    @property
    def err(self) -> float:
        return float(np.hypot(self.err_e, self.err_n))


def find_target_star(sources_csv: Path, fibre_xy: tuple[float, float], search_px: float) -> tuple[float, float, int] | None:
    """``(x, y, n)``: the brightest source of any fate within ``search_px`` of the fibre pixel, and how many there were."""
    best: tuple[float, float, float] | None = None
    n = 0
    with open(sources_csv, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                x, y, flux = float(row[X_COLUMN]), float(row[Y_COLUMN]), float(row["flux"])
            except (KeyError, ValueError):
                continue
            if not (np.isfinite(x) and np.isfinite(y) and np.isfinite(flux)):
                continue
            if np.hypot(x - fibre_xy[0], y - fibre_xy[1]) <= search_px:
                n += 1
                if best is None or flux > best[2]:
                    best = (x, y, flux)
    return None if best is None else (best[0], best[1], n)


def score_target(
    folder: Path, target: dict, modes: Sequence[str], params: Params, search_px: float
) -> list[PointingError] | str:
    """The target star's error in each mode, or the reason it cannot be scored."""
    sources = next(iter(sorted(folder.glob("*sources.csv"))), None)
    if sources is None:
        return "no sources.csv (not solved?)"
    star = find_target_star(sources, params.fibre_xy, search_px)
    if star is None:
        return f"no source within {search_px:g} px of the fibre pixel"
    loaded = load_modes(folder, modes, params)
    if loaded is None:
        return "a mode is missing its solved FITS"
    wcs, used, _ = loaded
    x, y, n = star
    gx, gy = to_grid([x], [y], params.factor)
    out: list[PointingError] = []
    for mode in modes:
        ra, dec = wcs[mode].all_pix2world(gx, gy, 1)
        e, nn = offsets_arcsec(ra[0], dec[0], float(target["ra_deg"]), float(target["dec_deg"]))
        fitted = not bool(held_out(gx, gy, [used[mode]], params.exclude_px)[0])
        out.append(
            PointingError(
                target["name"],
                mode,
                x,
                y,
                float(np.hypot(x - params.fibre_xy[0], y - params.fibre_xy[1])),
                float(e),
                float(nn),
                fitted,
                n,
                float(target["g"]),
            )
        )
    return out


def _mean_se(values: Sequence[float]) -> tuple[float, float]:
    v = np.asarray(values, float)
    return float(v.mean()), float(v.std(ddof=1) / np.sqrt(len(v))) if len(v) > 1 else float("nan")


def pointing_report(rows: Sequence[PointingError], modes: Sequence[str], skipped: dict[str, str]) -> str:
    base, test = modes[0], modes[-1]
    lines = [f"pointing: {len({r.target for r in rows})} targets scored, error = WCS position of the target star minus Gaia"]
    for m in modes:
        sel = [r for r in rows if r.mode == m]
        if not sel:
            continue
        e, n = (float(np.median([r.err_e for r in sel])), float(np.median([r.err_n for r in sel])))
        mean, se = _mean_se([r.err for r in sel])
        fitted = sum(r.fitted for r in sel)
        lines.append(
            f'  {m:<10} median vector {e:+.2f} E {n:+.2f} N ({np.hypot(e, n):.2f}"), '
            f'mean |error| {mean:.2f} +- {se:.2f}" over {len(sel)} targets, {fitted} fitted its star'
        )
    paired = {}
    for r in rows:
        paired.setdefault(r.target, {})[r.mode] = r
    both = [d for d in paired.values() if base in d and test in d]
    if base != test and len(both) > 1:
        diff = [d[test].err - d[base].err for d in both]
        mean, se = _mean_se(diff)
        better = sum(x < 0 for x in diff)
        verdict = "meets" if np.isfinite(se) and mean <= -2.0 * se else "does not meet"
        lines.append(
            f'  {test} minus {base}, per target: {mean:+.2f} +- {se:.2f}" ({better} of {len(both)} targets closer to Gaia); '
            f"{verdict} the plan's criterion (smaller by at least two standard errors)"
        )
    for name, why in sorted(skipped.items()):
        lines.append(f"  not scored: {name}: {why}")
    return "\n".join(lines)


def pointing(out: Path, params: Params, modes: Sequence[str], search_px: float, targets_csv: Path | None = None) -> str:
    """Score every target that has a solved frame, write ``pointing.csv`` into ``out``, return the report."""
    targets = {t["name"]: t for t in read_targets(targets_csv or out / "targets.csv")}
    rows: list[PointingError] = []
    skipped: dict[str, str] = {}
    for sidecar_path in sorted(out.glob("*/acquire.json")):
        name = sidecar_path.parent.name
        if name not in targets:
            continue
        result = score_target(sidecar_path.parent / "solve", targets[name], modes, params, search_px)
        if isinstance(result, str):
            skipped[name] = result
        else:
            rows.extend(result)
    _write_csv(out / "pointing.csv", [dict(dataclasses.asdict(r), err=r.err) for r in rows])
    return pointing_report(rows, modes, skipped)


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
    p = sub.add_parser("plan", help="choose the survey's Gaia targets for a night")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--n", type=int, default=PlanParams.n)
    p.add_argument("--when", help="UTC, ISO 8601 (default: now): the time the sky is laid out for")
    p.add_argument("--lat", type=float, help="site latitude, degrees (default: the configured site)")
    p.add_argument("--lon", type=float, help="site longitude, degrees east (default: the configured site)")
    p.add_argument("--min-g", type=float, default=PlanParams.min_g)
    p.add_argument("--max-g", type=float, default=PlanParams.max_g)
    p.add_argument("--max-pm", type=float, default=PlanParams.max_pm, help="mas/yr")
    p.add_argument("--isolation-arcsec", type=float, default=PlanParams.isolation_arcsec)
    p.add_argument("--faintest-g", type=float, default=PlanParams.faintest_g)
    p.add_argument("--min-alt", type=float, default=PlanParams.min_alt)
    p.add_argument("--max-alt", type=float, default=PlanParams.max_alt)
    p.add_argument("--n-alt", type=int, default=PlanParams.n_alt)
    p.add_argument("--n-az", type=int, default=PlanParams.n_az)
    p.add_argument("--search-deg", type=float, default=PlanParams.search_deg)
    p.add_argument("--catalog-root")
    p.add_argument("--catalog-name")
    q = sub.add_parser("acquire", help="run one mastrometry acquisition per target on a unit")
    q.add_argument("targets", type=Path, help="the targets.csv that `plan` wrote")
    q.add_argument("--unit", required=True, help="the unit's host name, e.g. mast02")
    q.add_argument("--mode", default="as-configured", help="a label for the sidecar: the mode the unit is set to")
    q.add_argument("--out", type=Path, required=True)
    q.add_argument("--seconds", type=float, default=5.0, help="exposure per acquisition frame")
    q.add_argument("--timeout", type=float, default=900.0, help="seconds to wait for one acquisition to end")
    q.add_argument("--only", default="", help="comma-separated target names")
    q.add_argument("--redo", action="store_true", help="run targets that already have an acquire.json again")
    v = sub.add_parser("solve", help="solve each acquired target's last frame in both modes (offline)")
    v.add_argument("out", type=Path, help="the folder `acquire` wrote into")
    v.add_argument("--unit", required=True, help="the unit the frames came from (its hot-pixel map is used)")
    v.add_argument("--fibre-xy", type=float, nargs=2, required=True, metavar=("X", "Y"))
    v.add_argument("--modes", default=",".join(DEFAULT_MODES))
    v.add_argument("--share-root", type=Path, help="where the share is mounted on this machine")
    v.add_argument("--source-survey", type=Path, default=DEFAULT_SOURCE_SURVEY)
    v.add_argument("--redo", action="store_true")
    w = sub.add_parser("pointing", help="score each target star's WCS position against its Gaia position")
    w.add_argument("out", type=Path, help="the folder `acquire` and `solve` wrote into (and `plan`'s targets.csv)")
    w.add_argument("--targets", type=Path, help="default: OUT/targets.csv")
    w.add_argument("--fibre-xy", type=float, nargs=2, required=True, metavar=("X", "Y"))
    w.add_argument("--modes", default=",".join(DEFAULT_MODES), help="the last is compared with the first")
    w.add_argument("--search-px", type=float, default=25.0, help="full-frame pixels around the fibre pixel")
    w.add_argument("--exclude-px", type=float, default=1.5, help="binned pixels: a star this close to one the fit used")
    w.add_argument("--bin", type=int, default=2)
    w.add_argument("--xylist-origin", type=int, choices=(0, 1), default=1)
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


def _plan_main(args: argparse.Namespace, cone: Cone | None) -> int:
    when = (
        datetime.datetime.fromisoformat(args.when.replace("Z", "+00:00"))
        if args.when
        else datetime.datetime.now(datetime.UTC)
    )
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.UTC)
    lat, lon = (args.lat, args.lon) if args.lat is not None and args.lon is not None else site_location()
    params = PlanParams(
        n=args.n,
        min_g=args.min_g,
        max_g=args.max_g,
        max_pm=args.max_pm,
        isolation_arcsec=args.isolation_arcsec,
        faintest_g=args.faintest_g,
        min_alt=args.min_alt,
        max_alt=args.max_alt,
        n_alt=args.n_alt,
        n_az=args.n_az,
        search_deg=args.search_deg,
    )
    print(plan(args.out, cone or catalog_cone(args.catalog_root, args.catalog_name), lat, lon, when, params))
    return 0


def _acquire_main(args: argparse.Namespace, unit: UnitClient | None) -> int:
    only = [n.strip() for n in args.only.split(",") if n.strip()]
    done = acquire(
        unit or HttpUnit(args.unit),
        read_targets(args.targets, only),
        args.out,
        unit_name=args.unit,
        mode=args.mode,
        seconds=args.seconds,
        timeout=args.timeout,
        redo=args.redo,
    )
    failed = [d["target"]["name"] for d in done if d["outcome"] != "succeeded"]
    print(
        f"{len(done)} run, {len(done) - len(failed)} succeeded" + (f"; not succeeded: {', '.join(failed)}" if failed else "")
    )
    return 1 if failed else 0


def main(
    argv: Sequence[str] | None = None,
    reference: Reference | None = None,
    cone: Cone | None = None,
    unit: UnitClient | None = None,
) -> int:
    args = _parse(argv)
    if args.command == "plan":
        return _plan_main(args, cone)
    if args.command == "acquire":
        return _acquire_main(args, unit)
    if args.command == "solve":
        modes = tuple(m.strip() for m in args.modes.split(",") if m.strip())
        outcomes = solve(
            args.out,
            unit=args.unit,
            fibre_xy=(args.fibre_xy[0], args.fibre_xy[1]),
            modes=modes,
            share_root=args.share_root,
            source_survey=args.source_survey,
            redo=args.redo,
        )
        return 1 if any(o.startswith("failed") for o in outcomes.values()) else 0
    if args.command == "pointing":
        modes = tuple(m.strip() for m in args.modes.split(",") if m.strip())
        params = Params(
            fibre_xy=(args.fibre_xy[0], args.fibre_xy[1]),
            exclude_px=args.exclude_px,
            factor=args.bin,
            xylist_origin=args.xylist_origin,
        )
        print(pointing(args.out, params, modes, args.search_px, args.targets))
        return 0
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
