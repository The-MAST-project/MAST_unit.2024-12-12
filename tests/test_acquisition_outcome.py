"""An acquisition records how it ended, so a caller waiting on it can tell success from failure.

`Acquiring` clears on every exit -- converged, a phase out of tolerance, cancelled -- so the flag
alone cannot say which. Flux metering read it as success and walked the spiral from an
unconverged pointing (MAST_unit#316).

Needs no hardware: every path here returns before touching a component.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from acquirer import REQUIRED_COMPONENTS, Acquirer
from common.canonical import CanonicalResponse


class _Unit:
    def __init__(self, missing=()):
        for name in REQUIRED_COMPONENTS:
            setattr(self, name, None if name in missing else object())
        self.errors: list[str] = []
        self.reference_image = None


def _acquirer(missing=()):
    a = object.__new__(Acquirer)
    a.unit = _Unit(missing)
    return a


@pytest.fixture(autouse=True)
def _no_folder_release(monkeypatch):
    import acquirer as acquirer_module

    class _Guardian:
        def release_folder(self, *args, **kwargs):
            pass

    monkeypatch.setattr(acquirer_module, "MoveGuardian", _Guardian)


def test_do_acquire_returns_why_it_did_not_acquire():
    a = _acquirer(missing={"solver"})

    failure = Acquirer.do_acquire(a, acquisition=None)

    assert failure is not None
    assert failure.endswith("these components did not initialize: solver")


def test_run_acquisition_records_the_failure_on_the_acquisition():
    a = _acquirer(missing={"stage"})
    acquisition = SimpleNamespace(folder="unused", failure="stale")

    Acquirer.run_acquisition(a, acquisition)

    assert acquisition.failure is not None
    assert "stage" in acquisition.failure


def test_run_acquisition_records_success_as_no_failure(monkeypatch):
    a = _acquirer()
    monkeypatch.setattr(a, "do_acquire", lambda acquisition: None, raising=False)
    acquisition = SimpleNamespace(folder="unused", failure="stale")

    Acquirer.run_acquisition(a, acquisition)

    assert acquisition.failure is None


def test_an_acquisition_that_raises_is_recorded_as_failed(monkeypatch):
    a = _acquirer()

    def boom(acquisition):
        raise RuntimeError("the solver fell over")

    monkeypatch.setattr(a, "do_acquire", boom, raising=False)
    acquisition = SimpleNamespace(folder="unused", failure=None)

    Acquirer.run_acquisition(a, acquisition)

    assert acquisition.failure is not None
    assert "the solver fell over" in acquisition.failure


def test_prepare_refuses_without_building_an_acquisition():
    a = _acquirer(missing={"mount"})

    prepared = Acquirer.prepare_acquisition(a)

    assert isinstance(prepared, CanonicalResponse)
    assert prepared.errors is not None
    assert "mount" in prepared.errors[0]
