"""`sep` as the source detector (the default): the same contract as `extract_sources`, and the same stars chosen."""

from __future__ import annotations

import numpy as np
import pytest
from test_selection_window import CENTER, LOW_COMA, _stars, _vertical_shadow

from calibration.analysis.optical_center import extract_sources
from solvers.sep_detect import extract_sources_sep
from solvers.source_selection import SelectionParams, select_sources

PARAMS = SelectionParams(n_target=12, min_snr=20.0, box_size=50, detector="photutils")  # the reference
SEP = SelectionParams(n_target=12, min_snr=20.0, box_size=50)


def _kept(selection):
    return {
        (round(float(x), 1), round(float(y), 1))
        for x, y, k in zip(selection.x, selection.y, selection.kept, strict=True)
        if k
    }


def test_it_returns_what_extract_sources_returns():
    frame, _ = _stars()

    found = extract_sources_sep(frame, nsigma=2.0, npixels=5, box_size=50, local_noise=True)
    reference = extract_sources(frame, nsigma=2.0, npixels=5, box_size=50, local_noise=True)

    assert found is not None and reference is not None
    assert set(found) == set(reference)
    n = found["n_detected"]
    for key in ("x", "y", "theta", "ellipticity", "area", "flux", "peak_x", "peak_y", "labels"):
        assert len(found[key]) == n
    assert found["data_sub"].shape == found["background_rms"].shape == found["segment_map"].shape == frame.shape
    assert found["shape"] == frame.shape


def test_the_sources_are_where_the_photutils_ones_are():
    frame, planted = _stars()

    found = extract_sources_sep(frame, nsigma=2.0, npixels=5, box_size=50, local_noise=True)
    reference = extract_sources(frame, nsigma=2.0, npixels=5, box_size=50, local_noise=True)

    here = np.column_stack([found["x"], found["y"]])
    there = np.column_stack([reference["x"], reference["y"]])
    nearest = np.array([np.hypot(*(here - p).T).min() for p in there])
    assert (nearest < 1.0).mean() > 0.9
    for px, py in planted:
        assert np.hypot(found["x"] - px, found["y"] - py).min() < 0.3  # every planted star, to a third of a pixel


def test_a_segment_is_its_sources_own_pixels():
    frame, _ = _stars()
    found = extract_sources_sep(frame, nsigma=2.0, npixels=5, box_size=50, local_noise=True)

    i = int(np.argmax(found["flux"]))
    own = found["segment_map"] == found["labels"][i]

    assert own.sum() == found["area"][i]
    assert own[int(found["peak_y"][i]), int(found["peak_x"][i])]


def test_a_blank_frame_finds_nothing():
    flat = np.random.default_rng(1).normal(500.0, 5.0, (300, 300))
    flat[:] = 500.0

    assert extract_sources_sep(flat, nsigma=2.0, npixels=5, box_size=50, local_noise=True) is None


def test_a_box_larger_than_the_frame_is_clipped():
    frame = np.random.default_rng(2).normal(500.0, 5.0, (40, 60))

    extract_sources_sep(frame, nsigma=2.0, npixels=5, box_size=50, local_noise=True)  # does not raise


def test_the_selection_chooses_the_same_stars_with_either_detector():
    frame, _ = _stars()
    kwargs = {"optical_center": CENTER, "low_coma_radius": LOW_COMA * 2}

    photutils = select_sources(frame, params=PARAMS, **kwargs)
    with_sep = select_sources(frame, params=SEP, **kwargs)

    assert len(_kept(photutils)) >= PARAMS.n_target
    assert _kept(with_sep) == _kept(photutils)


def test_the_windowed_selection_with_sep_chooses_the_same_stars_as_the_whole_frame_with_sep():
    frame, _ = _stars(n=60)
    params = SelectionParams(n_target=30, min_snr=20.0, box_size=50, detector="sep")
    kwargs = {"optical_center": CENTER, "low_coma_radius": 150.0, "params": params}

    whole = select_sources(frame, **kwargs)
    windowed = select_sources(frame, windowed=True, **kwargs)

    assert windowed.windows_used > 1
    assert _kept(windowed) == _kept(whole)


def test_a_shadow_is_still_excluded_with_sep():
    frame, _ = _stars(n=150)
    shadow = _vertical_shadow(CENTER[0], 450.0)
    params = SelectionParams(n_target=20, min_snr=20.0, box_size=50, detector="sep")

    sel = select_sources(frame, shadow=shadow, optical_center=CENTER, low_coma_radius=300.0, params=params, windowed=True)

    assert not (np.abs(sel.x - CENTER[0]) < 400).any()
    assert sel.kept.sum() >= params.n_target


def test_an_unknown_detector_is_refused():
    frame, _ = _stars(n=20)

    with pytest.raises(ValueError, match="detector"):
        select_sources(frame, params=SelectionParams(detector="sextractor"), optical_center=CENTER)


def test_sep_is_the_default_detector():
    assert SelectionParams().detector == "sep" and SEP.detector == "sep"
