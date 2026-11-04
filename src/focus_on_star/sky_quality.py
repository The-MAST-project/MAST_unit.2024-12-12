"""The sky-quality frame: one frame at best focus, after a solved focus_on_star.

Pure but for the catalog query; :meth:`FocusOnStarRun._sky_quality` takes the frame and
calls :func:`measure`.  What it measures, into ``latest.sky_quality.measured``
(``common.models.latest.LatestMeasuredSkyQuality``):

* **Image quality** -- the focus star's FWHM and HFD, in arcseconds.
* **Zero point** -- ``G + 2.5 log10(counts / s)``, the median over the box's unsaturated
  stars matched to Gaia DR3.  The frame is not plate-solved: the focus star's pixel and
  Gaia position are known, and so is the plate scale, which leaves only the field's
  rotation and parity.  :func:`match_field` finds them by trying every rotation, both
  ways round, and keeping the one that lands the most catalog stars on detections.
* **Background** -- the box's median, raw ADU, bias included.

Design: mast-claude-config ``plans/focus_on_star_design.md``.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from imaging.hfd import FieldStar, StarMeasure

#: A catalog star matches a detection within this many pixels.
MATCH_TOL_PX = 3.0
#: The rotation search's step.  0.25 deg moves a star 400 px out by 1.7 px, inside the tolerance.
ROTATION_STEP_DEGS = 0.25
#: Matches, the focus star's own aside, below which the field is not taken as matched.
MIN_MATCHES = 3
#: The faintest catalog stars asked for: fainter ones are rarely detected at 5 s.
FAINTEST_G = 17.0


@dataclass(frozen=True)
class FieldMatch:
    rotation_degs: float  # east-north -> x-y
    mirrored: bool
    #: (catalog index, detection index), the focus star's pair included.
    pairs: list[tuple[int, int]]


def tangent_offsets_arcsec(ra0_deg: float, dec0_deg: float, ra_deg, dec_deg) -> tuple[np.ndarray, np.ndarray]:
    """Gnomonic offsets of (ra, dec) from (ra0, dec0): ``xi`` east, ``eta`` north, arcsec."""
    ra0, dec0 = np.radians(ra0_deg), np.radians(dec0_deg)
    ra, dec = np.radians(np.asarray(ra_deg, dtype=float)), np.radians(np.asarray(dec_deg, dtype=float))
    cos_c = np.sin(dec0) * np.sin(dec) + np.cos(dec0) * np.cos(dec) * np.cos(ra - ra0)
    xi = np.cos(dec) * np.sin(ra - ra0) / cos_c
    eta = (np.cos(dec0) * np.sin(dec) - np.sin(dec0) * np.cos(dec) * np.cos(ra - ra0)) / cos_c
    return np.degrees(xi) * 3600.0, np.degrees(eta) * 3600.0


def airmass(altitude_degs: float | None) -> float | None:
    """Pickering (2002): good to the horizon, where 1/sin(alt) is not.  None below it."""
    if altitude_degs is None or altitude_degs <= 0:
        return None
    h = float(altitude_degs)
    return float(1.0 / np.sin(np.radians(h + 244.0 / (165.0 + 47.0 * h**1.1))))


def match_field(
    det_dx,
    det_dy,
    xi_arcsec,
    eta_arcsec,
    scale_arcsec: float,
    *,
    tol_px: float = MATCH_TOL_PX,
    step_degs: float = ROTATION_STEP_DEGS,
    min_matches: int = MIN_MATCHES,
) -> FieldMatch | None:
    """The rotation and parity that put the catalog on the detections; None if none does.

    Both sets are offsets from the focus star -- detections in pixels, the catalog in arcsec
    -- so only the rotation and the parity are unknown.  Each is tried (both parities, every
    ``step_degs``), each catalog star counting when a detection lies within ``tol_px``.  The
    most counts win, the smaller mean residual breaking a tie.  ``min_matches`` excludes the
    focus star, which matches at every rotation.
    """
    dx, dy = np.asarray(det_dx, dtype=float), np.asarray(det_dy, dtype=float)
    xi, eta = np.asarray(xi_arcsec, dtype=float), np.asarray(eta_arcsec, dtype=float)
    if len(dx) == 0 or len(xi) == 0:
        return None
    thetas = np.radians(np.arange(0.0, 360.0, step_degs))
    cos_t, sin_t = np.cos(thetas)[:, None], np.sin(thetas)[:, None]
    best: tuple[int, float, float, bool] | None = None  # (count, -residual, theta, mirrored)
    for mirrored in (False, True):
        u = (-xi if mirrored else xi) / scale_arcsec
        v = eta / scale_arcsec
        x = u[None, :] * cos_t - v[None, :] * sin_t  # (T, M)
        y = u[None, :] * sin_t + v[None, :] * cos_t
        d = np.hypot(x[:, :, None] - dx[None, None, :], y[:, :, None] - dy[None, None, :])  # (T, M, N)
        nearest = d.min(axis=2)
        hit = nearest <= tol_px
        counts = hit.sum(axis=1)
        residual = np.where(hit, nearest, 0.0).sum(axis=1) / np.maximum(counts, 1)
        t = int(np.lexsort((residual, -counts))[0])
        key = (int(counts[t]), -float(residual[t]), float(thetas[t]), mirrored)
        if best is None or key[:2] > best[:2]:
            best = key
    assert best is not None
    count, _, theta, mirrored = best
    if count - 1 < min_matches:
        return None
    u = (-xi if mirrored else xi) / scale_arcsec
    v = eta / scale_arcsec
    x = u * np.cos(theta) - v * np.sin(theta)
    y = u * np.sin(theta) + v * np.cos(theta)
    d = np.hypot(x[:, None] - dx[None, :], y[:, None] - dy[None, :])
    pairs = [(int(j), int(np.argmin(d[j]))) for j in range(len(xi)) if d[j].min() <= tol_px]
    return FieldMatch(rotation_degs=float(np.degrees(theta)) % 360.0, mirrored=mirrored, pairs=pairs)


def zero_point(fluxes, mags, exposure_seconds: float) -> tuple[float | None, float | None, int]:
    """Median and robust spread of ``mag + 2.5 log10(flux / exposure)``, and how many stars."""
    f, m = np.asarray(fluxes, dtype=float), np.asarray(mags, dtype=float)
    ok = (f > 0) & np.isfinite(m)
    if not ok.any():
        return None, None, 0
    zp = m[ok] + 2.5 * np.log10(f[ok] / exposure_seconds)
    median = float(np.median(zp))
    spread = float(1.4826 * np.median(np.abs(zp - median))) if ok.sum() > 1 else None
    return median, spread, int(ok.sum())


@dataclass
class Measurement:
    """What :func:`measure` found; the run copies it onto the record."""

    fwhm_px: float | None = None
    hfd_px: float | None = None
    detected_stars: int = 0
    catalog_stars: int = 0
    matched_stars: int = 0
    rotation_degs: float | None = None
    mirrored: bool | None = None
    zero_point_mag: float | None = None
    zero_point_spread_mag: float | None = None
    zero_point_stars: int = 0


def measure(
    star: StarMeasure,
    star_ra_deg: float,
    star_dec_deg: float,
    stars: list[FieldStar],
    cat_ra_deg,
    cat_dec_deg,
    cat_mag,
    *,
    scale_arcsec: float,
    exposure_seconds: float,
) -> Measurement:
    """Image quality from ``star``; the zero point from ``stars`` matched to the catalog."""
    out = Measurement(
        fwhm_px=star.fwhm if np.isfinite(star.fwhm) else None,
        hfd_px=star.hfd if np.isfinite(star.hfd) else None,
        detected_stars=len(stars),
        catalog_stars=len(np.atleast_1d(cat_mag)),
    )
    if not stars or out.catalog_stars == 0:
        return out
    xi, eta = tangent_offsets_arcsec(star_ra_deg, star_dec_deg, cat_ra_deg, cat_dec_deg)
    dx = [s.x - star.x for s in stars]
    dy = [s.y - star.y for s in stars]
    match = match_field(dx, dy, xi, eta, scale_arcsec)
    if match is None:
        return out
    out.rotation_degs, out.mirrored, out.matched_stars = match.rotation_degs, match.mirrored, len(match.pairs)
    mags = np.asarray(cat_mag, dtype=float)
    usable = [(stars[d].flux, mags[c]) for c, d in match.pairs if not stars[d].saturated]
    if usable:
        fluxes, ms = zip(*usable, strict=True)
        out.zero_point_mag, out.zero_point_spread_mag, out.zero_point_stars = zero_point(fluxes, ms, exposure_seconds)
    return out
