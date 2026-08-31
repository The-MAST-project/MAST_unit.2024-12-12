"""An acquisition refuses when a component did not build, instead of asserting on a thread (#83).

`do_acquire` opened with six `assert ... is not None` on components, and the endpoint spawns it
on a thread and returns `Ok`. So with a component unbuilt -- which a PHD2 connect failure
produces routinely (#84) -- the caller was told the acquisition had started, `unit.errors` had
just been cleared one line above the asserts, and the only record was an `AssertionError` in a
log file. `assert` was the wrong construct twice over: it states a thing cannot happen, and this
is the code path that runs when it does.

The endpoint now refuses synchronously, naming every missing component in one answer rather than
the first one, and `do_acquire` records on `unit.errors` -- which is what `/unit/status`
publishes -- for any caller that did not check.

Needs no hardware: the refusal is reached before the endpoint touches PWI4.
"""

from __future__ import annotations

import pytest

from acquirer import REQUIRED_COMPONENTS, Acquirer


class _Unit:
    def __init__(self, missing=()):
        for name in REQUIRED_COMPONENTS:
            setattr(self, name, None if name in missing else object())
        self.errors: list[str] = ["stale, must be cleared"]
        self.reference_image = "stale"


def _acquirer(missing=()):
    a = object.__new__(Acquirer)
    a.unit = _Unit(missing)
    return a


def test_missing_components_lists_them_in_declaration_order():
    a = _acquirer(missing={"imager", "mount"})

    assert a.missing_components() == ["mount", "imager"]


def test_a_complete_unit_reports_nothing_missing():
    assert _acquirer().missing_components() == []


def test_the_endpoint_refuses_and_names_every_missing_component():
    a = _acquirer(missing={"guider", "imager"})

    response = Acquirer.endpoint_start_acquisition_and_guiding(a)

    assert response.errors is not None
    assert len(response.errors) == 1
    assert "guider, imager" in response.errors[0]


def test_the_endpoint_starts_no_thread_when_a_component_is_missing(monkeypatch):
    """The point of refusing at the endpoint: nothing is spawned, so nothing can report Ok."""
    import acquirer as acquirer_module

    started: list[str] = []

    class _Thread:
        def __init__(self, *args, **kwargs):
            started.append(kwargs.get("name", "?"))

        def start(self):
            started.append("started")

    monkeypatch.setattr(acquirer_module, "Thread", _Thread)
    a = _acquirer(missing={"stage"})

    Acquirer.endpoint_start_acquisition_and_guiding(a)

    assert started == []


def test_do_acquire_records_on_unit_errors_rather_than_asserting():
    a = _acquirer(missing={"solver"})

    Acquirer.do_acquire(a, acquisition=None)

    assert len(a.unit.errors) == 1
    assert a.unit.errors[0].endswith("cannot acquire, these components did not initialize: solver")


def test_do_acquire_clears_the_previous_run_before_recording():
    a = _acquirer(missing={"mount"})

    Acquirer.do_acquire(a, acquisition=None)

    assert "stale, must be cleared" not in a.unit.errors
    assert a.unit.reference_image is None


def test_no_component_assert_survives_on_the_acquisition_path():
    """A precondition is a refusal, not an assertion -- and asserts vanish under -O."""
    import inspect

    source = inspect.getsource(Acquirer.do_acquire)

    for name in REQUIRED_COMPONENTS:
        assert f"assert self.unit.{name} is not None" not in source


@pytest.mark.parametrize("missing", [{"mount"}, {"guider"}, {"imager"}, {"solver"}, {"stage"}])
def test_every_required_component_is_individually_enforced(missing):
    a = _acquirer(missing=missing)

    response = Acquirer.endpoint_start_acquisition_and_guiding(a)

    assert response.errors is not None
    assert next(iter(missing)) in response.errors[0]
