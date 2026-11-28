"""Turn the guider's pixel drift at the fibre into a mount move, as plain-RA / Dec arcsec.

The guide stars have drifted by ``(dx, dy)`` px on the sensor since the reference frame, so the sky
has slid by the same ``(dx, dy)``: the fibre pixel now points where ``(fibre - d)`` pointed in the
reference. The move that brings the target back onto the fibre is therefore

    lock - WCS(fibre - d)

in **plain RA** (no cos dec, as ``mount_offset`` takes) and Dec arcsec. The WCS is the reference
frame's own plate solution -- the acquisition already produced it, with CRPIX set at the fibre, so
CRVAL is the fibre's reference pointing = the ``lock``; **the guider does not solve per frame**
(design §3, §11.1). The reference-minus-current form cancels the solve's absolute pointing error,
leaving only the differential displacement.

**One grid, no binning math.** MASTrometry solves on the 2x2-binned grid (the acquisition frame is
``DOWNSAMP=2``), and the guide loop captures at **binning 2** (asking the camera, not downsampling in
code), so the guide frames and the WCS share one grid. The guider therefore works entirely in that
binned grid -- ``fibre`` is CRPIX, ``(dx, dy)`` are binned pixels -- and there is **no** full-frame
<-> binned transform to do, so `solvers/pixel_grid.py` (the fenced surface behind two past silent
pointing bugs) is not needed and not touched. The one requirement is that the guide frames are
captured on the reference solve's grid (same binning, same crop). The plain-RA / Dec convention is
`sky_offsets.target_offset_plain_arcsec`, reused verbatim.
"""

from __future__ import annotations

from dataclasses import dataclass

import astropy.units as u
from astropy.coordinates import Angle

from common.utils import Coord
from sky_offsets import target_offset_plain_arcsec

DEGREES_PER_RA_HOUR = 15.0


@dataclass(frozen=True)
class PixelToSky:
    """The reference frame's plate solution, ready to turn a pixel drift at the fibre into a move.

    ``wcs`` is an ``astropy`` WCS on the solve's (2x2-binned) grid -- the same grid the guide frames
    are captured on. ``crpix`` is the fibre pixel on that grid, FITS 1-based (CRPIX1/2). ``lock`` is
    the fibre's reference pointing (CRVAL), where it is to be held. Build it with :meth:`from_solved`.
    """

    wcs: object
    crpix: tuple[float, float]
    lock: Coord

    @classmethod
    def from_solution(cls, solution, crpix: tuple[float, float]) -> PixelToSky:
        """Build directly from the acquisition's spec-mirror-in `SolvingSolution` (CRVAL + CD matrix),
        putting the fibre at ``crpix`` -- no FITS file, no re-solve, the reused-solve path (Option A).

        The CD matrix carries scale, rotation **and parity** exactly (per the binned pixel the solver
        downsampled to -- the guide frames' own grid), so no sign can be guessed wrong. CRVAL is the
        fibre's pointing (the solver sets CRPIX at the fibre), i.e. the lock. The *absolute* value of
        ``crpix`` barely matters: whatever pixel we declare as CRPIX maps to CRVAL by construction, so
        ``correction(0, 0)`` is exactly zero; the fibre estimate only sets where on the tangent plane
        the (tiny, second-order) projection curvature is reckoned. Raises ``ValueError`` if the
        solution lacks the CD matrix or a centre -- guiding on a missing parity is the silent-sign bug
        the CD matrix exists to prevent.
        """
        import math  # noqa: PLC0415

        cd = (solution.cd1_1, solution.cd1_2, solution.cd2_1, solution.cd2_2)
        if any(v is None for v in cd) or solution.ra_rads is None or solution.dec_rads is None:
            raise ValueError(
                "cannot build PixelToSky: the solution has no CD matrix / centre "
                f"(cd={cd}, ra_rads={solution.ra_rads}, dec_rads={solution.dec_rads})"
            )
        from astropy.wcs import WCS  # noqa: PLC0415

        ra_deg = math.degrees(float(solution.ra_rads))
        dec_deg = math.degrees(float(solution.dec_rads))
        wcs = WCS(naxis=2)
        wcs.wcs.ctype = ["RA---TAN", "DEC--TAN"]
        wcs.wcs.crpix = [float(crpix[0]), float(crpix[1])]  # FITS 1-based
        wcs.wcs.crval = [ra_deg, dec_deg]
        wcs.wcs.cd = [[float(cd[0]), float(cd[1])], [float(cd[2]), float(cd[3])]]
        lock = Coord(ra=Angle(ra_deg * u.deg), dec=Angle(dec_deg * u.deg))
        return cls(wcs=wcs, crpix=(float(crpix[0]), float(crpix[1])), lock=lock)

    @classmethod
    def from_solved(cls, solved) -> PixelToSky:
        """From a solved FITS header (or path): the WCS, the fibre pixel from CRPIX, and the lock from
        CRVAL, so ``correction(0, 0)`` is exactly zero by construction."""
        import warnings  # noqa: PLC0415

        from astropy.io import fits  # noqa: PLC0415
        from astropy.wcs import WCS, FITSFixedWarning  # noqa: PLC0415

        header = solved if hasattr(solved, "get") else fits.getheader(solved)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", FITSFixedWarning)
            wcs = WCS(header)
        lock = Coord(ra=Angle(float(header["CRVAL1"]) * u.deg), dec=Angle(float(header["CRVAL2"]) * u.deg))
        return cls(wcs=wcs, crpix=(float(header["CRPIX1"]), float(header["CRPIX2"])), lock=lock)

    def pointing(self, dx: float, dy: float) -> tuple[float, float]:
        """Where the fibre points, ``(RA deg, Dec deg)``, when the guide stars have drifted by
        ``(dx, dy)`` binned px from the reference: ``WCS(fibre - d)``. (CRPIX is FITS 1-based, so this
        evaluates the WCS with ``origin=1``.)"""
        px, py = self.crpix[0] - dx, self.crpix[1] - dy
        ra, dec = self.wcs.all_pix2world([[px, py]], 1)[0]
        return float(ra), float(dec)

    def correction(self, dx: float, dy: float) -> tuple[float, float]:
        """The mount move ``(plain RA arcsec, Dec arcsec)`` that holds the lock on the fibre when the
        guide stars have drifted by ``(dx, dy)`` binned px: ``lock - WCS(fibre - d)``."""
        ra_deg, dec_deg = self.pointing(dx, dy)
        return target_offset_plain_arcsec(self.lock, ra_deg / DEGREES_PER_RA_HOUR, dec_deg)
