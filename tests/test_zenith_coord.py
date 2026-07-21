"""Calibration points at the ZENITH by default.

Zenith = current LST + the site's latitude.  Minimum airmass, so the least
refraction/extinction/seeing degradation -- which matters because every
calibration product (focus, coma-derived optical centre, stage-shadow geometry)
is measured from star *shapes*.  The previous default of ``dec = 20.0`` pointed
~10 degrees off zenith at Neot Smadar (latitude 30.05).

The two axes resolve at different moments on purpose, and that asymmetry is
what these pin:

* ``ra`` stays ``None`` -- LST advances while the phase does its hardware
  preparation, so the caller substitutes it at slew time;
* ``dec`` resolves immediately to the latitude, which does not change.

Latitude is taken from the MOUNT, not the MAST config: the mount is what
actually points, and it reports latitude in the same status object the phases
already read for the LST.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from calibration.calibrator import Calibrator

MOUNT_LATITUDE = 30.0527777777778  # what PWI4 reports at Neot Smadar
CONFIG_LATITUDE = 30.05301166519461  # what the MAST config carries (differs ~2m)


def calibrator_with(pw=None, coord_dec=None, coord_ra=None):
    """A Calibrator whose settings/unit are stubbed -- no hardware, no DB."""
    c = Calibrator.__new__(Calibrator)
    c._initialized = False
    c.__init__(None)
    unit = MagicMock()
    unit.pw = pw
    unit.unit_conf.calibration.settings.coord = SimpleNamespace(ra=coord_ra, dec=coord_dec)
    c.unit = unit
    return c


def mount_at(latitude):
    pw = MagicMock()
    pw.status.return_value.site.latitude_degs = latitude
    return pw


def test_dec_defaults_to_the_mounts_latitude():
    """The headline: no configured dec -> point at the zenith."""
    c = calibrator_with(pw=mount_at(MOUNT_LATITUDE))

    ra, dec = c.resolve_coord(None, None)

    assert dec == pytest.approx(MOUNT_LATITUDE)
    assert ra is None, "ra stays deferred -- LST is resolved at slew time"


def test_the_mount_wins_over_the_configured_latitude(monkeypatch):
    """Two sources of truth exist and differ; pointing must follow the thing
    that actually slews."""
    config = MagicMock()
    config.local_site.location.latitude = CONFIG_LATITUDE
    monkeypatch.setattr("calibration.calibrator.Config", lambda: config)

    c = calibrator_with(pw=mount_at(MOUNT_LATITUDE))

    _, dec = c.resolve_coord(None, None)

    assert dec == pytest.approx(MOUNT_LATITUDE)
    assert dec != pytest.approx(CONFIG_LATITUDE)


def test_falls_back_to_the_configured_latitude_without_a_mount(monkeypatch):
    config = MagicMock()
    config.local_site.location.latitude = CONFIG_LATITUDE
    monkeypatch.setattr("calibration.calibrator.Config", lambda: config)

    c = calibrator_with(pw=None)

    _, dec = c.resolve_coord(None, None)

    assert dec == pytest.approx(CONFIG_LATITUDE)


def test_a_broken_mount_falls_back_rather_than_raising(monkeypatch):
    config = MagicMock()
    config.local_site.location.latitude = CONFIG_LATITUDE
    monkeypatch.setattr("calibration.calibrator.Config", lambda: config)
    pw = MagicMock()
    pw.status.side_effect = ConnectionError("PWI4 unreachable")

    _, dec = calibrator_with(pw=pw).resolve_coord(None, None)

    assert dec == pytest.approx(CONFIG_LATITUDE)


def test_no_latitude_anywhere_yields_None_not_an_exception(monkeypatch):
    """Every phase treats a missing coordinate as "calibrate at the current
    pointing" -- a better outcome than failing a run over a default."""
    config = MagicMock()
    config.local_site.location.latitude = None
    monkeypatch.setattr("calibration.calibrator.Config", lambda: config)

    _, dec = calibrator_with(pw=None).resolve_coord(None, None)

    assert dec is None


@pytest.mark.parametrize(
    "explicit_dec, configured_dec, expected",
    [
        pytest.param(45.0, 20.0, 45.0, id="explicit-argument-wins"),
        pytest.param(None, 20.0, 20.0, id="configured-dec-wins-over-zenith"),
        pytest.param(None, None, MOUNT_LATITUDE, id="zenith-is-the-last-resort"),
    ],
)
def test_resolution_order(explicit_dec, configured_dec, expected):
    """explicit -> config -> observatory. A site that deliberately configures a
    dec must keep getting it."""
    c = calibrator_with(pw=mount_at(MOUNT_LATITUDE), coord_dec=configured_dec)

    _, dec = c.resolve_coord(None, explicit_dec)

    assert dec == pytest.approx(expected)
