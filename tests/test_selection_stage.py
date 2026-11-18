"""The source-selection stage of the unit's solver (`solvers/selection_stage.py`): modes, shadow, config.

No solver, no unit: the shadow detector is stood in for by the synthetic `ShadowModel` of the
selection tests, and the config object by MAST_common's pydantic model.
"""

from __future__ import annotations

import numpy as np
import pytest
from astropy.io import fits
from test_source_selection import NX, NY, _frame, _shadow

from common.config.unit import SourceSelectionConfig
from solvers import selection_stage
from solvers.selection_stage import SourceSelectionSettings, prepare, resolve_settings, solved_fits_from_wcs
from solvers.source_selection import SelectionParams


@pytest.fixture
def shadowed(monkeypatch):
    """`detect_mirror_shadow` finds the synthetic band, whatever the frame."""
    monkeypatch.setattr(selection_stage, "detect_mirror_shadow", lambda data, **kw: _shadow())


def test_unconfigured_is_off():
    assert resolve_settings(object(), None).mode == "off"


def test_the_callers_settings_win_over_the_configuration():
    given = SourceSelectionSettings(mode="full")
    assert resolve_settings(object(), given) is given


def test_config_gives_only_what_it_sets_over_the_defaults():
    class Conf:
        class solving:  # noqa: N801
            source_selection = SourceSelectionConfig(mode="shadow_only", min_snr=12.0, grid=(2, 2))

    settings = resolve_settings(Conf, None)
    assert settings.mode == "shadow_only"
    assert settings.params.min_snr == 12.0
    assert settings.params.grid == (2, 2)
    assert settings.params.n_target == SelectionParams().n_target


def test_a_mode_that_does_not_exist_is_refused():
    with pytest.raises(ValueError):
        SourceSelectionSettings(mode="partial")  # type: ignore[arg-type]


def test_shadow_only_darkens_the_band_and_leaves_the_rest(shadowed):
    frame, _ = _frame()
    frame = frame.astype(np.uint16)
    stage = prepare(frame, fits.Header(), "spec", SourceSelectionSettings(mode="shadow_only"))

    assert stage.selection is None and stage.failed is None
    assert stage.data.dtype == np.uint16
    mask = _shadow().mask(include_penumbra=True)
    assert np.any(stage.data[mask] != frame[mask])
    np.testing.assert_array_equal(stage.data[~mask], frame[~mask])


def test_the_sky_phase_looks_for_no_shadow(monkeypatch):
    def boom(data, **kw):
        raise AssertionError("the mirror is out for a sky frame")

    monkeypatch.setattr(selection_stage, "detect_mirror_shadow", boom)
    frame, _ = _frame()
    stage = prepare(frame, fits.Header(), "sky", SourceSelectionSettings(mode="shadow_only"))
    assert stage.shadow is None
    np.testing.assert_array_equal(stage.data, frame)


def test_a_frame_with_no_shadow_is_not_darkened(monkeypatch):
    from imaging.mirror_shadow import ShadowModel

    monkeypatch.setattr(
        selection_stage, "detect_mirror_shadow", lambda data, **kw: ShadowModel(present=False, image_shape=(NY, NX))
    )
    frame, _ = _frame()
    stage = prepare(frame, fits.Header(), "spec", SourceSelectionSettings(mode="shadow_only"))
    np.testing.assert_array_equal(stage.data, frame)


def test_full_selects_on_the_undarkened_frame(shadowed):
    frame, _ = _frame()
    stage = prepare(frame, fits.Header(), "spec", SourceSelectionSettings(mode="full"))

    assert stage.selection is not None
    np.testing.assert_array_equal(stage.data, frame)  # the image fallback is the frame as it came
    assert stage.selection.shadow_present
    assert stage.record()["mode"] == "full"
    assert stage.record()["counts"]["kept"] == len(stage.selection.xylist())


def test_too_few_sources_fails_the_frame(shadowed):
    frame, _ = _frame(planted=False, n_clean=3)
    stage = prepare(frame, fits.Header(), "spec", SourceSelectionSettings(mode="full"))
    assert stage.failed in ("too_few_sources", "no_sources")


def test_the_solved_fits_is_the_image_with_the_wcs(tmp_path):
    image, wcs = tmp_path / "image.fits", tmp_path / "x.wcs"
    fits.writeto(image, np.zeros((4, 6), dtype=np.uint16), fits.Header({"EXPTIME": 5.0}))
    fits.PrimaryHDU(header=fits.Header({"CRVAL1": 10.5, "CRVAL2": -3.25, "CRPIX1": 3.0, "IMAGEW": 6.0})).writeto(wcs)

    out = tmp_path / "solved.fits"
    solved_fits_from_wcs(image, wcs, out)

    with fits.open(out) as hdul:
        assert hdul[0].data.shape == (4, 6)
        assert hdul[0].header["EXPTIME"] == 5.0
        assert (hdul[0].header["CRVAL1"], hdul[0].header["CRVAL2"], hdul[0].header["CRPIX1"]) == (10.5, -3.25, 3.0)
