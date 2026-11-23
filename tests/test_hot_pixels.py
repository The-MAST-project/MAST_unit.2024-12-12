"""Hot pixels: found by persistence across frames of one camera, filled before detection (imaging/hot_pixels.py)."""

from __future__ import annotations

import numpy as np
import pytest
from astropy.io import fits

from imaging.hot_pixels import HotPixels, calibration_path, fill_hot_pixels, find_hot_pixels
from solvers.source_selection import window_centroid

NY, NX = 300, 400
HOT = [(25, 30), (120, 44), (200, 100), (350, 250), (77, 281), (310, 12), (5, 150), (390, 290)]  # (x, y)


def _frame(seed: int, hot=HOT, static_star=True, n_stars=12) -> np.ndarray:
    """Noise on a flat sky, `n_stars` stars at places that change from frame to frame, a bright star that does
    not (a static field), and the camera's hot pixels."""
    rng = np.random.default_rng(seed)
    image = rng.normal(1000.0, 30.0, (NY, NX))
    yy, xx = np.mgrid[0:NY, 0:NX]

    def star(x, y, amplitude, sigma):
        image[...] += amplitude * np.exp(-((xx - x) ** 2 + (yy - y) ** 2) / (2 * sigma**2))

    for _ in range(n_stars):
        star(rng.uniform(20, NX - 20), rng.uniform(20, NY - 20), rng.uniform(1500, 8000), rng.uniform(1.5, 3.0))
    if static_star:
        star(150.0, 200.0, 20000.0, 1.5)
    for x, y in hot:
        image[y, x] += 3000.0
    return np.clip(image, 0, 65535).astype(np.uint16)


def _frames(n=8, **kw):
    return [_frame(seed, **kw) for seed in range(n)]


def test_the_planted_hot_pixels_are_found_and_nothing_else():
    found = find_hot_pixels(_frames())
    assert set(zip(found.x.tolist(), found.y.tolist(), strict=True)) == set(HOT)
    assert found.image_shape == (NY, NX)
    assert np.all(found.frames == 8)
    assert np.allclose(found.excess, 3000.0, atol=400.0)


def test_a_bright_star_that_is_in_every_frame_is_not_a_hot_pixel():
    """A static field: the star's peak is high in all frames, but so are its neighbours."""
    found = find_hot_pixels(_frames(), min_fraction=0.8)
    cx, cy = 150, 200
    assert not any(abs(x - cx) <= 3 and abs(y - cy) <= 3 for x, y in zip(found.x, found.y, strict=True))


def test_persistence_is_what_separates_a_hot_pixel_from_a_lucky_spike():
    """In 7 of 8 frames (88%) it is hot; in 3 of 8 (38%) it is not."""
    frames = _frames(8, hot=HOT[:6])
    for i, frame in enumerate(frames):
        if i != 0:
            frame[100, 200] = np.uint16(min(65535, int(frame[100, 200]) + 0))  # (200, 100) is in HOT[:6]: no change
    often, rarely = (60, 60), (61, 200)
    for i, frame in enumerate(frames):
        if i < 7:
            frame[often[1], often[0]] = np.uint16(min(65535, int(frame[often[1], often[0]]) + 3000))
        if i < 3:
            frame[rarely[1], rarely[0]] = np.uint16(min(65535, int(frame[rarely[1], rarely[0]]) + 3000))
    found = find_hot_pixels(frames)
    pixels = set(zip(found.x.tolist(), found.y.tolist(), strict=True))
    assert often in pixels
    assert rarely not in pixels


def test_too_few_frames_cannot_tell():
    with pytest.raises(ValueError, match="at least"):
        find_hot_pixels(_frames(3))


def test_frames_of_different_sizes_are_refused():
    frames = _frames(5)
    frames[2] = frames[2][:-1]
    with pytest.raises(ValueError, match="different sizes"):
        find_hot_pixels(frames)


def test_frames_may_be_fits_paths_and_the_provenance_is_kept(tmp_path):
    paths = []
    for i, frame in enumerate(_frames(6)):
        header = fits.Header({"EXPOSURE": 5.0 + i, "CCD-TEMP": 4.0 + 0.5 * i, "INSTRUME": "ZWO ASI294MM Pro"})
        path = tmp_path / f"f{i}.fits"
        fits.PrimaryHDU(frame, header).writeto(path)
        paths.append(path)
    found = find_hot_pixels(paths)
    assert len(found) == len(HOT)
    assert found.meta["n_frames"] == 6
    assert found.meta["camera"] == "ZWO ASI294MM Pro"
    assert found.meta["exposure_s"] == [5.0, 10.0]
    assert found.meta["ccd_temp_c"] == [4.0, 6.5]


# -- filling -----------------------------------------------------------------------------------------------------


def test_a_hot_pixel_is_replaced_by_the_median_of_its_neighbours():
    data = np.full((20, 30), 1000, dtype=np.uint16)
    data[:, 10] = 900  # a column the median of the 5x5 square sees
    data[8, 12] = 40000
    out = fill_hot_pixels(data, np.array([12]), np.array([8]))
    assert out.dtype == np.uint16
    assert out[8, 12] == 1000
    assert data[8, 12] == 40000  # the input is left alone


def test_a_neighbouring_hot_pixel_is_not_taken_for_sky():
    data = np.full((20, 30), 1000, dtype=np.float64)
    data[8, 12] = 40000.0
    data[8, 13] = 50000.0
    out = fill_hot_pixels(data, np.array([12, 13]), np.array([8, 8]))
    assert out[8, 12] == pytest.approx(1000.0)
    assert out[8, 13] == pytest.approx(1000.0)


def test_pixels_at_the_edge_and_outside_the_array():
    data = np.full((20, 30), 1000, dtype=np.float64)
    data[0, 0] = 40000.0
    out = fill_hot_pixels(data, np.array([0, 99, -3]), np.array([0, 5, 5]))
    assert out[0, 0] == pytest.approx(1000.0)


def test_a_crop_is_filled_with_the_maps_pixels_that_fall_inside_it():
    full = _frame(1)
    hp = HotPixels(
        x=np.array([x for x, _ in HOT]), y=np.array([y for _, y in HOT]), excess=np.full(len(HOT), 3000.0),
        frames=np.full(len(HOT), 8), image_shape=(NY, NX),
    )  # fmt: skip
    crop = full[20:90, 100:180]  # holds (120, 44) only: (200, 100) is to the right of it, (25, 30) to the left
    filled = hp.fill(crop, origin=(100, 20))
    changed = np.argwhere(filled != crop)
    assert [tuple(c) for c in changed] == [(44 - 20, 120 - 100)]


def test_a_hot_pixel_in_a_stars_window_no_longer_pulls_its_centroid():
    rng = np.random.default_rng(3)
    yy, xx = np.mgrid[0:60, 0:60]
    star = 5000.0 * np.exp(-((xx - 30.0) ** 2 + (yy - 30.0) ** 2) / (2 * 2.0**2)) + rng.normal(0, 5, (60, 60))
    hot = star.copy()
    hot[30, 34] += 30000.0
    bias_x, bias_y, *_ = window_centroid(hot, 30.0, 30.0, 2.0)
    x0, y0, *_ = window_centroid(star, 30.0, 30.0, 2.0)
    fx, fy, *_ = window_centroid(fill_hot_pixels(hot, np.array([34]), np.array([30])), 30.0, 30.0, 2.0)
    assert abs(bias_x - x0) > 0.5  # the hot pixel drags it
    assert abs(fx - x0) < 0.1 and abs(fy - y0) < 0.1


# -- storing -----------------------------------------------------------------------------------------------------


def test_a_map_round_trips_through_its_json_file(tmp_path):
    found = find_hot_pixels(_frames())
    path = calibration_path(tmp_path / "mast01")
    assert path.parts[-2:] == ("Calibration", "hot_pixels.json")
    found.save(path)
    loaded = HotPixels.load(path)
    assert set(zip(loaded.x.tolist(), loaded.y.tolist(), strict=True)) == set(HOT)
    assert loaded.image_shape == (NY, NX)
    assert loaded.meta["n_frames"] == 8


def test_a_map_of_another_version_is_refused():
    with pytest.raises(ValueError, match="version"):
        HotPixels.from_dict({"version": 99})


def test_a_map_is_for_frames_of_the_size_it_was_built_on():
    hp = HotPixels(x=np.array([1]), y=np.array([1]), excess=np.array([1.0]), frames=np.array([1]), image_shape=(NY, NX))
    assert hp.matches((NY, NX))
    assert not hp.matches((NY // 2, NX // 2))


# -- the build tool: only frames at the sensor's set temperature --------------------------------------------------


def _write(path, ccd, setpoint):
    header = fits.Header()
    if ccd is not None:
        header["CCD-TEMP"] = ccd
    if setpoint is not None:
        header["SET-TEMP"] = setpoint
    fits.PrimaryHDU(np.zeros((8, 8), dtype=np.uint16), header).writeto(path)
    return path


def test_only_frames_at_their_set_temperature_are_used(tmp_path):
    import sys

    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "tools"))
    from build_hot_pixels import at_operating_temperature

    cold = _write(tmp_path / "cold.fits", 4.9, 5.0)
    warm = _write(tmp_path / "warm.fits", 29.7, 5.0)
    none = _write(tmp_path / "none.fits", None, 5.0)
    kept, notes = at_operating_temperature([cold, warm, none], 2.0)
    assert kept == [cold]
    assert any("warm.fits" in n and "29.7" in n for n in notes) and any("none.fits" in n for n in notes)


def test_a_camera_that_reports_no_temperature_keeps_all_its_frames(tmp_path):
    from build_hot_pixels import at_operating_temperature

    frames = [_write(tmp_path / f"{i}.fits", None, None) for i in range(3)]
    kept, notes = at_operating_temperature(frames, 2.0)
    assert kept == frames and notes == []


def test_the_temperature_test_can_be_turned_off(tmp_path):
    from build_hot_pixels import at_operating_temperature

    warm = _write(tmp_path / "warm.fits", 29.7, 5.0)
    assert at_operating_temperature([warm], 0.0) == ([warm], [])


def test_two_populations_of_hot_pixels_are_reported_as_such():
    """Eight frames with one set of hot pixels and eight with another (a camera whose pixels changed): none is
    persistent in 80% of the frames, but many are in half of them -- the signature the build tool warns about."""
    first = [(20, 20), (60, 40), (100, 80), (140, 120), (180, 160), (220, 200), (260, 240), (300, 280)]
    second = [(30, 30), (70, 50), (110, 90), (150, 130), (190, 170), (230, 210), (270, 250), (310, 290)]
    frames = _frames(8, hot=first) + [_frame(100 + i, hot=second) for i in range(8)]
    found = find_hot_pixels(frames)
    assert len(found) == 0
    # half of 16 frames is 8: a pixel in 7 of its set's 8 frames falls just short, so allow for one or two
    assert len(first) + len(second) - 2 <= found.meta["persistent_in_half"] <= len(first) + len(second)
    assert found.meta["max_persistence"] == 8


def test_one_population_has_no_such_warning_signature():
    found = find_hot_pixels(_frames(8))
    assert found.meta["persistent_in_half"] == len(found) == len(HOT)
    assert found.meta["max_persistence"] == 8


def test_frames_are_grouped_into_nights_and_the_recent_ones_chosen(tmp_path):
    from build_hot_pixels import last_nights, night_of

    def frame(name, stamp):
        path = tmp_path / name
        fits.PrimaryHDU(np.zeros((4, 4), dtype=np.uint16), fits.Header({"DATE-OBS": stamp})).writeto(path)
        return path

    a = frame("a.fits", "2026-11-15T20:00:00")
    b = frame("b.fits", "2026-11-16T03:00:00")  # the small hours belong to the night that began the day before
    c = frame("c.fits", "2026-11-16T21:00:00")
    d = frame("d.fits", "2026-11-17T22:30:00")
    assert night_of(a) == night_of(b) == "2026-11-15"
    assert night_of(c) == "2026-11-16"
    assert last_nights([a, b, c, d], 2) == [c, d]
    assert last_nights([a, b, c, d], 1) == [d]
