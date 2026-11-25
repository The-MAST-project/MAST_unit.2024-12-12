"""The prototype's stages 0 to 2: an array described, brought to the 2x2 solve grid and cropped to the region -- and the
selection run on that grid finding the stars the full-resolution selection finds. Synthetic frames only."""

from __future__ import annotations

import numpy as np
import pytest
from scipy import ndimage
from test_source_selection import NOISE, SKY, _star

from imaging.hot_pixels import HotPixels
from solvers.pixel_grid import orig_to_grid_fits, roi_center_to_crpix
from solvers.solve_grid import FrameInfo, SolveGrid, binned_params, select_on_grid, to_solve_grid
from solvers.source_selection import SelectionParams, select_sources

FULL = (400, 600)  # (ny, nx) of the pretend sensor
SAT = 60000.0


def _hot(*xy) -> HotPixels:
    x, y = np.array([p[0] for p in xy]), np.array([p[1] for p in xy])
    return HotPixels(x=x, y=y, excess=np.full(len(x), 5000.0), frames=np.full(len(x), 10), image_shape=FULL)


def _flat(value=100.0, shape=FULL) -> np.ndarray:
    return np.full(shape, value)


class TestCoordinates:
    def test_the_two_directions_agree(self):
        grid = SolveGrid(np.zeros((50, 60)), origin=(40, 20))
        gx, gy = np.array([0.0, 7.5, 59.0]), np.array([0.0, 3.0, 49.0])

        x, y = grid.to_full(gx, gy)
        back_x, back_y = grid.to_grid(x, y)

        assert np.allclose(back_x, gx) and np.allclose(back_y, gy)
        assert x[0] == 40.5 and y[0] == 20.5  # the first block's centre: its two pixels are 40 and 41

    def test_it_is_pixel_grids_mapping(self):
        grid = SolveGrid(np.zeros((50, 60)), origin=(0, 0))
        x = np.array([0.0, 11.0, 300.0])

        gx, _ = grid.to_grid(x, x)

        assert np.allclose(gx + 1.0, [orig_to_grid_fits(v + 1.0, 2) for v in x])  # FITS 1-based both ways

    def test_the_fibre_as_crpix_is_what_the_unit_computes_for_an_roi(self):
        info = FrameInfo(binning=1, full_shape=FULL, origin=(100, 60))
        grid = to_solve_grid(_flat(shape=(120, 200)), info)

        crpix = grid.fibre_fits((180, 110))

        assert crpix == pytest.approx((roi_center_to_crpix(180, 100, 2), roi_center_to_crpix(110, 60, 2)))


class TestBinningOne:
    def test_blocks_are_averaged(self):
        frame = np.arange(FULL[0] * FULL[1], dtype=float).reshape(FULL)

        grid = to_solve_grid(frame, FrameInfo(binning=1, full_shape=FULL))

        assert grid.shape == (200, 300)
        assert grid.data[0, 0] == frame[:2, :2].mean() and grid.data[7, 9] == frame[14:16, 18:20].mean()

    def test_an_odd_origin_drops_the_first_row_and_column_and_keeps_the_sensors_blocks(self):
        frame = np.arange(7 * 9, dtype=float).reshape(7, 9)

        grid = to_solve_grid(frame, FrameInfo(binning=1, full_shape=FULL, origin=(11, 5)))

        assert grid.origin == (12, 6)  # blocks start on even sensor pixels
        assert grid.shape == (3, 4)  # 6 rows and 8 columns are left, then halved
        assert grid.data[0, 0] == frame[1:3, 1:3].mean()

    def test_a_star_keeps_its_position(self):
        frame = np.full(FULL, SKY)
        _star(frame, 301.3, 177.6, 3e5)

        grid = to_solve_grid(frame, FrameInfo(binning=1, full_shape=FULL))
        gy, gx = ndimage.center_of_mass(grid.data - SKY, labels=None)
        x, y = grid.to_full(gx, gy)

        assert (float(x), float(y)) == pytest.approx((301.3, 177.6), abs=0.1)

    def test_a_hot_pixel_is_filled_before_it_is_averaged_in(self):
        frame = _flat()
        frame[41, 80] = 60000.0
        hot = _hot((80, 41))

        with_map = to_solve_grid(frame, FrameInfo(binning=1, full_shape=FULL), hot)
        without = to_solve_grid(frame, FrameInfo(binning=1, full_shape=FULL))

        assert with_map.data[20, 40] == pytest.approx(100.0) and with_map.hot_filled == 1
        assert without.data[20, 40] > 15000  # binned in, it is a quarter of 60000

    def test_a_map_for_another_sensor_is_ignored(self):
        frame = _flat()
        frame[41, 80] = 60000.0
        other = HotPixels(
            x=np.array([80]), y=np.array([41]), excess=np.array([1.0]), frames=np.array([1]), image_shape=(10, 10)
        )

        grid = to_solve_grid(frame, FrameInfo(binning=1, full_shape=FULL), other)

        assert grid.hot_filled == 0 and grid.data[20, 40] > 15000

    def test_a_block_with_a_saturated_pixel_reads_as_saturated(self):
        frame = _flat()
        frame[41, 80] = 65535.0

        grid = to_solve_grid(frame, FrameInfo(binning=1, full_shape=FULL, saturation_adu=SAT))

        assert grid.data[20, 40] == SAT and grid.saturated_blocks == 1  # a mean of 16534 would have hidden it


class TestBinningTwo:
    def test_the_array_is_used_as_delivered(self):
        frame = np.random.default_rng(1).normal(100, 5, (200, 300))

        grid = to_solve_grid(frame, FrameInfo(binning=2, full_shape=FULL))

        assert np.array_equal(grid.data, frame) and grid.origin == (0, 0)

    def test_a_hot_pixel_fills_its_binned_pixel_from_the_binned_neighbours(self):
        frame = np.full((200, 300), 100.0)
        frame[20, 40] = 9000.0  # the binned pixel holding the unbinned hot pixel (80, 41)

        grid = to_solve_grid(frame, FrameInfo(binning=2, full_shape=FULL), _hot((80, 41)))

        assert grid.data[20, 40] == pytest.approx(100.0) and grid.hot_filled == 1

    def test_an_origin_off_the_sensors_blocks_is_refused(self):
        with pytest.raises(ValueError, match="start on a block of the sensor"):
            to_solve_grid(np.zeros((10, 10)), FrameInfo(binning=2, full_shape=FULL, origin=(3, 4)))

    def test_only_one_or_two(self):
        with pytest.raises(ValueError, match="binning must be 1 or 2"):
            to_solve_grid(np.zeros((10, 10)), FrameInfo(binning=3, full_shape=FULL))


class TestRegion:
    def test_the_crop_keeps_the_pixels_where_they_were(self):
        frame = np.arange(FULL[0] * FULL[1], dtype=float).reshape(FULL)
        whole = to_solve_grid(frame, FrameInfo(binning=1, full_shape=FULL))

        cropped = to_solve_grid(frame, FrameInfo(binning=1, full_shape=FULL), region=(300, 200, 100, 60))

        # centre (300, 200), half sizes (100, 60): the crop starts at unbinned (200, 140), which is grid pixel (100, 70)
        assert cropped.shape == (60, 100) and cropped.origin == (200, 140)
        x_whole, y_whole = whole.to_full(160.0, 80.0)
        x_crop, y_crop = cropped.to_full(160.0 - 100, 80.0 - 70)  # the same grid pixel, in the crop's array
        assert (float(x_whole), float(y_whole)) == (float(x_crop), float(y_crop))
        assert cropped.data[0, 0] == whole.data[70, 100]

    def test_a_region_past_the_frame_is_clipped(self):
        grid = to_solve_grid(_flat(), FrameInfo(binning=1, full_shape=FULL), region=(590, 390, 200, 200))

        assert grid.origin == (390, 190) and grid.shape == (105, 105)


def test_the_pixel_parameters_follow_the_grid():
    p = binned_params(SelectionParams(seeing_scale=1.0))

    assert p.box_size == 25 and p.default_fwhm_px == pytest.approx(3.8)
    assert p.min_area == 2 and p.edge_margin_extra_px == 2.5 and p.coma_px == 1.0
    assert p.npixels == SelectionParams(seeing_scale=1.0).npixels == 5  # not scaled: it let in 68,000 noise blobs a frame
    assert (p.nsigma, p.min_snr) == (2.0, 15.0)  # signal to noise is not in pixels


# ------------------------------------------------------------------------------------------------ the selection on the grid


def _field(seed=2):
    """A 1800 x 2400 sensor with 20 planted stars on a grid and a few hot pixels, and where they are."""
    ny, nx = 1800, 2400
    rng = np.random.default_rng(seed)
    frame = SKY + rng.normal(0, NOISE, (ny, nx))
    planted = []
    for i, x in enumerate(np.linspace(250, 2150, 5)):
        for j, y in enumerate(np.linspace(300, 1500, 4)):
            px, py = x + 0.37 * ((i * 7 + j) % 5), y + 0.29 * ((j * 5 + i) % 4)  # off the pixel grid
            _star(frame, px, py, 2e5)
            planted.append((px, py))
    hx, hy = np.array([500, 1300, 900]), np.array([400, 900, 1400])
    frame[hy, hx] += 4000.0
    hot = HotPixels(x=hx, y=hy, excess=np.full(3, 4000.0), frames=np.full(3, 9), image_shape=(ny, nx))
    return frame, planted, hot


def test_the_grid_selection_finds_the_stars_the_full_resolution_one_does():
    frame, planted, hot = _field()
    params = SelectionParams(seeing_scale=1.0, n_target=20, n_max=40, min_snr=25.0, grid=(2, 2))
    fibre, radius = (1200.0, 900.0), 1500.0
    full = select_sources(
        hot.fill(frame), optical_center=fibre, low_coma_radius=radius, params=params
    )  # the full-resolution frame, as the stage selects today
    grid = to_solve_grid(frame, FrameInfo(binning=1, full_shape=frame.shape), hot)

    binned = select_on_grid(grid, params, fibre_xy=fibre, low_coma_radius_px=radius)

    assert full.status == binned.status == "ok"
    full_xy = np.column_stack([full.x[full.kept], full.y[full.kept]])
    bx, by = grid.to_full(binned.x[binned.kept], binned.y[binned.kept])
    nearest = np.array([np.min(np.hypot(full_xy[:, 0] - x, full_xy[:, 1] - y)) for x, y in zip(bx, by, strict=True)])
    assert binned.kept.sum() >= 18 and (nearest < 0.5).mean() >= 0.9  # the same stars, to half a pixel of the sensor


def test_the_grid_selection_recovers_the_planted_positions():
    frame, planted, hot = _field()
    params = SelectionParams(seeing_scale=1.0, n_target=20, n_max=40, min_snr=25.0, grid=(2, 2))
    grid = to_solve_grid(frame, FrameInfo(binning=1, full_shape=frame.shape), hot)

    sel = select_on_grid(grid, params, fibre_xy=(1200.0, 900.0), low_coma_radius_px=1500.0)

    x, y = grid.to_full(sel.x[sel.kept], sel.y[sel.kept])
    errors = [min(np.hypot(px - a, py - b) for px, py in planted) for a, b in zip(x, y, strict=True)]
    assert len(errors) >= 18 and max(errors) < 0.3


def test_a_cropped_grid_selects_in_the_same_sky():
    frame, planted, hot = _field()
    params = SelectionParams(seeing_scale=1.0, n_target=10, n_max=40, min_snr=25.0, grid=(2, 2))
    info = FrameInfo(binning=1, full_shape=frame.shape)
    grid = to_solve_grid(frame, info, hot, region=(1200.0, 900.0, 1000.0, 700.0))

    sel = select_on_grid(grid, params, fibre_xy=(1200.0, 900.0), low_coma_radius_px=900.0)

    x, y = grid.to_full(sel.x[sel.kept], sel.y[sel.kept])
    assert sel.kept.sum() >= 10
    assert all(min(np.hypot(px - a, py - b) for px, py in planted) < 0.3 for a, b in zip(x, y, strict=True))
    assert all(200 <= v <= 2200 for v in x) and all(200 <= v <= 1600 for v in y)  # inside the region


class TestCropBeforeBinning:
    def test_it_is_the_grid_of_binning_the_whole_frame_and_cropping(self):
        rng = np.random.default_rng(3)
        frame = rng.normal(1000.0, 50.0, FULL)
        frame[100, 200] = 65535.0  # a saturated pixel: its block reads as saturated either way
        hot = _hot((205, 103), (400, 300))
        info = FrameInfo(binning=1, full_shape=FULL, saturation_adu=SAT)
        region = (300.0, 200.0, 100.0, 60.0)

        cropped = to_solve_grid(frame, info, hot, region)
        whole = to_solve_grid(frame, info, hot)
        gx, gy = (cropped.origin[0] - whole.origin[0]) // 2, (cropped.origin[1] - whole.origin[1]) // 2
        same = whole.data[gy : gy + cropped.shape[0], gx : gx + cropped.shape[1]]

        assert cropped.shape == (60, 100)
        assert np.array_equal(cropped.data, same)

    def test_only_the_regions_hot_pixels_are_filled(self):
        info = FrameInfo(binning=1, full_shape=FULL)
        hot = _hot((205, 103), (400, 300))

        grid = to_solve_grid(_flat(), info, hot, region=(210.0, 110.0, 20.0, 20.0))

        assert grid.hot_filled == 1

    def test_the_block_mean_is_the_same_numbers_as_the_reshaped_mean(self):
        rng = np.random.default_rng(4)
        frame = rng.integers(0, 60000, FULL).astype(float)

        grid = to_solve_grid(frame, FrameInfo(binning=1, full_shape=FULL))

        blocks = frame.reshape(FULL[0] // 2, 2, FULL[1] // 2, 2).mean(axis=(1, 3))
        assert np.allclose(grid.data, blocks, rtol=0, atol=1e-9)


def test_the_grid_selection_looks_only_in_the_candidate_region():
    frame, _planted, hot = _field()
    params = SelectionParams(seeing_scale=1.0, n_target=10, n_max=40, min_snr=25.0, grid=(2, 2))
    grid = to_solve_grid(frame, FrameInfo(binning=1, full_shape=frame.shape), hot)
    fibre, radius = (1200.0, 900.0), 700.0

    sel = select_on_grid(grid, params, fibre_xy=fibre, low_coma_radius_px=radius)

    x, y = grid.to_full(sel.x, sel.y)
    assert sel.kept.sum() >= 10
    assert sel.reach_px is not None
    assert (np.hypot(x - fibre[0], y - fibre[1]) <= 1.7 * radius + 60.0).all()


class TestBinnedShadow:
    def test_a_binned_model_reads_the_same_deficit_at_the_same_place(self):
        from imaging.mirror_shadow import ShadowModel
        from solvers.source_selection import _shadow_deficit

        ny, nx = 400, 600
        s = np.arange(-400.0, 401.0, 8.0)
        model = ShadowModel(
            present=True,
            image_shape=(ny, nx),
            angle=np.pi / 2 - 0.05,
            offset=30.0,
            umbra_half_width=40.0,
            penumbra_half_width=90.0,
            depth=0.5,
            prominence=20.0,
            s=s,
            profile=np.clip(0.5 * (90.0 - np.abs(s - 30.0)) / 50.0, 0.0, 0.5),
            umbra_s=(-10.0, 70.0),
            penumbra_s=(-60.0, 120.0),
        )
        x = np.array([100.0, 290.0, 330.0, 520.0])
        y = np.array([50.0, 200.0, 350.0, 20.0])

        full, _ = _shadow_deficit(model, x, y, (ny, nx))
        grid, _ = _shadow_deficit(model.binned(2), (x - 0.5) / 2, (y - 0.5) / 2, (ny // 2, nx // 2))

        assert np.allclose(full, grid, atol=0.02)
        assert model.binned(2).image_shape == (200, 300) and model.binned(2).penumbra_s == (-30.0, 60.0)

    def test_an_absent_shadow_stays_absent(self):
        from imaging.mirror_shadow import ShadowModel

        assert not ShadowModel(present=False, image_shape=(400, 600)).binned(2).present


class TestSeeingDoesNotDependOnThePixels:
    """A star is as wide on 2x2 blocks as on the sensor's pixels. The half-maximum pixel count this replaced read 21-33%
    wider on blocks, which widened the centroid window and moved the fibre ~0.06 arcsec."""

    def test_the_seeing_of_the_grid_is_the_seeing_of_the_frame(self):
        frame, _planted, hot = _field()
        uncapped = {"reach_factor": None, "reach_factor_closed_shadow": None}
        params = SelectionParams(seeing_scale=1.0, n_target=20, n_max=40, min_snr=25.0, grid=(2, 2), **uncapped)
        full = select_sources(
            hot.fill(frame), optical_center=(1200.0, 900.0), low_coma_radius=1500.0, params=params, windowed=False
        )
        grid = to_solve_grid(frame, FrameInfo(binning=1, full_shape=frame.shape), hot)

        binned = select_on_grid(grid, params, fibre_xy=(1200.0, 900.0), low_coma_radius_px=1500.0)

        assert 2.0 * binned.fwhm_seeing_px == pytest.approx(full.fwhm_seeing_px, rel=0.05)
        assert 2.0 * binned.sigma_window_px == pytest.approx(full.sigma_window_px, rel=0.05)

    @pytest.mark.parametrize("fwhm", [6.0, 7.5, 10.0, 13.0])
    def test_the_width_of_a_gaussian_star_is_its_fwhm_on_either_pixels(self, fwhm):
        import sep

        sigma = fwhm / 2.3548
        ys, xs = np.mgrid[0:120, 0:120].astype(float)
        star = np.zeros((120, 120))
        for sy in np.linspace(-0.4, 0.4, 5):
            for sx in np.linspace(-0.4, 0.4, 5):
                star += np.exp(-((xs + sx - 60.3) ** 2 + (ys + sy - 60.7) ** 2) / (2 * sigma**2)) / 25.0
        binned = star.reshape(60, 2, 60, 2).mean(axis=(1, 3))

        full_r, _ = sep.flux_radius(star.astype(np.float32), np.array([60.3]), np.array([60.7]), np.array([25.0]), 0.5)
        grid_r, _ = sep.flux_radius(binned.astype(np.float32), np.array([29.9]), np.array([30.1]), np.array([12.5]), 0.5)

        assert 2.0 * full_r[0] == pytest.approx(fwhm, rel=0.03)  # a Gaussian: twice its half-light radius is its FWHM
        assert 4.0 * grid_r[0] == pytest.approx(fwhm, rel=0.04)  # and the same on 2x2 blocks (their radius is in blocks)
