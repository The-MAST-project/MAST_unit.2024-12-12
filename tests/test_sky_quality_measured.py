"""The sky-quality frame after a solved focus_on_star: `focus_on_star.sky_quality`, and the run's step 7.

The frame is not plate-solved; the match finds the field's rotation and parity from the
focus star's known pixel and Gaia position. These tests build fields at a known rotation and
check it is found, then run the whole step on a synthetic frame.
"""

import numpy as np
import pytest

import focus_on_star.session as session_mod
from focus_on_star import sky_quality
from focus_on_star.star_selection import FocusStar
from imaging.hfd import FieldStar, StarMeasure

SCALE = 0.2616  # arcsec / px


def _rotate(xi, eta, degrees, mirrored):
    """Catalog offsets (arcsec) to pixel offsets -- the transform match_field inverts."""
    t = np.radians(degrees)
    u, v = (-xi if mirrored else xi) / SCALE, eta / SCALE
    return u * np.cos(t) - v * np.sin(t), u * np.sin(t) + v * np.cos(t)


def _field(n=12, seed=1):
    rng = np.random.default_rng(seed)
    xi = np.concatenate([[0.0], rng.uniform(-90, 90, n)])  # the focus star first, at the origin
    eta = np.concatenate([[0.0], rng.uniform(-60, 60, n)])
    return xi, eta


class TestTangentOffsets:
    def test_north_is_eta(self):
        xi, eta = sky_quality.tangent_offsets_arcsec(100.0, 30.0, 100.0, 30.0 + 10 / 3600)
        assert xi == pytest.approx(0, abs=1e-6) and eta == pytest.approx(10, rel=1e-4)

    def test_east_shrinks_with_declination(self):
        xi, _ = sky_quality.tangent_offsets_arcsec(100.0, 60.0, 100.0 + 20 / 3600, 60.0)
        assert xi == pytest.approx(10, rel=1e-3)  # cos 60 = 0.5


class TestAirmass:
    def test_overhead_is_one(self):
        assert sky_quality.airmass(90.0) == pytest.approx(1.0, abs=1e-3)

    def test_at_thirty_degrees_about_two(self):
        assert sky_quality.airmass(30.0) == pytest.approx(1.995, abs=0.01)

    @pytest.mark.parametrize("altitude", [None, 0.0, -5.0])
    def test_none_below_the_horizon_or_unknown(self, altitude):
        assert sky_quality.airmass(altitude) is None


class TestMatchField:
    @pytest.mark.parametrize(("degrees", "mirrored"), [(0.0, False), (37.0, True), (212.5, False), (91.25, True)])
    def test_the_rotation_and_parity_are_found(self, degrees, mirrored):
        xi, eta = _field()
        dx, dy = _rotate(xi, eta, degrees, mirrored)
        rng = np.random.default_rng(2)
        dx, dy = dx + rng.normal(0, 0.5, len(dx)), dy + rng.normal(0, 0.5, len(dy))

        match = sky_quality.match_field(dx, dy, xi, eta, SCALE)

        assert match is not None and match.mirrored is mirrored
        assert (match.rotation_degs - degrees + 180) % 360 - 180 == pytest.approx(0, abs=0.3)
        assert len(match.pairs) == len(xi)
        assert all(c == d for c, d in match.pairs)

    def test_missing_and_spurious_detections_do_not_spoil_it(self):
        xi, eta = _field(n=15)
        dx, dy = _rotate(xi, eta, 120.0, False)
        dx, dy = np.concatenate([dx[:10], [300.0, -250.0, 40.0]]), np.concatenate([dy[:10], [200.0, 10.0, -330.0]])

        match = sky_quality.match_field(dx, dy, xi, eta, SCALE)

        assert match is not None and match.rotation_degs == pytest.approx(120.0, abs=0.3)
        assert len(match.pairs) == 10

    def test_too_few_stars_is_no_match(self):
        xi, eta = _field(n=2)
        dx, dy = _rotate(xi, eta, 10.0, False)
        assert sky_quality.match_field(dx, dy, xi, eta, SCALE) is None


class TestZeroPoint:
    def test_it_recovers_the_zero_point(self):
        mags = np.array([11.0, 12.0, 13.0, 14.5])
        fluxes = 5.0 * 10 ** ((24.0 - mags) / 2.5)
        zp, spread, n = sky_quality.zero_point(fluxes, mags, 5.0)
        assert zp == pytest.approx(24.0) and spread == pytest.approx(0.0, abs=1e-9) and n == 4

    def test_no_positive_flux_is_none(self):
        assert sky_quality.zero_point([0.0, -3.0], [12.0, 13.0], 5.0) == (None, None, 0)


def test_measure_leaves_saturated_stars_out_of_the_zero_point():
    xi, eta = _field(n=8)
    mags = np.concatenate([[11.2], np.linspace(10.0, 15.0, 8)])
    dx, dy = _rotate(xi, eta, 45.0, True)
    star = StarMeasure(x=500.0, y=400.0, hfd=8.6, fwhm=7.6, peak=30000, flux=1e6, saturated=False)
    stars = [
        FieldStar(x=500 + x, y=400 + y, flux=5.0 * 10 ** ((24.0 - m) / 2.5), peak=1.0, saturated=(m < 10.5))
        for x, y, m in zip(dx, dy, mags, strict=True)
    ]
    ra = 100.0 + xi / 3600 / np.cos(np.radians(30.0))
    dec = 30.0 + eta / 3600

    found = sky_quality.measure(star, 100.0, 30.0, stars, ra, dec, mags, scale_arcsec=SCALE, exposure_seconds=5.0)

    assert found.matched_stars == 9 and found.mirrored is True
    assert found.zero_point_mag == pytest.approx(24.0, abs=0.01)
    assert found.zero_point_stars == 9 - sum(m < 10.5 for m in mags)
    assert (found.fwhm_px, found.hfd_px) == (7.6, 8.6)


# ------------------------------------------------------------------ the run's step 7


SHAPE = (800, 1000)  # (ny, nx): a small "sensor", so the frame is cheap
FIBRE = (500.0, 400.0)


def _sky_frame(stars, seed=0):
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0 : SHAPE[0], 0 : SHAPE[1]]
    img = 1200.0 + rng.normal(0, 8, SHAPE)
    for x, y, sigma, peak in stars:
        img += peak * np.exp(-((xx - x) ** 2 + (yy - y) ** 2) / (2 * sigma * sigma))
    return img


@pytest.fixture
def step7(monkeypatch):
    """A run object with just what `_sky_quality` touches, and a field at a known rotation."""
    from test_focus_on_star_run import FakeUnit

    unit = FakeUnit()
    unit.imager.camera_x_size, unit.imager.camera_y_size = SHAPE[1], SHAPE[0]
    unit.pw.status.return_value.mount.altitude_degs = 60.0
    state = session_mod.FocusOnStarStatus().begin(None)
    run = session_mod.FocusOnStarRun(unit, state)
    monkeypatch.setattr(session_mod.FocusOnStarRun, "_still_calibrating", lambda self: True)

    # Focus star at the fibre pixel, five neighbours at 30 deg, not mirrored.
    offsets = [(0.0, 0.0), (8.0, 6.0), (-10.0, 4.0), (5.0, -9.0), (-7.0, -8.0), (11.0, -2.0)]
    mags = [11.3, 12.0, 12.6, 13.1, 12.4, 13.5]
    xi = np.array([o[0] for o in offsets])
    eta = np.array([o[1] for o in offsets])
    dx, dy = _rotate(xi, eta, 30.0, False)
    peaks = [20000 * 10 ** ((11.3 - m) / 2.5) for m in mags]
    frame = _sky_frame([(FIBRE[0] + x, FIBRE[1] + y, 3.0, p) for x, y, p in zip(dx, dy, peaks, strict=True)])
    monkeypatch.setattr(session_mod.FocusOnStarRun, "_expose", lambda self, st, folder, tag: frame)

    ra = 100.0 + xi / 3600 / np.cos(np.radians(30.0))
    dec = 30.0 + eta / 3600
    monkeypatch.setattr(session_mod, "CatsHtm", lambda *a: None)
    monkeypatch.setattr(session_mod, "field_catalog", lambda *a, **k: (ra, dec, np.array(mags)))
    star = FocusStar(
        ra_deg=100.0, dec_deg=30.0, mag=11.3, separation_deg=0, search_radius_deg=0.5, n_candidates=1, n_rejected=0
    )
    st = unit.unit_conf.calibration.settings.focuser.model_copy(update={"exposure": 5.0, "binning": 1})
    box = (300, 700, 250, 550)
    return run, st, star, box


def test_step_7_measures_the_frame(step7):
    run, st, star, box = step7
    ls = session_mod.FocusOnStarSettings()

    run._sky_quality(st, star, FIBRE, box, ls, folder=None)

    record = run.sky_quality
    assert record.succeeded is True, record.errors
    assert record.fwhm_arcsec == pytest.approx(3.0 * 2.3548 * SCALE, rel=0.12)
    assert record.hfd_over_fwhm == pytest.approx(1.0, abs=0.15)  # a Gaussian's HFD is its FWHM
    assert record.matched_stars == 6 and record.mirrored is False
    assert record.rotation_degs == pytest.approx(30.0, abs=0.5)
    assert record.zero_point_mag is not None and record.zero_point_spread_mag < 0.05
    assert record.airmass == pytest.approx(1.154, abs=0.01)
    assert record.background_adu == pytest.approx(1200, abs=20)


def test_step_7_never_raises(step7, monkeypatch):
    run, st, star, box = step7

    def boom(*a, **k):
        raise RuntimeError("the imager fell over")

    monkeypatch.setattr(session_mod.FocusOnStarRun, "_expose", boom)
    run._sky_quality(st, star, FIBRE, box, session_mod.FocusOnStarSettings(), folder=None)

    assert run.sky_quality.succeeded is False
    assert any("the imager fell over" in e for e in run.sky_quality.errors)
