"""End-to-end offline validation of the stage-geometry chain.

Synthetic frames -> ``detect_mirror_shadow`` -> ``find_spec_stage_position`` ->
recover a KNOWN spec position.  This chain had never been executed anywhere --
not on sky (the phase could not run: no folder), not in tests -- so before an
on-sky attempt this is the strongest available evidence that the pieces
actually compose.

The synthetic scene honours the detector's stated contract rather than
bypassing it: a near-vertical band (the search is restricted to
``near_vertical_deg`` of vertical), penumbra half-width above the 60px floor,
depth above ``min_depth``, a starfield that the reference-divide must remove,
and noise for the prominence score to beat.
"""

import numpy as np
import pytest

from calibration.analysis.mirror_shadow import detect_mirror_shadow
from calibration.analysis.stage_geometry import find_spec_stage_position

NY, NX = 1200, 1600
OPTICAL_CENTER = (800.0, 600.0)  # (x, y) px
#: The stage->detector mapping the solver must recover: the band centerline
#: crosses x = 800 (the optical center) at stage position 1500.
X0_AT = {pos: 200.0 + 0.4 * pos for pos in (1000, 1500, 2000, 2500, 3000)}
TRUE_SPEC_POSITION = 1500.0


def _starfield(rng):
    """A flat sky + stars, shared by reference and shadow frames so the
    reference-divide removes them (fresh noise per frame, as in reality)."""
    field = np.full((NY, NX), 1000.0)
    yy, xx = np.mgrid[0:NY, 0:NX]
    for _ in range(40):
        sx, sy = rng.uniform(50, NX - 50), rng.uniform(50, NY - 50)
        amp = rng.uniform(500, 3000)
        field += amp * np.exp(-(((xx - sx) ** 2 + (yy - sy) ** 2) / (2 * 2.5**2)))
    return field


def _frame(field, rng, band_x=None, depth=0.15, half_width=100.0):
    """One noisy exposure of ``field``, optionally shadowed by a vertical band."""
    frame = field.copy()
    if band_x is not None:
        x = np.arange(NX, dtype=float)
        # super-Gaussian: flat-ish umbra with soft penumbra edges
        profile = depth * np.exp(-(((x - band_x) / half_width) ** 4))
        frame = frame * (1.0 - profile)[None, :]
    return frame + rng.normal(0, 10.0, frame.shape)


@pytest.fixture(scope="module")
def scene():
    rng = np.random.default_rng(2026)
    field = _starfield(rng)
    reference = _frame(field, rng)
    shadows = {pos: _frame(field, rng, band_x=X0_AT[pos]) for pos in X0_AT}
    return reference, shadows


def test_detector_finds_the_band_where_it_was_painted(scene):
    reference, shadows = scene
    model = detect_mirror_shadow(shadows[1500], reference=reference)

    assert model.present
    # vertical band -> |s| of the centerline == |x0 - cx|; at pos 1500 the band
    # sits at x=800 with cx=799.5, i.e. essentially through the image center
    assert abs(model.offset) < 25
    assert abs(abs(np.degrees(model.angle)) - 90) < 6, "found near-vertical, as painted"
    assert model.penumbra_half_width >= 60
    assert model.depth == pytest.approx(0.15, abs=0.05)


def test_no_band_reports_absent(scene):
    reference, _ = scene
    rng = np.random.default_rng(7)
    clean = _frame(_starfield(rng), rng)  # different field, no band

    assert not detect_mirror_shadow(clean, reference=None).present


def test_chain_recovers_the_spec_position(scene):
    """The whole point: sweep -> detect each -> solve -> s* within a few px of
    truth.  B is 0.4 px/step, so 25 steps of tolerance is 10 px on the detector."""
    reference, shadows = scene
    positions = sorted(shadows)
    models = [detect_mirror_shadow(shadows[p], reference=reference) for p in positions]
    assert all(m.present for m in models), "every sweep frame must detect"

    result = find_spec_stage_position(models, positions, OPTICAL_CENTER)

    assert result.has_solution, result.message
    assert result.bracketed, "sweep straddles the optical center by construction"
    assert result.spec_position == pytest.approx(TRUE_SPEC_POSITION, abs=25)
    assert abs(result.slope) == pytest.approx(0.4, rel=0.15), "recovers px-per-step scale"
    assert result.residual_rms < 5.0


def test_unbracketed_sweep_is_refused(scene):
    """All frames on one side of the center -> extrapolation refused by default.
    require_bracketed exists so a mis-centred sweep fails loudly instead of
    extrapolating the fiber position off the sampled range."""
    reference, shadows = scene
    # the three positions whose band sits ABOVE the optical center's x --
    # enough frames to satisfy the solver's min_frames, all on one side
    one_side = [p for p in sorted(shadows) if X0_AT[p] > OPTICAL_CENTER[0]]
    assert len(one_side) >= 3, "scene must provide a one-sided subset of >= min_frames"
    models = [detect_mirror_shadow(shadows[p], reference=reference) for p in one_side]

    result = find_spec_stage_position(models, one_side, OPTICAL_CENTER)

    assert not result.has_solution
    assert not result.bracketed
