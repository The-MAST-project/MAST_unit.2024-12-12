"""The star map's inputs, as a real `solve-field` writes them.

SKIPPED unless astrometry.net, the index directory and the sample frame are present, like
`test_equivalence_integration.py`; run it by hand on a unit after touching the solve-field flags
or `star_map.py` (`solvers/CLAUDE.md`, rule 3). The unit tests of the star map imitate the
`--corr` file and the extraction list with the column names the reader expects; this is the test
that those names are the real ones, and that a used star's catalogue position, projected through
the solved WCS, falls on the star that was measured.

Solves the sample frame the way `MastrometryDotNet` does (numpy 2x2 pre-downsample, tweak left on)
with `--corr <path>` instead of `--corr none`.
"""

import numpy as np
import pytest
import star_map

from .test_equivalence_integration import FACTOR, PIXELSCALE, _common_args, _run, _win_to_cygwin

astropy_fits = pytest.importorskip("astropy.io.fits")

#: A matched star lands within this many pixels of where it was measured, through the solved WCS.
#: Generous: it catches a wrong column or a wrong pixel origin (a half-pixel or a swapped axis
#: gives pixels of rms), not the arcsecond-level fit quality.
RESIDUAL_RMS_TOLERANCE_PX = 1.5
MIN_USED_STARS = 8


def test_the_corr_and_the_extraction_list_have_the_columns_the_star_map_reads(astrometry_env, tmp_path):
    solve_field = astrometry_env["solve_field"]
    with astropy_fits.open(astrometry_env["test_fits"]) as hdul:
        header = hdul[0].header.copy()
        data = hdul[0].data
        dtype = data.dtype
        dh, dw = data.shape[0] // FACTOR, data.shape[1] // FACTOR
    downsampled = data[: dh * FACTOR, : dw * FACTOR].reshape(dh, FACTOR, dw, FACTOR).mean(axis=(1, 3)).astype(dtype)
    header["NAXIS1"], header["NAXIS2"] = dw, dh
    frame = tmp_path / "downsampled_frame.fits"
    astropy_fits.writeto(frame, downsampled, header, overwrite=True)

    solved = tmp_path / "solved.fits"
    corr = tmp_path / "solved.corr"
    effective = PIXELSCALE * FACTOR
    args = _common_args(solve_field, astrometry_env["index_dir"], tmp_path)
    args[args.index("--corr") + 1] = _win_to_cygwin(str(corr))
    args += [
        "--scale-low",
        f"{0.9 * effective}",
        "--scale-high",
        f"{1.1 * effective}",
        "--new-fits",
        _win_to_cygwin(str(solved)),
        _win_to_cygwin(str(frame)),
    ]
    _run(args)

    # the correspondences: the used stars
    matched = star_map.read_matched(corr)
    assert matched is not None, "no usable --corr: see the warning for the columns solve-field wrote"
    assert len(matched.x) >= MIN_USED_STARS
    print(f"used stars: {len(matched.x)}; match_weight present: {matched.weight is not None}")

    # a used star's catalogue position, through the solved WCS, lands on the star that was measured
    rx, ry, crpix, scale = star_map.residuals(matched, solved)
    rms = float(np.sqrt(np.mean(rx**2 + ry**2)))
    print(f"residual rms {rms:.3f} px ({rms * scale:.3f} arcsec); crpix {crpix}; scale {scale:.4f} arcsec/px")
    assert rms < RESIDUAL_RMS_TOLERANCE_PX
    assert scale == pytest.approx(effective, rel=0.15)

    # the extraction list: every source image2xy found, a superset of the used stars
    extracted = star_map.read_extracted(tmp_path)
    assert extracted is not None, "no .axy / .xyls with X and Y columns in the scratch folder"
    assert len(extracted) >= len(matched.x)

    # and the whole map is made from them
    summary = star_map.write_star_map(frame, corr, solved, tmp_path, tmp_path / "map.jpg", tmp_path / "map.json")
    assert summary is not None and (tmp_path / "map.jpg").stat().st_size > 0
    print(summary)
