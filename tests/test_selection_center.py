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
PARAMS = SelectionParams(seeing_scale=1.0, n_target=8, n_max=40, min_snr=25.0, grid=(1, 1))


@pytest.fixture(autouse=True)
def noshadow(monkeypatch):
    from imaging.mirror_shadow import ShadowModel

    monkeypatch.setattr(
        selection_stage, "detect_mirror_shadow", lambda data, **kw: ShadowModel(present=False, image_shape=data.shape)
    )


def _frame() -> np.ndarray:
    """Two columns of 10 equal stars: one beside the fibre pixel, one across the frame from it. (Spaced 40 px: on the 2x2
    grid, 20 px of a 3.75 px star, so each is detected alone.)"""
    frame = SKY + np.random.default_rng(4).normal(0, NOISE, (NY, NX))
    for x in (150.0, 1050.0):
        for y in np.arange(250.0, 650.0, 40.0):
            _star(frame, x + 0.3, y + 0.2, 2e5)
    return frame


def _run(center):
    return prepare(_frame(), fits.Header(), "spec", SourceSelectionSettings(mode="selective", params=PARAMS), center=center)


def test_the_search_starts_at_the_low_coma_radius_about_the_fibre_pixel():
    stage = _run(FIBRE)
    sel = stage.selection

    # the selection runs on the 2x2 grid: its pixels are half the frame's, and its pixel g is the frame's 2g and 2g + 1
    assert sel is not None and stage.grid is not None and stage.grid.factor == 2
    assert sel.optical_center == pytest.approx(((FIBRE[0] - 0.5) / 2, (FIBRE[1] - 0.5) / 2))
    assert sel.radius_used_px == sel.low_coma_radius_px == 112.5  # 0.25 of the shorter side, 225 px, in grid pixels
    kept_x = sel.x[sel.kept]
    assert len(kept_x) >= 8 and (kept_x < 150).all()  # every kept star is beside the fibre pixel
    assert stage.record()["search_center_px"] == pytest.approx([74.75, 224.75]) and stage.record()["grid_factor"] == 2


def test_from_the_frames_middle_the_candidate_region_does_not_reach_the_stars():
    stage = _run(None)
    sel = stage.selection

    assert sel is not None and sel.optical_center == pytest.approx(((NX / 2 - 0.5) / 2 - 0.25, (NY / 2 - 0.5) / 2 - 0.25))
    # nothing lies within 225 px of the middle, and the candidate region stops at 1.7 of that: the stars in the columns are
    # not looked for (the radius used to grow to reach them), so the frame has too few
    assert sel.reach_px == pytest.approx(1.7 * 112.5)
    assert sel.status == "too_few_sources" and stage.failed is not None


def test_the_settings_say_where_the_search_grows_from():
    assert SourceSelectionSettings(mode="selective").search_from == "fibre"
    assert SourceSelectionSettings(mode="selective", search_from="frame").search_from == "frame"
    with pytest.raises(ValueError, match="search_from"):
        SourceSelectionSettings(mode="selective", search_from="optical-centre")  # type: ignore[arg-type]
