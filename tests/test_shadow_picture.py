"""The picture of a detected shadow, and that a stage with an artifacts folder leaves one."""

from __future__ import annotations

import numpy as np
from astropy.io import fits
from PIL import Image
from test_source_selection import _frame, _shadow

from solvers.mastrometry import MastrometryDotNet
from solvers.selection_stage import SourceSelectionSettings, prepare
from solvers.shadow_picture import caption_lines, write_shadow_picture


def test_a_picture_of_the_shadow(tmp_path):
    frame, _ = _frame()
    out = tmp_path / "s-shadow.jpg"
    write_shadow_picture(frame, _shadow(), out, caption_lines(_shadow(), "title", 1.5))
    with Image.open(out) as picture:
        assert picture.format == "JPEG" and picture.height > frame.shape[0] // 8


def test_a_shadowed_picture_has_the_original_the_detected_and_the_darkened_frames(tmp_path):
    frame, _ = _frame()
    single, pair = tmp_path / "a.jpg", tmp_path / "b.jpg"
    write_shadow_picture(frame, _shadow(), single, ["x"])
    write_shadow_picture(frame, _shadow(), pair, ["x"], darkened=frame)
    with Image.open(single) as one, Image.open(pair) as two:
        assert two.width > one.width * 0.9  # three panels on one line


def test_no_shadow_is_said(tmp_path):
    assert caption_lines(None, "t")[1] == "no shadow found"


def test_the_solver_writes_the_picture_beside_its_artifacts(tmp_path, monkeypatch):
    frame, _ = _frame()
    monkeypatch.setattr("solvers.selection_stage.detect_mirror_shadow", lambda *a, **k: _shadow())
    stage = prepare(frame, fits.Header(), "spec", SourceSelectionSettings(mode="shadowed"))
    MastrometryDotNet._write_shadow_picture(tmp_path / "new", "frame,solver=x", frame, stage)
    assert (tmp_path / "new" / "frame,solver=x-shadow.jpg").exists()
    assert np.isfinite(stage.timings["shadow"])


def test_a_picture_that_cannot_be_made_does_not_raise(tmp_path):
    MastrometryDotNet._write_shadow_picture(tmp_path, "s", None, None)  # type: ignore[arg-type]
