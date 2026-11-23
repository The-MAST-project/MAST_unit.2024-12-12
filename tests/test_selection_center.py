"""The selection's search grows out from the fibre pixel, not from the middle of the frame it is given."""

from __future__ import annotations

import numpy as np
import pytest
from astropy.io import fits
from test_source_selection import NOISE, SKY, _star

from solvers import selection_stage
from solvers.selection_stage import SourceSelectionSettings, prepare
from solvers.source_selection import SelectionParams

NX, NY = 1200, 900
FIBRE = (150.0, 450.0)
PARAMS = SelectionParams(n_target=10, n_max=40, min_snr=25.0, grid=(1, 1))


@pytest.fixture(autouse=True)
def noshadow(monkeypatch):
    from imaging.mirror_shadow import ShadowModel

    monkeypatch.setattr(
        selection_stage, "detect_mirror_shadow", lambda data, **kw: ShadowModel(present=False, image_shape=data.shape)
    )


def _frame() -> np.ndarray:
    """Two columns of 16 equal stars: one beside the fibre pixel, one across the frame from it."""
    frame = SKY + np.random.default_rng(4).normal(0, NOISE, (NY, NX))
    for x in (150.0, 1050.0):
        for y in np.arange(250.0, 650.0, 25.0):
            _star(frame, x + 0.3, y + 0.2, 2e5)
    return frame


def _run(center):
    return prepare(_frame(), fits.Header(), "spec", SourceSelectionSettings(mode="selective", params=PARAMS), center=center)


def test_the_search_starts_at_the_low_coma_radius_about_the_fibre_pixel():
    stage = _run(FIBRE)
    sel = stage.selection

    assert sel is not None and sel.optical_center == FIBRE
    assert sel.radius_used_px == sel.low_coma_radius_px == 225.0  # 0.25 of the shorter side: it did not have to grow
    kept_x = sel.x[sel.kept]
    assert len(kept_x) >= 10 and (kept_x < 300).all()  # every kept star is beside the fibre pixel
    assert stage.record()["search_center_px"] == [150.0, 450.0]


def test_from_the_frames_middle_the_same_frame_has_to_reach_further():
    stage = _run(None)
    sel = stage.selection

    assert sel is not None and sel.optical_center == ((NX - 1) / 2.0, (NY - 1) / 2.0)
    assert sel.radius_used_px > 225.0  # nothing lies within 225 px of the middle: the radius grew to reach the columns


def test_the_settings_say_where_the_search_grows_from():
    assert SourceSelectionSettings(mode="selective").search_from == "fibre"
    assert SourceSelectionSettings(mode="selective", search_from="frame").search_from == "frame"
    with pytest.raises(ValueError, match="search_from"):
        SourceSelectionSettings(mode="selective", search_from="optical-centre")  # type: ignore[arg-type]
