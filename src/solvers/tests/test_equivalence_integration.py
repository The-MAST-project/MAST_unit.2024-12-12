"""Integration drift test: numpy pre-downsample vs solve-field --downsample.

SKIPPED unless astrometry.net, the index directory, and a sample full-frame FITS
are present (see conftest for the env vars). When it runs it re-executes the core
of the equivalence study: solve the same frame both ways, convert MASTrometry's
binned-grid WCS back to original pixels via ``pixel_grid``, and assert the two
solutions still agree to sub-arcsecond. It catches drift in the numpy kernel,
the solve-field version/behavior, or the conversion -- the things the pure-math
test cannot see.

This intentionally replicates the numpy downsample kernel standalone (as the
original study did) rather than driving the full MastrometryDotNet class, which
needs the RAM-disk/Filer/unit-config plumbing. The fragile part -- the
coordinate conversion -- IS the real ``pixel_grid`` code. An end-to-end ROI test
that drives the real class is a worthwhile future addition (see README).
"""

import os
import subprocess

import pixel_grid as pg
import pytest

# Heavy deps are only needed when this test actually runs; gate them through
# importorskip so the module skips cleanly (rather than erroring at collection)
# on a machine without the scientific stack.
np = pytest.importorskip("numpy")
astropy_fits = pytest.importorskip("astropy.io.fits")
import astropy.units as u  # noqa: E402
from astropy.coordinates import SkyCoord  # noqa: E402
from astropy.wcs import WCS  # noqa: E402

PIXELSCALE = 0.2616
FACTOR = 2
# Generous tolerances: this guards against kernel/solver drift, not the exact
# convention (test_pixel_grid pins that to the milli-pixel). FINDINGS observed
# 0.06" center / 0.13" corner with tweak on.
CENTER_TOL_ARCSEC = 0.5
CORNER_TOL_ARCSEC = 1.0
# Loose, on purpose. Moving CRPIX off-center re-anchors solve-field's SIP fit,
# which shifts the reported CRVAL by ~0.5-1.5" vs a centered solve -- a real
# astrometry.net behavior, NOT a refpix error (the convention is pinned exactly
# by test_pixel_grid). This bound only catches gross misplacement / mis-wiring.
# See COORDINATE_SURFACE.md "Off-center CRPIX behavior".
OFFCENTER_CRVAL_TOL_ARCSEC = 2.0


def _win_to_cygwin(path: str) -> str:
    if path[:2].lower() == "c:":
        path = "/cygdrive/c" + path[2:]
    elif path[:2].lower() == "d:":
        path = "/cygdrive/d" + path[2:]
    return path.replace("\\", "/")


def _cygwin_env() -> dict:
    """PATH that solve-field's cygwin sub-tools need (mirrors mastrometry.py).

    Without ``C:\\cygwin64\\bin`` (to load cygwin1.dll) and the ``/usr/lib/lapack``
    POSIX path (cygwin1.dll re-parses PATH at startup), image2pnm/removelines fail
    with a confusing "image type not recognized" error. Setting this matches how
    ``MastrometryDotNet`` actually invokes the solver in production.
    """
    env = os.environ.copy()
    env["PATH"] = r"C:\cygwin64\bin" + os.pathsep + "/usr/lib/lapack" + os.pathsep + env.get("PATH", "")
    return env


def _common_args(solve_field, index_dir, workdir):
    return [
        solve_field,
        "--scale-units",
        "arcsecperpix",
        "--index-dir",
        _win_to_cygwin(index_dir),
        "--no-plots",
        "--overwrite",
        "--cpulimit",
        "60",
        "--solved",
        "none",
        "--match",
        "none",
        "--rdls",
        "none",
        "--corr",
        "none",
        "--dir",
        _win_to_cygwin(str(workdir)),
        "--temp-dir",
        _win_to_cygwin(str(workdir)),
        "--crpix-center",  # tweak left ON (no --no-tweak), matching production
    ]


def _run(args):
    proc = subprocess.run(" ".join(args), capture_output=True, shell=True, env=_cygwin_env())
    assert proc.returncode == 0, f"solve-field failed (rc={proc.returncode}):\n{proc.stderr.decode(errors='replace')}"


def test_numpy_downsample_matches_native_downsample(astrometry_env, tmp_path):
    solve_field = astrometry_env["solve_field"]
    index_dir = astrometry_env["index_dir"]
    test_fits = astrometry_env["test_fits"]

    with astropy_fits.open(test_fits) as hdul:
        header = hdul[0].header.copy()
        data = hdul[0].data
        height, width = data.shape
        dtype = data.dtype

    # --- Config A: numpy 2x2 block-mean pre-downsample, then solve -----------
    dh, dw = height // FACTOR, width // FACTOR
    downsampled = data[: dh * FACTOR, : dw * FACTOR].reshape(dh, FACTOR, dw, FACTOR).mean(axis=(1, 3)).astype(dtype)
    header["NAXIS1"], header["NAXIS2"] = dw, dh
    a_in = tmp_path / "a_downsampled.fits"
    astropy_fits.writeto(a_in, downsampled, header, overwrite=True)
    a_out = tmp_path / "a_solved.fits"
    eff = PIXELSCALE * FACTOR
    args_a = _common_args(solve_field, index_dir, tmp_path) + [
        "--scale-low",
        f"{0.9 * eff}",
        "--scale-high",
        f"{1.1 * eff}",
        "--new-fits",
        _win_to_cygwin(str(a_out)),
        _win_to_cygwin(str(a_in)),
    ]
    _run(args_a)

    # --- Config B: solve-field --downsample 2 on the original frame ----------
    b_out = tmp_path / "b_solved.fits"
    args_b = _common_args(solve_field, index_dir, tmp_path) + [
        "--scale-low",
        f"{0.9 * PIXELSCALE}",
        "--scale-high",
        f"{1.1 * PIXELSCALE}",
        "--downsample",
        str(FACTOR),
        "--new-fits",
        _win_to_cygwin(str(b_out)),
        _win_to_cygwin(str(test_fits)),
    ]
    _run(args_b)

    assert a_out.exists() and b_out.exists()
    wcs_a = WCS(astropy_fits.getheader(a_out, 0))  # binned grid (factor 2)
    wcs_b = WCS(astropy_fits.getheader(b_out, 0))  # original full-frame grid (factor 1)

    points = {
        "center": (width / 2.0, height / 2.0),
        "BL": (1.0, 1.0),
        "BR": (float(width), 1.0),
        "TL": (1.0, float(height)),
        "TR": (float(width), float(height)),
    }
    max_corner = 0.0
    center_sep = None
    for name, (xo, yo) in points.items():
        # A lives in the binned grid -> map original pixel onto it via pixel_grid.
        ra_a, dec_a = wcs_a.wcs_pix2world(pg.orig_to_grid_fits(xo, FACTOR), pg.orig_to_grid_fits(yo, FACTOR), 1)
        # B lives in the original grid -> use the pixel directly (factor 1).
        ra_b, dec_b = wcs_b.wcs_pix2world(xo, yo, 1)
        sep = (
            SkyCoord(float(ra_a) * u.deg, float(dec_a) * u.deg)
            .separation(SkyCoord(float(ra_b) * u.deg, float(dec_b) * u.deg))
            .arcsecond
        )
        if name == "center":
            center_sep = sep
        else:
            max_corner = max(max_corner, sep)
        print(f'{name:8s} {sep:.4f}"')

    print(f'center sep = {center_sep:.4f}"  max corner = {max_corner:.4f}"')
    assert center_sep < CENTER_TOL_ARCSEC, f'center disagreement {center_sep:.3f}" too large'
    assert max_corner < CORNER_TOL_ARCSEC, f'corner disagreement {max_corner:.3f}" too large'


def test_offcenter_crpix_is_honored_and_lands_near_truth(astrometry_env, tmp_path):
    """The ROI/spec path passes an explicit, fractional, OFF-CENTER --crpix-x/y
    (from pixel_grid.roi_center_to_crpix) so the solved CRVAL is the sky at the
    fiber pixel. This exercises that invocation end-to-end.

    What is asserted, and why these two strengths:
    - **Exact (deterministic):** solve-field applies the fractional off-center
      crpix we computed -- CRPIX1/CRPIX2 in the solved header equal the requested
      values. This is the production wiring guard.
    - **Loose (OFFCENTER_CRVAL_TOL_ARCSEC):** the reported CRVAL lands near the
      sky of that pixel per an independent full-frame solve. It is loose on
      purpose: moving CRPIX off-center re-anchors solve-field's SIP fit and
      shifts CRVAL by ~0.5-1.5" vs a centered solve (a real astrometry.net
      behavior, not a refpix error). The convention's correctness is pinned to
      the milli-pixel by test_pixel_grid; the off-center *mapping* is validated
      across the frame by the corner check above. See COORDINATE_SURFACE.md.

    NOTE: no crop here -- cropping to a small field shifts the solution by a
    further few arcsec (also documented), which would swamp this check; that is
    a property of small-field solves, separate from crpix placement.
    """
    solve_field = astrometry_env["solve_field"]
    index_dir = astrometry_env["index_dir"]
    test_fits = astrometry_env["test_fits"]

    with astropy_fits.open(test_fits) as hdul:
        header = hdul[0].header.copy()
        data = hdul[0].data
        height, width = data.shape
        dtype = data.dtype

    # --- Independent ground truth: full-frame --downsample, CRPIX center -----
    # (Agrees with the numpy downsample to ~0.13" across the frame; see the
    #  corner check above. Used only to confirm CRVAL isn't grossly misplaced.)
    ref_out = tmp_path / "ref_solved.fits"
    args_ref = _common_args(solve_field, index_dir, tmp_path) + [
        "--scale-low",
        f"{0.9 * PIXELSCALE}",
        "--scale-high",
        f"{1.1 * PIXELSCALE}",
        "--downsample",
        str(FACTOR),
        "--new-fits",
        _win_to_cygwin(str(ref_out)),
        _win_to_cygwin(str(test_fits)),
    ]
    _run(args_ref)
    wcs_ref = WCS(astropy_fits.getheader(ref_out, 0))  # full-frame pixels (factor 1)

    # --- numpy 2x2 downsample (no crop), solved with an explicit off-center crpix
    dh, dw = height // FACTOR, width // FACTOR
    downsampled = data[: dh * FACTOR, : dw * FACTOR].reshape(dh, FACTOR, dw, FACTOR).mean(axis=(1, 3)).astype(dtype)
    header["NAXIS1"], header["NAXIS2"] = dw, dh
    ds_in = tmp_path / "ds.fits"
    astropy_fits.writeto(ds_in, downsampled, header, overwrite=True)

    # Off-center target (0-based original pixel), well away from the image center.
    # No crop -> crop origin is (0, 0); this is the production refpix function.
    px0, py0 = 6000, 4000
    crpix_x = pg.roi_center_to_crpix(px0, 0, FACTOR)
    crpix_y = pg.roi_center_to_crpix(py0, 0, FACTOR)

    out = tmp_path / "offcenter_solved.fits"
    eff = PIXELSCALE * FACTOR
    # Explicit --crpix-x/y (NOT --crpix-center), so build args without _common_args.
    args = [
        solve_field,
        "--scale-units",
        "arcsecperpix",
        "--scale-low",
        f"{0.9 * eff}",
        "--scale-high",
        f"{1.1 * eff}",
        "--index-dir",
        _win_to_cygwin(index_dir),
        "--no-plots",
        "--overwrite",
        "--cpulimit",
        "60",
        "--solved",
        "none",
        "--match",
        "none",
        "--rdls",
        "none",
        "--corr",
        "none",
        "--dir",
        _win_to_cygwin(str(tmp_path)),
        "--temp-dir",
        _win_to_cygwin(str(tmp_path)),
        "--crpix-x",
        str(crpix_x),
        "--crpix-y",
        str(crpix_y),
        "--new-fits",
        _win_to_cygwin(str(out)),
        _win_to_cygwin(str(ds_in)),
    ]
    _run(args)

    hdr = astropy_fits.getheader(out, 0)
    # Exact: solve-field applied the fractional off-center crpix we asked for.
    assert hdr["CRPIX1"] == pytest.approx(crpix_x, abs=1e-3)
    assert hdr["CRPIX2"] == pytest.approx(crpix_y, abs=1e-3)

    crval_ra, crval_dec = float(hdr["CRVAL1"]), float(hdr["CRVAL2"])
    ra_true, dec_true = wcs_ref.wcs_pix2world(px0 + 1, py0 + 1, 1)
    sep = (
        SkyCoord(crval_ra * u.deg, crval_dec * u.deg)
        .separation(SkyCoord(float(ra_true) * u.deg, float(dec_true) * u.deg))
        .arcsecond
    )
    print(f"off-center pixel (0-based) = ({px0},{py0})  crpix = ({crpix_x},{crpix_y})")
    print(f'CRVAL = ({crval_ra:.6f}, {crval_dec:.6f})  sep vs full-frame truth = {sep:.4f}"')
    # Loose: catches gross misplacement only (SIP re-anchoring dominates the residual).
    assert sep < OFFCENTER_CRVAL_TOL_ARCSEC, f'off-center CRVAL grossly off by {sep:.3f}"'
