"""Pure-math drift guard for the binned/full-frame coordinate surface.

Runs ANYWHERE -- no astrometry.net, no MAST runtime. This is the primary
canary: if anyone reintroduces the naive ``orig/factor`` mapping or the
integer-division ``refpix``, these assertions fail immediately.

See ``solvers/pixel_grid.py`` and ``solvers/COORDINATE_SURFACE.md``.
"""

import pytest

import pixel_grid as pg

ARCSEC_PER_ORIG_PX = 0.2616  # for translating pixel errors to sky, documentation only


# --- the validated convention -------------------------------------------------


def test_crpix_center_matches_naxis_plus_1_over_2():
    # solve-field --crpix-center yields (NAXIS+1)/2 on each grid. For the full
    # 8288-wide frame the original center is 4144.5; binned 2x it must be 2072.5
    # (the value observed in the equivalence study, report.tweak.txt Config A).
    orig_center = (8288 + 1) / 2.0
    assert pg.orig_to_grid_fits(orig_center, 2) == pytest.approx(2072.5)
    orig_center_y = (5644 + 1) / 2.0
    assert pg.orig_to_grid_fits(orig_center_y, 2) == pytest.approx(1411.5)


def test_factor_one_is_identity():
    for v in (1.0, 100.5, 4144.5):
        assert pg.orig_to_grid_fits(v, 1) == v
        assert pg.grid_to_orig_fits(v, 1) == v


@pytest.mark.parametrize("factor", [2, 3, 4])
@pytest.mark.parametrize("orig", [1.0, 250.0, 4144.5, 8288.0])
def test_round_trip(orig, factor):
    grid = pg.orig_to_grid_fits(orig, factor)
    assert pg.grid_to_orig_fits(grid, factor) == pytest.approx(orig)


def test_naive_division_is_rejected():
    # The tempting orig/factor is wrong by (factor-1)/(2*factor) binned px.
    orig = 1000.0
    correct = pg.orig_to_grid_fits(orig, 2)
    naive = orig / 2
    assert correct - naive == pytest.approx(0.25)  # 0.25 binned px == 0.5 orig px ~= 0.13"


# --- the ROI refpix fix (the bug that was live in mastrometry.py) -------------


def test_roi_center_to_crpix_known_value():
    # ROI center at original 0-based index 2000, crop origin at 1000, factor 2.
    # Cropped FITS coord = (2000 - 1000) + 1 = 1001; binned = (1001 + 0.5)/2 = 500.75.
    assert pg.roi_center_to_crpix(2000, 1000, 2) == pytest.approx(500.75)


def test_roi_refpix_beats_old_integer_division():
    center0, start0, factor = 2000, 1000, 2
    new_crpix = pg.roi_center_to_crpix(center0, start0, factor)
    old_crpix = (center0 - start0) // factor  # the previous, buggy code

    # Translate each CRPIX back to the cropped-frame original pixel it points at.
    true_cropped_fits = (center0 - start0) + 1
    new_points_at = pg.grid_to_orig_fits(new_crpix, factor)
    old_points_at = pg.grid_to_orig_fits(old_crpix, factor)

    # The fixed mapping is exact; the old one is off by ~1.5 original px (~0.4").
    assert new_points_at == pytest.approx(true_cropped_fits)
    err_orig_px = abs(old_points_at - true_cropped_fits)
    assert err_orig_px == pytest.approx(1.5)
    assert err_orig_px * ARCSEC_PER_ORIG_PX == pytest.approx(0.3924, abs=1e-3)


@pytest.mark.parametrize("factor", [0, -1])
def test_invalid_factor_raises(factor):
    with pytest.raises(ValueError):
        pg.orig_to_grid_fits(10.0, factor)
    with pytest.raises(ValueError):
        pg.grid_to_orig_fits(10.0, factor)
