"""`Mount.wait_until_settled`: its tolerance and stable-sample count come from the unit's configuration when the caller
gives none (they were hard-coded, with a TODO), and a caller's value still wins."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import mount as mount_module
from mount import SETTLE_STABLE_SAMPLES, SETTLE_TOLERANCE_ARCSEC, Mount, SettleMode


@pytest.fixture
def seen(monkeypatch):
    """A Mount with the wait itself replaced by a recorder of what it was asked to wait for."""
    recorded: dict = {}

    def fake_wait_slew(tolerance, samples, grace, poll, deadline, op):
        recorded.update(tolerance=tolerance, samples=samples)
        return True

    monkeypatch.setattr(Mount, "connected", property(lambda self: True))
    monkeypatch.setattr(Mount, "_wait_slew", lambda self, *a: fake_wait_slew(*a))
    monkeypatch.setattr(mount_module, "logger", SimpleNamespace(info=lambda m: None, warning=lambda m: None))
    return recorded


def _mount(mount_conf) -> Mount:
    m = object.__new__(Mount)
    m.unit = SimpleNamespace(unit_conf=SimpleNamespace(mount=mount_conf))
    return m


def test_without_configuration_the_module_defaults_apply(seen):
    _mount(SimpleNamespace(ascom_driver="x")).wait_until_settled(SettleMode.SLEW)

    assert (seen["tolerance"], seen["samples"]) == (SETTLE_TOLERANCE_ARCSEC, SETTLE_STABLE_SAMPLES) == (0.5, 2)


def test_the_units_configuration_is_used(seen):
    conf = SimpleNamespace(ascom_driver="x", settle_tolerance_arcsec=6.0, settle_stable_samples=3)

    _mount(conf).wait_until_settled(SettleMode.SLEW)

    assert (seen["tolerance"], seen["samples"]) == (6.0, 3)


def test_a_callers_value_wins_over_the_configuration(seen):
    conf = SimpleNamespace(ascom_driver="x", settle_tolerance_arcsec=6.0, settle_stable_samples=3)

    _mount(conf).wait_until_settled(SettleMode.SLEW, dist_tolerance_arcsec=10.0, stable_samples=5)

    assert (seen["tolerance"], seen["samples"]) == (10.0, 5)


def test_a_mount_with_no_unit_still_has_the_defaults(seen):
    m = object.__new__(Mount)  # a Mount built without a unit, as some tests do

    m.wait_until_settled(SettleMode.SLEW)

    assert (seen["tolerance"], seen["samples"]) == (0.5, 2)


def test_the_configuration_is_read_each_call_not_remembered(seen):
    conf = SimpleNamespace(ascom_driver="x", settle_tolerance_arcsec=6.0)
    m = _mount(conf)
    m.wait_until_settled(SettleMode.SLEW)
    assert seen["tolerance"] == 6.0

    conf.settle_tolerance_arcsec = 2.5  # edited in the database, picked up by the running unit
    m.wait_until_settled(SettleMode.SLEW)

    assert seen["tolerance"] == 2.5
