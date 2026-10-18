"""Choosing focus_on_star's star from the catalog: pure, no hardware.

The rule (decided 2026-10-11):

* a candidate is no brighter than the limit -- ``mag >= mag_limit`` -- and lies
  within the search radius of the sky position;
* candidates are tried brightest first (closest to the limit), ties broken by
  distance from the sky position;
* the first candidate with **no star brighter than itself** inside the isolation
  radius wins.  That radius covers the measured box wherever the camera's
  position angle puts it (see :func:`isolation_radius_deg`), so nothing brighter
  can sit in the box and outweigh the star the box is about.

If no candidate qualifies, the search radius grows by ``search_growth`` up to
``max_search_radius_deg``.  Each query reaches ``radius + isolation`` so a
candidate near the edge of the search still has all its neighbours checked.
"""

from __future__ import annotations

import datetime
import math
from dataclasses import asdict, dataclass

import numpy as np

from catalogs.catshtm import CatsHtm, angular_distance

#: GAIA DR3 column names, as in GAIADR3_htmColCell.mat.
RA, DEC, PMRA, PMDEC = "RA", "Dec", "PMRA", "PMDec"

_MAS_PER_DEG = 3.6e6


@dataclass(frozen=True)
class FocusStar:
    ra_deg: float
    dec_deg: float
    mag: float
    separation_deg: float  # from the requested sky position
    search_radius_deg: float  # the radius at which it was found
    n_candidates: int  # stars that met the magnitude rule within that radius
    n_rejected: int  # of those, tried and rejected for a brighter neighbour

    def to_dict(self) -> dict:
        return asdict(self)


def separation_deg(ra1_deg, dec1_deg, ra2_deg, dec2_deg):
    return np.degrees(angular_distance(np.radians(ra1_deg), np.radians(dec1_deg), np.radians(ra2_deg), np.radians(dec2_deg)))


def isolation_radius_deg(box_w_px: float, box_h_px: float, pixel_scale_arcsec: float, margin_px: float) -> float:
    """Radius about the star that contains the whole box, at ANY position angle.

    The star sits at the box's center, so the farthest the box reaches is its
    half-diagonal.  A circle, not the box itself, because the camera's rotation
    on the sky is not known here; this rejects a few candidates a true rotated
    box would accept, which only costs search radius.  ``margin_px`` keeps a
    neighbour just outside the box from spilling its wings in.
    """
    return (0.5 * math.hypot(box_w_px, box_h_px) + margin_px) * pixel_scale_arcsec / 3600.0


_J2000 = datetime.datetime(2000, 1, 1, 12, tzinfo=datetime.UTC)


def years_since(epoch_jyear: float, when: datetime.datetime | None = None) -> float:
    """Julian years from ``epoch_jyear`` (GAIA DR3: J2016.0) to ``when`` (default: now)."""
    when = when or datetime.datetime.now(datetime.UTC)
    now_jyear = 2000.0 + (when - _J2000).total_seconds() / (86400.0 * 365.25)
    return now_jyear - epoch_jyear


def propagate_proper_motion(ra_deg, dec_deg, pmra_mas_yr, pmdec_mas_yr, years: float):
    """Move catalog positions on by ``years`` of proper motion (linear; fine over decades).

    Gaia's ``pmra`` is mu_alpha* -- it already includes cos(dec) -- so the RA
    step divides it back out.  A missing proper motion (NaN, 2-parameter
    solutions) leaves the position where it was.
    """
    pmra = np.nan_to_num(np.asarray(pmra_mas_yr, dtype=float))
    pmdec = np.nan_to_num(np.asarray(pmdec_mas_yr, dtype=float))
    dec = np.asarray(dec_deg, dtype=float)
    cosd = np.maximum(np.cos(np.radians(dec)), 1e-9)
    ra = (np.asarray(ra_deg, dtype=float) + pmra * years / _MAS_PER_DEG / cosd) % 360.0
    return ra, dec + pmdec * years / _MAS_PER_DEG


def select_focus_star(
    ra_deg,
    dec_deg,
    mag,
    *,
    center_ra_deg: float,
    center_dec_deg: float,
    mag_limit: float,
    search_radius_deg: float,
    isolation_radius_deg: float,
) -> FocusStar | None:
    """Apply the rule in the module docstring to one set of catalog stars."""
    ra = np.asarray(ra_deg, dtype=float)
    dec = np.asarray(dec_deg, dtype=float)
    mag = np.asarray(mag, dtype=float)
    finite = np.isfinite(mag) & np.isfinite(ra) & np.isfinite(dec)
    sep = separation_deg(center_ra_deg, center_dec_deg, ra, dec)

    cand = np.flatnonzero(finite & (mag >= mag_limit) & (sep <= search_radius_deg))
    cand = cand[np.lexsort((sep[cand], mag[cand]))]  # brightest first, then nearest

    # Only stars brighter than a candidate can disqualify it.  Sorting by
    # magnitude once makes each candidate's pool a prefix, which stays small:
    # the candidates tried first sit just at the limit.
    by_mag = np.flatnonzero(finite)
    by_mag = by_mag[np.argsort(mag[by_mag], kind="stable")]
    mags_sorted = mag[by_mag]

    for n_rejected, i in enumerate(cand):
        brighter = by_mag[: np.searchsorted(mags_sorted, mag[i], side="left")]
        if len(brighter) and np.any(separation_deg(ra[i], dec[i], ra[brighter], dec[brighter]) <= isolation_radius_deg):
            continue
        return FocusStar(
            ra_deg=float(ra[i]),
            dec_deg=float(dec[i]),
            mag=float(mag[i]),
            separation_deg=float(sep[i]),
            search_radius_deg=float(search_radius_deg),
            n_candidates=len(cand),
            n_rejected=n_rejected,
        )
    return None


def search_radii(start_deg: float, growth: float, max_deg: float) -> list[float]:
    """``start, start*growth, ...`` below ``max_deg``, then ``max_deg`` itself."""
    radii, r = [], float(start_deg)
    while r < max_deg and growth > 1.0:
        radii.append(r)
        r *= growth
    radii.append(float(max_deg))
    return radii


def find_focus_star(
    catalog: CatsHtm,
    *,
    ra_deg: float,
    dec_deg: float,
    mag_limit: float,
    isolation_deg: float,
    mag_column: str,
    search_radius_deg: float,
    search_growth: float,
    max_search_radius_deg: float,
    years_of_proper_motion: float | None,
) -> FocusStar | None:
    """Query the catalog at growing radii until :func:`select_focus_star` finds one.

    ``years_of_proper_motion=None`` uses the catalog positions as they are.
    Raises :class:`catalogs.catshtm.CatalogError` when the catalog is unreadable.
    """
    for radius in search_radii(search_radius_deg, search_growth, max_search_radius_deg):
        columns = [RA, DEC, mag_column] + ([PMRA, PMDEC] if years_of_proper_motion is not None else [])
        data = catalog.cone_search(ra_deg, dec_deg, radius + isolation_deg, columns)
        ra, dec = np.degrees(data[RA]), np.degrees(data[DEC])
        if years_of_proper_motion is not None:
            ra, dec = propagate_proper_motion(ra, dec, data[PMRA], data[PMDEC], years_of_proper_motion)
        star = select_focus_star(
            ra,
            dec,
            data[mag_column],
            center_ra_deg=ra_deg,
            center_dec_deg=dec_deg,
            mag_limit=mag_limit,
            search_radius_deg=radius,
            isolation_radius_deg=isolation_deg,
        )
        if star is not None:
            return star
    return None
