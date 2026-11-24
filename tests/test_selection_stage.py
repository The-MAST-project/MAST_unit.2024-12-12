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
def noshadow(monkeypatch):
    """`detect_mirror_shadow` finds nothing, whatever the frame (for tests that are not about the shadow)."""
    from imaging.mirror_shadow import ShadowModel

    monkeypatch.setattr(
        selection_stage, "detect_mirror_shadow", lambda data, **kw: ShadowModel(present=False, image_shape=data.shape)
    )


@pytest.fixture
def shadowed(monkeypatch):
    """`detect_mirror_shadow` finds the synthetic band, whatever the frame."""
    monkeypatch.setattr(selection_stage, "detect_mirror_shadow", lambda data, **kw: _shadow())


def test_unconfigured_is_original():
    assert resolve_settings(object(), None).mode == "original"


def test_the_callers_settings_win_over_the_configuration():
    given = SourceSelectionSettings(mode="selective")
    assert resolve_settings(object(), given) is given


def test_config_gives_only_what_it_sets_over_the_defaults():
    class Conf:
        class solving:  # noqa: N801
            source_selection = SourceSelectionConfig(mode="shadowed", min_snr=12.0, grid=(2, 2))

    settings = resolve_settings(Conf, None)
    assert settings.mode == "shadowed"
    assert settings.params.min_snr == 12.0
    assert settings.params.grid == (2, 2)
    assert settings.params.n_target == SelectionParams().n_target


def test_a_mode_that_does_not_exist_is_refused():
    with pytest.raises(ValueError):
        SourceSelectionSettings(mode="partial")  # type: ignore[arg-type]


def test_shadowed_darkens_the_band_and_leaves_the_rest(shadowed):
    frame, _ = _frame()
    frame = frame.astype(np.uint16)
    stage = prepare(frame, fits.Header(), "spec", SourceSelectionSettings(mode="shadowed"))

    assert stage.selection is None and stage.failed is None
    assert stage.data.dtype == np.uint16
    mask = _shadow().mask(include_penumbra=True)
    assert np.any(stage.data[mask] != frame[mask])
    np.testing.assert_array_equal(stage.data[~mask], frame[~mask])


def test_every_phase_looks_for_the_shadow_the_sky_phases_included(shadowed):
    """The `sky` position puts the mirror's edge at the frame's edge: a partial shadow, in the sky ROI too."""
    frame, _ = _frame()
    for phase in ("sky", "spec", "full"):
        stage = prepare(frame, fits.Header(), phase, SourceSelectionSettings(mode="shadowed"))
        assert stage.shadow is not None and stage.shadow.present, phase


def test_a_frame_with_no_shadow_is_not_darkened(monkeypatch):
    from imaging.mirror_shadow import ShadowModel

    monkeypatch.setattr(
        selection_stage, "detect_mirror_shadow", lambda data, **kw: ShadowModel(present=False, image_shape=(NY, NX))
    )
    frame, _ = _frame()
    stage = prepare(frame, fits.Header(), "spec", SourceSelectionSettings(mode="shadowed"))
    np.testing.assert_array_equal(stage.data, frame)


def test_full_selects_on_the_undarkened_frame(shadowed):
    frame, _ = _frame()
    stage = prepare(frame, fits.Header(), "spec", SourceSelectionSettings(mode="selective"))

    assert stage.selection is not None
    np.testing.assert_array_equal(stage.data, frame)  # the image fallback is the frame as it came
    assert stage.selection.shadow_present
    assert stage.record()["mode"] == "selective"
    assert stage.record()["counts"]["kept"] == len(stage.selection.xylist())


def test_too_few_sources_fails_the_frame(shadowed):
    frame, _ = _frame(planted=False, n_clean=3)
    stage = prepare(frame, fits.Header(), "spec", SourceSelectionSettings(mode="selective"))
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


# -- the camera's hot pixels ---------------------------------------------------------------------------------------


def _hot_pixels(shape, pixels):
    from imaging.hot_pixels import HotPixels

    return HotPixels(
        x=np.array([p[0] for p in pixels]),
        y=np.array([p[1] for p in pixels]),
        excess=np.full(len(pixels), 5000.0),
        frames=np.full(len(pixels), 10),
        image_shape=shape,
    )


def test_hot_pixels_are_filled_before_anything_else(noshadow):
    frame, _ = _frame()
    frame = frame.copy()
    frame[100, 200] = 60000.0
    settings = SourceSelectionSettings(mode="shadowed", hot_pixels=_hot_pixels(frame.shape, [(200, 100)]))
    stage = prepare(frame, fits.Header(), "spec", settings)

    assert stage.hot_pixels_filled == 1
    assert stage.data[100, 200] < 5000.0  # the sky around it, not the 60000
    assert stage.record()["hot_pixels_filled"] == 1


def test_a_crop_is_filled_by_the_maps_full_frame_pixels(noshadow):
    frame, _ = _frame()
    frame = frame.copy()
    crop = frame[50:300, 100:400]  # its top-left is (100, 50) in the full frame
    full_shape = frame.shape
    settings = SourceSelectionSettings(mode="shadowed", hot_pixels=_hot_pixels(full_shape, [(250, 200), (10, 10)]))
    stage = prepare(crop, fits.Header(), "spec", settings, origin=(100, 50), frame_shape=full_shape)
    assert stage.hot_pixels_filled == 1  # (250, 200) is in the crop, (10, 10) is not


def test_a_map_for_another_frame_size_is_not_applied(noshadow):
    frame, _ = _frame()
    settings = SourceSelectionSettings(mode="shadowed", hot_pixels=_hot_pixels((10, 10), [(5, 5)]))
    stage = prepare(frame, fits.Header(), "spec", settings)
    assert stage.hot_pixels_filled == 0
    np.testing.assert_array_equal(stage.data, frame)


def test_no_map_no_filling(noshadow):
    frame, _ = _frame()
    stage = prepare(frame, fits.Header(), "spec", SourceSelectionSettings(mode="shadowed"))
    assert stage.hot_pixels_filled == 0
    np.testing.assert_array_equal(stage.data, frame)


def test_the_unit_looks_for_its_map_on_the_share_only_when_the_selection_is_on(tmp_path, monkeypatch):
    from imaging.hot_pixels import calibration_path

    _hot_pixels((900, 1200), [(5, 5)]).save(calibration_path(tmp_path))

    class _Filer:
        class shared:  # noqa: N801
            root = str(tmp_path)

    import common.filer as filer_module

    monkeypatch.setattr(filer_module, "Filer", _Filer)

    class Conf:
        class solving:  # noqa: N801
            source_selection = SourceSelectionConfig(mode="shadowed")

    assert len(resolve_settings(Conf, None).hot_pixels) == 1

    Conf.solving.source_selection = SourceSelectionConfig(mode="original")
    assert resolve_settings(Conf, None).hot_pixels is None


def test_a_missing_or_unreadable_map_is_no_map(tmp_path, monkeypatch):
    from imaging.hot_pixels import calibration_path

    class _Filer:
        class shared:  # noqa: N801
            root = str(tmp_path)

    import common.filer as filer_module

    monkeypatch.setattr(filer_module, "Filer", _Filer)
    assert selection_stage.load_machine_hot_pixels() is None  # nothing there

    path = calibration_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("not json", encoding="utf-8")
    assert selection_stage.load_machine_hot_pixels() is None  # there, unreadable


def test_each_step_is_timed(shadowed):
    frame, _ = _frame()
    shadowed_stage = prepare(frame, fits.Header(), "spec", SourceSelectionSettings(mode="shadowed"))
    assert set(shadowed_stage.timings) == {"hot_pixels", "shadow", "darken"}
    selective = prepare(frame, fits.Header(), "spec", SourceSelectionSettings(mode="selective"))
    assert set(selective.timings) == {"hot_pixels", "shadow", "select"}
    assert all(v >= 0 for v in selective.timings.values())
    assert set(selective.record()["timings_s"]) == set(selective.timings)
    cached = prepare(frame, fits.Header(), "spec", SourceSelectionSettings(mode="selective"), shadow=_shadow())
    assert cached.timings["shadow"] == 0.0  # the acquisition's cached model: no detection


def test_windowed_is_passed_to_the_selection(shadowed, monkeypatch):
    seen = {}
    real = selection_stage.select_sources

    def spy(*args, **kwargs):
        seen["windowed"] = kwargs.get("windowed")
        return real(*args, **kwargs)

    monkeypatch.setattr(selection_stage, "select_sources", spy)
    frame, _ = _frame()

    stage = prepare(frame, fits.Header(), "spec", SourceSelectionSettings(mode="selective", windowed=True))

    assert seen["windowed"] is True and stage.selection is not None
    assert SourceSelectionSettings().windowed is False
