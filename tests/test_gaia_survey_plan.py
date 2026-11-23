"""The Gaia survey's ``plan`` step: choosing isolated stars spread over the sky.

A synthetic sky -- a function that returns stars wherever it is asked -- stands in for the catalog, so the rule is
tested without h5py or the share.
"""

from __future__ import annotations

import csv
import datetime
import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

_SPEC = importlib.util.spec_from_file_location(
    "gaia_accuracy_survey_plan", Path(__file__).resolve().parent.parent / "tools" / "gaia_accuracy_survey.py"
)
survey = importlib.util.module_from_spec(_SPEC)
sys.modules["gaia_accuracy_survey_plan"] = survey
_SPEC.loader.exec_module(survey)  # type: ignore[union-attr]

LAT, LON = 30.05, 35.0
WHEN = datetime.datetime(2026, 11, 24, 20, 0, tzinfo=datetime.UTC)


def _sky(extra=None, g=9.5, pm=2.0, spacing_deg=0.5):
    """A cone function: a regular lattice of stars of magnitude ``g`` (so each cone holds some), plus ``extra``
    ``(ra, dec, g, pmra, pmdec)`` rows, each given to every cone that contains it."""
    ras, decs = np.meshgrid(np.arange(0, 360, spacing_deg), np.arange(-30, 90, spacing_deg))
    base = np.column_stack([ras.ravel(), decs.ravel(), np.full(ras.size, g), np.full(ras.size, pm), np.zeros(ras.size)])
    rows = base if extra is None else np.vstack([base, np.asarray(extra, float)])

    def cone(ra, dec, radius):
        d = survey.offsets_arcsec(rows[:, 0], rows[:, 1], ra, dec)
        near = np.hypot(*d) / 3600.0 <= radius
        r = rows[near]
        return r[:, 0], r[:, 1], r[:, 2], r[:, 3], r[:, 4]

    return cone


def test_altaz_and_radec_are_inverse():
    for alt, az in [(40.0, 10.0), (60.0, 200.0), (75.0, 300.0)]:
        ra, dec = survey.altaz_to_radec(alt, az, LAT, LON, WHEN)
        a, z = survey.radec_to_altaz(ra, dec, LAT, LON, WHEN)
        assert (a, z) == pytest.approx((alt, az), abs=1e-6)


def test_zenith_has_the_declination_of_the_latitude():
    _, dec = survey.altaz_to_radec(90.0, 0.0, LAT, LON, WHEN)
    assert dec == pytest.approx(LAT, abs=1e-6)


def test_a_known_sidereal_time():
    # 2000-01-01 12:00 UTC: GMST is 18h 41m 50.5s = 280.4606 degrees
    when = datetime.datetime(2000, 1, 1, 12, tzinfo=datetime.UTC)
    assert survey._gmst_deg(when) == pytest.approx(280.4606, abs=1e-3)


def test_cells_are_ordered_farthest_first():
    params = survey.PlanParams()
    cells = survey.sky_cells(params)
    assert len(cells) == params.n_alt * params.n_az
    assert len({c[0] for c in cells}) == len(cells)
    # the second cell is nowhere near the first: they are on opposite sides of the sky
    v0, v1 = (survey._alt_az_vector(c[1], c[2]) for c in cells[:2])
    assert float(v0 @ v1) < 0.5


def test_it_picks_n_targets_spread_over_the_sky(tmp_path):
    targets, empty = survey.plan_targets(_sky(), LAT, LON, WHEN, survey.PlanParams(n=12))
    assert len(targets) == 12
    assert empty == []
    assert [t.name for t in targets] == [f"T{k:02d}" for k in range(1, 13)]
    az = [t.az_deg for t in targets]
    assert az == sorted(az)  # one sweep
    assert len({t.cell for t in targets}) == 12
    assert min(t.alt_deg for t in targets) >= 35.0
    assert max(t.alt_deg for t in targets) <= 80.0
    assert max(az) - min(az) > 180.0


def test_the_nearest_to_the_cell_centre_wins():
    params = survey.PlanParams(n=1, n_alt=1, n_az=1)
    cell = survey.sky_cells(params)[0]
    centre = survey.altaz_to_radec(cell[1], cell[2], LAT, LON, WHEN)
    cone = _sky(extra=[(centre[0] + 0.01, centre[1] + 0.01, 9.0, 1.0, 1.0)], spacing_deg=2.0)
    (t,), _ = survey.plan_targets(cone, LAT, LON, WHEN, params)
    assert t.g == 9.0
    assert t.ra_deg == pytest.approx(centre[0] + 0.01, abs=1e-3)


def test_a_faint_neighbour_disqualifies_a_star():
    params = survey.PlanParams(n=1, n_alt=1, n_az=1, search_deg=0.3)
    cell = survey.sky_cells(params)[0]
    ra, dec = survey.altaz_to_radec(cell[1], cell[2], LAT, LON, WHEN)
    star = (ra, dec, 9.0, 1.0, 1.0)
    neighbour = (ra + 20.0 / 3600.0 / np.cos(np.radians(dec)), dec, 17.5, 1.0, 1.0)  # 20 arcsec away, G 17.5
    far_neighbour = (ra + 40.0 / 3600.0 / np.cos(np.radians(dec)), dec, 17.5, 1.0, 1.0)
    sparse = dict(g=99.0, spacing_deg=5.0)  # a lattice too faint to matter
    (t,), _ = survey.plan_targets(_sky([star], **sparse), LAT, LON, WHEN, params)
    assert t.g == 9.0
    targets, empty = survey.plan_targets(_sky([star, neighbour], **sparse), LAT, LON, WHEN, params)
    assert targets == [] and len(empty) == 1
    (t,), _ = survey.plan_targets(_sky([star, far_neighbour], **sparse), LAT, LON, WHEN, params)
    assert t.g == 9.0


def test_a_neighbour_fainter_than_the_catalog_limit_is_ignored():
    params = survey.PlanParams(n=1, n_alt=1, n_az=1, search_deg=0.3, faintest_g=18.0)
    cell = survey.sky_cells(params)[0]
    ra, dec = survey.altaz_to_radec(cell[1], cell[2], LAT, LON, WHEN)
    star = (ra, dec, 9.0, 1.0, 1.0)
    faint = (ra + 10.0 / 3600.0 / np.cos(np.radians(dec)), dec, 19.0, 1.0, 1.0)
    (t,), _ = survey.plan_targets(_sky([star, faint], g=99.0, spacing_deg=5.0), LAT, LON, WHEN, params)
    assert t.g == 9.0


@pytest.mark.parametrize(
    ("g", "pm"),
    [(7.5, 1.0), (11.5, 1.0), (9.5, 50.0), (9.5, np.nan)],
    ids=["too bright", "too faint", "fast proper motion", "no proper motion"],
)
def test_a_star_outside_the_rules_is_not_chosen(g, pm):
    targets, empty = survey.plan_targets(_sky(g=g, pm=pm), LAT, LON, WHEN, survey.PlanParams(n=4))
    assert targets == []
    assert len(empty) == survey.PlanParams().n_alt * survey.PlanParams().n_az


def test_an_empty_cell_falls_back_to_the_next():
    # only the cells that are not the first have stars: the plan still reaches n by using the fallbacks
    params = survey.PlanParams(n=5)
    first = survey.sky_cells(params)[0]
    centre = survey.altaz_to_radec(first[1], first[2], LAT, LON, WHEN)
    inner = _sky()

    def cone(ra, dec, radius):
        if abs(ra - centre[0]) < 1e-6 and abs(dec - centre[1]) < 1e-6:
            return tuple(np.empty(0) for _ in range(5))
        return inner(ra, dec, radius)

    targets, empty = survey.plan_targets(cone, LAT, LON, WHEN, params)
    assert len(targets) == 5
    assert empty == [first[0]]


def test_proper_motion_is_applied_to_the_observation_epoch():
    params = survey.PlanParams(n=1, n_alt=1, n_az=1, search_deg=0.5)
    cell = survey.sky_cells(params)[0]
    ra, dec = survey.altaz_to_radec(cell[1], cell[2], LAT, LON, WHEN)
    star = (ra, dec, 9.0, 0.0, 10.0)  # 10 mas/yr north
    (t,), _ = survey.plan_targets(_sky([star], g=99.0, spacing_deg=5.0), LAT, LON, WHEN, params)
    years = survey._years_since(2016.0, WHEN)
    assert (t.dec_deg - dec) * 3.6e6 == pytest.approx(10.0 * years, rel=1e-6)
    assert t.pm_mas_yr == pytest.approx(10.0)


def test_the_command_line_writes_targets_csv(tmp_path, capsys):
    out = tmp_path / "plan"
    rc = survey.main(
        ["plan", "--out", str(out), "--n", "6", "--when", "2026-11-24T20:00:00Z", "--lat", str(LAT), "--lon", str(LON)],
        cone=_sky(),
    )
    assert rc == 0
    with open(out / "targets.csv", newline="") as f:
        rows = list(csv.DictReader(f))
    assert [r["name"] for r in rows] == [f"T{k:02d}" for k in range(1, 7)]
    assert {"ra_deg", "dec_deg", "g", "pm_mas_yr", "alt_deg", "az_deg", "cell", "density_per_deg2"} <= set(rows[0])
    text = capsys.readouterr().out
    assert "6 targets" in text and "T01" in text
