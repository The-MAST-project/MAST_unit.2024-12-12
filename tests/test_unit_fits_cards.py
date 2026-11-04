"""The unit's FITS cards: the project-standard ones plus the unit's own, on both ways a frame is written."""

import datetime
from types import SimpleNamespace

import numpy as np
import pytest
from astropy.io import fits

from common.fits_headers import stamp_fits_file
from common.models.latest import LatestMeasuredSkyQuality, LatestSkyQuality
from imaging.fits_cards import unit_fits_cards


def _unit(plan=None, sky=None, focuser=21000, stage=50000):
    return SimpleNamespace(
        plan_owner=plan,
        _latest_sky_quality=lambda: sky,
        focuser=SimpleNamespace(position=focuser) if focuser is not None else None,
        stage=SimpleNamespace(position=stage) if stage is not None else None,
    )


def _fresh_measurement():
    m = LatestMeasuredSkyQuality(
        observed_at=(datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=5)).isoformat(), fwhm_arcsec=2.1
    ).begin()
    m.finish(True)
    return LatestSkyQuality(measured=m)


def test_the_standard_cards_and_the_unit_s_own():
    cards = unit_fits_cards(_unit(plan="01PLAN", sky=_fresh_measurement()))
    assert cards["ORIGIN"][0] == "MAST"
    assert cards["PLANID"][0] == "01PLAN"
    assert cards["SQMFWHM"][0] == 2.1
    assert cards["FOCUSPOS"][0] == 21000 and cards["STAGEPOS"][0] == 50000


def test_a_component_that_cannot_answer_loses_its_card_not_the_rest():
    class Broken:
        @property
        def position(self):
            raise RuntimeError("the stage is not connected")

    unit = _unit()
    unit.stage = Broken()
    cards = unit_fits_cards(unit)
    assert "STAGEPOS" not in cards and "FOCUSPOS" in cards and "ORIGIN" in cards


def test_a_failing_sky_quality_read_still_gives_the_unit_s_cards():
    unit = _unit()
    unit._latest_sky_quality = lambda: (_ for _ in ()).throw(RuntimeError("no guider"))
    cards = unit_fits_cards(unit)
    assert "FOCUSPOS" in cards


def test_no_unit_gives_the_standard_cards():
    assert unit_fits_cards(None)["ORIGIN"][0] == "MAST"


@pytest.mark.parametrize("missing", ["focuser", "stage"])
def test_a_missing_component_has_no_card(missing):
    cards = unit_fits_cards(_unit(**{missing: None}))
    assert ("FOCUSPOS" if missing == "focuser" else "STAGEPOS") not in cards


def test_a_phd2_frame_is_stamped_in_place(tmp_path):
    path = tmp_path / "001_GUIDE.fits"
    fits.PrimaryHDU(np.zeros((4, 4), dtype=np.uint16)).writeto(path)

    assert stamp_fits_file(path, unit_fits_cards(_unit(plan="01PLAN")))

    header = fits.getheader(path)
    assert header["PLANID"] == "01PLAN" and header["FOCUSPOS"] == 21000
