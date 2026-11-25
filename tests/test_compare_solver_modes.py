"""The original/selective comparison tool: which frames it solves, and what it reports. The solver is stubbed."""

from __future__ import annotations

import csv
import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits

from imaging.mirror_shadow import ShadowModel
from imaging.shadow_store import ShadowStore, StoredShadow
from solvers.survey_solve import SolveOutcome

_TOOLS = Path(__file__).resolve().parent.parent / "tools"
sys.path.insert(0, str(_TOOLS))
_SPEC = importlib.util.spec_from_file_location("compare_solver_modes", _TOOLS / "compare_solver_modes.py")
cmp = importlib.util.module_from_spec(_SPEC)
sys.modules["compare_solver_modes"] = cmp
_SPEC.loader.exec_module(cmp)  # type: ignore[union-attr]

SHAPE = (64, 96)


def _frame(folder: Path, name: str, stagepos: int | None) -> Path:
    header = fits.Header()
    if stagepos is not None:
        header["STAGEPOS"] = stagepos
    path = folder / name
    folder.mkdir(parents=True, exist_ok=True)
    fits.writeto(path, np.zeros(SHAPE, np.uint16), header, overwrite=True)
    return path


def _store(path: Path, position: int) -> ShadowStore:
    model = ShadowModel(present=True, image_shape=SHAPE, depth=0.5, umbra_half_width=5.0, penumbra_half_width=9.0)
    store = ShadowStore(path.parent)
    store.path = path
    entry = StoredShadow(
        model=model, stage_position=position, detected_at="2026-11-01T00:00:00+00:00", seconds=7.0, image_path=""
    )
    assert store.save(entry, lambda stored: abs(stored - position) <= 100)
    return store


def test_offset_is_east_and_north_in_arcsec():
    east, north = cmp.offset_arcsec((10.0, 0.0), (10.0 + 1 / 3600, 1 / 3600))
    assert east == pytest.approx(1.0, abs=1e-6)
    assert north == pytest.approx(1.0, abs=1e-6)
    east, _ = cmp.offset_arcsec((359.9999, 0.0), (0.0001, 0.0))  # across RA 0
    assert east == pytest.approx(0.2 * 3600 / 1000 * 1, rel=1e-3)


def test_frames_without_a_model_or_a_stage_position_are_skipped_with_the_reason(tmp_path):
    store = _store(tmp_path / "shadow_models.json", 99998)
    near = _frame(tmp_path / "f", "near.fits", 99950)
    far = _frame(tmp_path / "f", "far.fits", 50000)
    none = _frame(tmp_path / "f", "none.fits", None)

    assert cmp.model_for(store, near, 100)[0] is not None
    assert "no stored model" in cmp.model_for(store, far, 100)[1]
    assert "STAGEPOS" in cmp.model_for(store, none, 100)[1]


def _outcome(ra: float, dec: float, seconds: float, used: int, rms: float) -> SolveOutcome:
    o = SolveOutcome(status="ok", seconds=seconds, crval=(ra, dec), residual_rms_arcsec=rms, residual_clipped_rms_arcsec=rms)
    o.used_x = np.zeros(used)
    o.timings = {"select": 0.5} if seconds < 5 else {}
    return o


def test_the_comparison_solves_both_modes_and_reports_the_differences(tmp_path, monkeypatch):
    store = _store(tmp_path / "shadow_models.json", 99998)
    _frame(tmp_path / "f", "a.fits", 99998)
    _frame(tmp_path / "f", "b.fits", 12345)
    seen: list[tuple[str, object]] = []

    def fake(path, args, params, out_dir, shadow=None):
        mode = args.solver_modes[0]
        seen.append((mode, shadow))
        if mode == "original":
            return {mode: _outcome(10.0, 20.0, 10.0, 30, 0.30)}
        return {mode: _outcome(10.0 + 1 / 3600 / np.cos(np.radians(20.0)), 20.0 - 2 / 3600, 4.0, 25, 0.25)}

    monkeypatch.setattr(cmp.survey, "_solve_via_solver", fake)
    out = tmp_path / "out"

    rc = cmp.main(
        [str(tmp_path / "f"), "--out", str(out), "--fibre-xy", "40", "30", "--shadow-store", str(store.path), "--no-warmup"]
    )

    assert rc == 0
    assert [m for m, _ in seen] == ["original", "selective"]
    assert seen[0][1] is None and seen[1][1] is not None  # only the selective mode is given the stored model
    rows = {r["frame"]: r for r in csv.DictReader((out / "compare.csv").open(encoding="utf-8"))}
    assert rows["b.fits"]["skipped"]
    a = rows["a.fits"]
    assert float(a["d_east_arcsec"]) == pytest.approx(1.0, abs=1e-3)
    assert float(a["d_north_arcsec"]) == pytest.approx(-2.0, abs=1e-3)
    assert float(a["speedup"]) == pytest.approx(2.5)
    summary = (out / "summary.txt").read_text(encoding="utf-8")
    assert "1 with a stored shadow model, 1 skipped" in summary and "speed-up" in summary
