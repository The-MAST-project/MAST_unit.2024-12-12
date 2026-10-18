"""focus_on_star's star choice: the magnitude rule, isolation, and the growing search."""

import datetime

import numpy as np
import pytest

from focus_on_star.star_selection import (
    find_focus_star,
    isolation_radius_deg,
    propagate_proper_motion,
    search_radii,
    select_focus_star,
    years_since,
)

CENTER = (100.0, 30.0)
ISO = 0.05  # deg


def _select(stars, mag_limit=10.0, search=0.5, iso=ISO):
    ra, dec, mag = (np.array(c, dtype=float) for c in zip(*stars, strict=True))
    return select_focus_star(
        ra,
        dec,
        mag,
        center_ra_deg=CENTER[0],
        center_dec_deg=CENTER[1],
        mag_limit=mag_limit,
        search_radius_deg=search,
        isolation_radius_deg=iso,
    )


def test_the_brightest_star_no_brighter_than_the_limit_wins():
    star = _select([(100.1, 30.0, 8.0), (100.2, 30.0, 10.4), (100.3, 30.0, 10.1), (100.0, 30.2, 12.0)])
    assert star is not None
    assert star.mag == 10.1  # not 8.0 (too bright), not 10.4 or 12.0 (fainter than 10.1)


def test_equal_magnitudes_go_to_the_nearer_star():
    star = _select([(100.3, 30.0, 10.5), (100.1, 30.0, 10.5)])
    assert star is not None
    assert star.ra_deg == pytest.approx(100.1)


def test_a_brighter_neighbour_in_the_field_disqualifies_and_the_next_candidate_wins():
    star = _select(
        [
            (100.1, 30.0, 10.1),  # best candidate ...
            (100.1, 30.03, 9.0),  # ... but a brighter star sits 0.03 deg away
            (100.4, 30.0, 10.3),
        ]
    )
    assert star is not None
    assert star.mag == 10.3
    assert star.n_rejected == 1


def test_a_brighter_star_outside_the_isolation_radius_does_not_count():
    star = _select([(100.1, 30.0, 10.1), (100.1, 30.08, 6.0)])
    assert star is not None
    assert star.mag == 10.1


def test_a_fainter_neighbour_does_not_count():
    star = _select([(100.1, 30.0, 10.1), (100.1, 30.01, 14.0)])
    assert star is not None
    assert star.mag == 10.1


def test_no_candidate_inside_the_search_radius_is_none():
    assert _select([(101.0, 30.0, 10.1)], search=0.5) is None


def test_isolation_radius_is_the_box_half_diagonal_plus_margin():
    # 829 x 564 px at 0.2616"/px: half-diagonal 501.4 px, + 80 px margin
    r = isolation_radius_deg(829, 564, 0.2616, 80)
    assert r * 3600 == pytest.approx((0.5 * np.hypot(829, 564) + 80) * 0.2616)


def test_search_radii_grow_and_end_at_the_maximum():
    assert search_radii(0.5, 2.0, 4.0) == [0.5, 1.0, 2.0, 4.0]
    assert search_radii(0.5, 2.0, 3.0) == [0.5, 1.0, 2.0, 3.0]
    assert search_radii(1.0, 1.0, 4.0) == [4.0]  # no growth: straight to the maximum


class FakeCatalog:
    """Answers a cone from fixed stars (degrees), recording the radii asked for."""

    def __init__(self, stars):
        self.ra, self.dec, self.mag = (np.array(c, dtype=float) for c in zip(*stars, strict=True))
        self.radii = []

    def cone_search(self, ra_deg, dec_deg, radius_deg, columns):
        self.radii.append(radius_deg)
        from focus_on_star.star_selection import separation_deg

        inside = separation_deg(ra_deg, dec_deg, self.ra, self.dec) <= radius_deg
        data = {"RA": np.radians(self.ra[inside]), "Dec": np.radians(self.dec[inside]), "G": self.mag[inside]}
        data["PMRA"] = np.zeros(inside.sum())
        data["PMDec"] = np.zeros(inside.sum())
        return {c: data[c] for c in columns}


def _find(catalog, pm=None):
    return find_focus_star(
        catalog,
        ra_deg=CENTER[0],
        dec_deg=CENTER[1],
        mag_limit=10.0,
        isolation_deg=ISO,
        mag_column="G",
        search_radius_deg=0.5,
        search_growth=2.0,
        max_search_radius_deg=4.0,
        years_of_proper_motion=pm,
    )


def test_the_search_grows_until_a_star_is_found():
    cat = FakeCatalog([(100.0, 31.5, 10.2)])  # 1.5 deg away
    star = _find(cat)
    assert star is not None and star.search_radius_deg == 2.0
    # Each query reaches past the search radius by the isolation radius.
    assert cat.radii == pytest.approx([0.5 + ISO, 1.0 + ISO, 2.0 + ISO])


def test_a_neighbour_just_beyond_the_search_radius_is_still_seen():
    # Candidate at 0.49 deg; its brighter neighbour 0.53 deg out, inside the isolation radius.
    cat = FakeCatalog([(100.0, 30.49, 10.2), (100.0, 30.53, 7.0), (100.0, 31.0, 10.4)])
    star = _find(cat)
    assert star is not None
    assert star.mag == 10.4


def test_nothing_within_the_maximum_is_none():
    assert _find(FakeCatalog([(100.0, 36.0, 10.2)])) is None


def test_proper_motion_moves_dec_and_divides_ra_by_cos_dec():
    ra, dec = propagate_proper_motion(np.array([10.0]), np.array([60.0]), np.array([3600.0]), np.array([1800.0]), 10.0)
    # 3600 mas/yr * 10 yr = 36000 mas = 0.01 deg on the sky; / cos(60) = 0.02 deg of RA
    assert ra[0] == pytest.approx(10.02)
    assert dec[0] == pytest.approx(60.005)


def test_a_missing_proper_motion_leaves_the_position_alone():
    ra, dec = propagate_proper_motion(np.array([10.0]), np.array([0.0]), np.array([np.nan]), np.array([np.nan]), 10.0)
    assert (ra[0], dec[0]) == (10.0, 0.0)


def test_years_since_the_gaia_epoch():
    when = datetime.datetime(2026, 1, 1, 12, tzinfo=datetime.UTC)
    assert years_since(2016.0, when) == pytest.approx(10.0, abs=0.01)
