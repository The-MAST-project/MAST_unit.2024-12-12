"""Sensor temperature must reach the FITS header, and must never break a frame.

PHD2 writes no temperature at all, so a frame cannot be matched to a dark and a
dark library cannot be validated.  These pin the two halves: that a bracketed
reading lands in the header with the right keywords, and that every way of
having no reading degrades quietly instead of failing an exposure that already
succeeded.
"""

import numpy as np
import pytest
from astropy.io import fits

from phd2.fits_header import stamp_cooling


@pytest.fixture
def frame(tmp_path):
    """A minimal FITS file standing in for a saved PHD2 frame."""
    path = tmp_path / "001_FOCUS10250.fits"
    fits.PrimaryHDU(np.zeros((8, 8), dtype=np.uint16)).writeto(path)
    return str(path)


def status(temperature, setpoint=5.0, cooler_on=True, power=100.0):
    return {
        "temperature": temperature,
        "setpoint": setpoint,
        "coolerOn": cooler_on,
        "power": power,
    }


def test_bracketed_reading_lands_in_the_header(frame):
    stamp_cooling(frame, status(23.9), status(24.3))

    h = fits.getheader(frame)
    # CCD-TEMP is the MEAN, and the standard keyword, so ordinary tooling finds it.
    assert h["CCD-TEMP"] == pytest.approx(24.1)
    assert h["CCDTEMP1"] == pytest.approx(23.9)
    assert h["CCDTEMP2"] == pytest.approx(24.3)
    assert h["SET-TEMP"] == pytest.approx(5.0)
    assert h["COOLERON"] is True
    assert h["COOLPOWR"] == pytest.approx(100.0)


def test_drift_during_the_exposure_is_recorded(frame):
    """The pair's whole point: a single number cannot show in-frame drift.

    With the cooler pinned at 100% and not holding setpoint, the sensor tracks
    ambient -- CCDTDELT is what makes that visible per-frame.
    """
    stamp_cooling(frame, status(23.9), status(24.3))

    assert fits.getheader(frame)["CCDTDELT"] == pytest.approx(0.4)


def test_one_sided_reading_still_records_what_it_has(frame):
    """A failed post-exposure read must not discard the pre-exposure one."""
    stamp_cooling(frame, status(23.9), None)

    h = fits.getheader(frame)
    assert h["CCD-TEMP"] == pytest.approx(23.9)
    assert h["CCDTEMP1"] == pytest.approx(23.9)
    assert "CCDTEMP2" not in h
    assert "CCDTDELT" not in h, "no delta is knowable from one sample"


@pytest.mark.parametrize(
    "path, before, after",
    [
        pytest.param(None, status(23.9), status(24.0), id="memory-imager-has-no-file"),
        pytest.param("frame", None, None, id="no-reading-at-all"),
    ],
)
def test_nothing_to_stamp_is_not_an_error(frame, path, before, after):
    stamp_cooling(frame if path else None, before, after)

    if path:  # file untouched, still readable
        assert "CCD-TEMP" not in fits.getheader(frame)


def test_unwritable_file_does_not_raise(tmp_path):
    """The frame is already safely on disk -- losing the stamp must not lose it."""
    stamp_cooling(str(tmp_path / "does-not-exist.fits"), status(23.9), status(24.0))
