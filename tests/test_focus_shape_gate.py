"""The V-curve shape gate, against every sweep the focus phase has recorded on sky.

On mast00 2026-10-01 three runs started 2000-5000 ticks from focus "converged"
on a vertex as far from focus as they started.  Their sweeps were over faint
donuts: the HFD of the small sources that still extract there is set by the
measurement aperture, not by a star, so it is flat, and noise alone put the
smallest sample in the interior -- which is all the bracketing gate asks.  One of
the three was persisted as the unit's focus.

Nothing about the vertex gave them away: their Dmin (13-20 px) was SMALLER than
real focus (18-27 px), so `max_best_hfd_px` could never catch them.  What does is
the shape: a real V-curve is a parabola and rises away from its vertex; these
were flat or zig-zag.

The rows below are the samples exactly as each run's status.json recorded them
(Z:/MAST/<unit>/<night>/Calibration/Focuser/<run>/), so the test exercises the
gate on the real data without the 94 MB frames behind it.  `genuine` is from
the outcome: the mast02 runs agree to 46 ticks, the mast00 ones to 36 ticks of
the reference, and the three false ones landed 2175-5008 ticks away.
"""

from __future__ import annotations

import pytest

import calibration.analysis.vcurve as vcurve
from calibration.analysis.vcurve import _fit_vcurve, analyze_focus_samples, vcurve_shape

# (run, genuine, recorded best focus, [(position, hfd_px), ...])
RECORDED = [
    (
        "mast00 2026-10-01 0001",
        True,
        13301,
        [(12772, 46.9), (12922, 34.51), (13072, 26.24), (13222, 22.9), (13372, 21.62), (13522, 25.85), (13672, 35.9)],
    ),
    (
        "mast00 2026-10-01 0002",
        True,
        13281,
        [(12937, 38.15), (13087, 25.37), (13237, 19.77), (13387, 21.73), (13537, 30.01), (13687, 40.42), (13837, 58.49)],
    ),
    (
        "mast00 2026-10-01 0003",
        True,
        13265,
        [(12838, 27.29), (12988, 23.21), (13138, 21.84), (13288, 20.88), (13438, 20.93), (13588, 23.79), (13738, 30.91)],
    ),
    (
        "mast00 2026-10-01 0004",
        True,
        13270,
        [(13097, 29.4), (13247, 24.7), (13397, 27.24), (13547, 36.55), (13697, 42.01), (13847, 56.59), (13997, 67.53)],
    ),
    (
        "mast00 2026-10-01 0005",
        False,
        15465,
        [(14840, 25.27), (14990, 26.0), (15140, 25.81), (15290, 26.08), (15440, 17.4), (15590, 25.82), (15740, 22.4)],
    ),
    (
        "mast00 2026-10-01 0006",
        False,
        10806,
        [(10615, 18.07), (10765, 11.43), (10915, 18.9), (11065, 29.22), (11365, 18.11), (11515, 44.56)],
    ),
    (
        "mast00 2026-10-01 0008",
        False,
        8282,
        [(7840, 19.68), (7990, 20.81), (8140, 20.15), (8290, 20.22), (8440, 17.49), (8590, 21.59), (8740, 20.68)],
    ),
    (
        "mast02 2026-07-21 0001",
        True,
        12013,
        [(11527, 50.58), (11677, 38.4), (11827, 24.32), (11977, 30.31), (12127, 21.08), (12277, 33.18), (12427, 44.08)],
    ),
    (
        "mast02 2026-07-21 0003",
        True,
        12026,
        [(11563, 46.39), (11713, 31.27), (11863, 22.93), (12013, 20.29), (12163, 22.5), (12313, 30.6), (12463, 40.66)],
    ),
    (
        "mast02 2026-07-21 0004",
        True,
        12004,
        [(11576, 48.32), (11726, 34.47), (11876, 21.44), (12026, 20.05), (12176, 24.86), (12326, 37.56), (12476, 49.63)],
    ),
    (
        "mast02 2026-07-21 0005",
        True,
        12019,
        [(11554, 50.78), (11704, 35.09), (11854, 24.87), (12004, 20.15), (12154, 23.56), (12304, 33.53), (12454, 45.72)],
    ),
    (
        "mast02 2026-07-21 0006",
        True,
        12031,
        [(11569, 39.53), (11719, 29.45), (11869, 26.95), (12019, 26.74), (12169, 23.35), (12319, 31.19), (12469, 38.35)],
    ),
    (
        "mast02 2026-07-22 0002",
        True,
        12036,
        [(11881, 30.63), (12031, 28.31), (12181, 30.57), (12331, 38.48), (12481, 50.81), (12631, 63.66), (12781, 78.21)],
    ),
    (
        "mast02 2026-07-22 0003",
        True,
        12039,
        [(11586, 38.81), (11736, 32.79), (11886, 23.55), (12036, 22.61), (12186, 25.09), (12336, 29.52), (12486, 39.87)],
    ),
    (
        "mast02 2026-07-22 0004",
        True,
        12050,
        [(11589, 47.05), (11739, 29.42), (11889, 21.92), (12039, 19.58), (12189, 20.32), (12339, 30.1), (12489, 41.31)],
    ),
]
IDS = [r[0] for r in RECORDED]


def _analyze(samples, monkeypatch, **kw):
    """Run the real analysis with the recorded per-frame HFDs standing in for frames."""
    monkeypatch.setattr(vcurve, "measure_sweep_hfd", lambda images, **_: ([(h, 10) for h in images], 10))
    return analyze_focus_samples([(float(p), h) for p, h in samples], **kw).analysis_result


@pytest.mark.parametrize(("run", "genuine", "best", "samples"), RECORDED, ids=IDS)
def test_gate_accepts_exactly_the_genuine_runs(run, genuine, best, samples, monkeypatch):
    result = _analyze(samples, monkeypatch)
    assert result.has_solution is genuine, f"{run}: R^2={result.fit_r2:.2f}, errors={result.errors}"
    assert result.fit_r2 is not None  # recorded whether accepted or not


@pytest.mark.parametrize(("run", "genuine", "best", "samples"), RECORDED, ids=IDS)
def test_without_the_gate_every_run_solves_as_recorded(run, genuine, best, samples, monkeypatch):
    """The replay reproduces the night: ungated, all fifteen solve where they did."""
    result = _analyze(samples, monkeypatch, min_fit_r2=None, min_edge_rise=None)
    assert result.has_solution
    assert result.best_focus_position == pytest.approx(best, abs=1)


def test_the_thresholds_have_margin_on_both_sides():
    """Not a knife edge: the worst genuine run and the best false one are well apart."""
    scores = {}
    for _run, genuine, _, samples in RECORDED:
        x, d = zip(*samples, strict=True)
        r2, rise = vcurve_shape(x, d, _fit_vcurve(x, d, 0.025))
        scores.setdefault(genuine, []).append((r2, rise))
    assert min(r2 for r2, _ in scores[True]) >= 0.90
    assert max(r2 for r2, _ in scores[False]) <= 0.70
    assert min(rise for _, rise in scores[True]) >= 1.45


def test_a_flat_curve_with_a_perfect_fit_is_still_refused(monkeypatch):
    """R^2 alone can be fooled by a shallow but smooth bowl; the edge rise is not."""
    samples = [(p, 20.0 + 1e-6 * (p - 1000) ** 2) for p in range(700, 1301, 100)]
    result = _analyze(samples, monkeypatch)
    assert not result.has_solution
    assert any("rise only" in e for e in result.errors)
