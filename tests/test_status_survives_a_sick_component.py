"""One sick component costs its own section of `status`, not the whole response (#222).

`Unit.status` read nine live values inline, so any one of them raising returned a canonical
error with no value at all -- no mount, no covers, no activity flags. That is the wrong
failure mode for the one endpoint a caller reaches for *because* something is already wrong.

Not hypothetical: on mast01, 2026-09-08, a crossed PHD2 reply (#220) made `guider_status()`
raise a pydantic `ValidationError`, and `GET /unit/status` returned nothing usable while the
mount, covers, focuser and stage were all healthy and answering on their own routes.
"""

from __future__ import annotations

import pytest

from common.models.statuses import ComponentStatus, FullUnitStatus
from unit import Unit

STANDING_ERROR = "a standing unit error"


class Part:
    """A component whose `status()` either answers or fails."""

    def __init__(self, failure: str | None = None):
        self.failure = failure

    def status(self):
        if self.failure:
            raise RuntimeError(self.failure)
        return None


class Guider(Part):
    """The guider is read twice -- `status()` and `is_guiding` -- and they fail separately."""

    def __init__(self, failure: str | None = None, guiding_failure: str | None = None):
        super().__init__(failure)
        self.guiding_failure = guiding_failure

    @property
    def is_guiding(self) -> bool:
        if self.guiding_failure:
            raise RuntimeError(self.guiding_failure)
        return True


class Autofocuser:
    is_autofocusing = False


class FluxMetering:
    """A session that has run, so `status` takes the branch that actually reads it."""

    has_run = True

    def __init__(self, failure: str | None = None):
        self.failure = failure

    def status(self):
        if self.failure:
            raise RuntimeError(self.failure)
        return None


class Stub:
    """Enough of a Unit to run `status`'s own body."""

    def __init__(self, **parts):
        self.autofocus_result = None
        self.errors = [STANDING_ERROR]
        self.autofocuser = Autofocuser()
        self.guider = parts.pop("guider", Guider())
        for name in ("power_switch", "mount", "imager", "covers", "focuser", "stage"):
            setattr(self, name, parts.pop(name, Part()))
        # Not in the loop above: it is not a component and is never None on a real Unit --
        # `Unit.__init__` always builds a FluxMeteringSession -- but `status` reads it, so a
        # Stub without one raises where the real thing cannot.
        self.flux_metering = parts.pop("flux_metering", FluxMetering())
        assert not parts, f"unknown parts: {sorted(parts)}"

    def component_status(self) -> ComponentStatus:
        return ComponentStatus(
            detected=True,
            connected=True,
            activities=0,
            activities_verbal=None,
            operational=True,
            why_not_operational=[],
            was_shut_down=False,
        )


def status_of(stub: Stub) -> FullUnitStatus:
    return Unit.status(stub)  # type: ignore[arg-type]


class TestAHealthyUnit:
    def test_nothing_is_reported_that_did_not_fail(self):
        status = status_of(Stub())

        assert status.errors == [STANDING_ERROR]
        assert status.guiding is True


class TestOneSickComponent:
    def test_the_response_is_still_produced(self):
        status = status_of(Stub(guider=Guider(failure="PHD2 replied with someone else's answer")))

        assert isinstance(status, FullUnitStatus), "a sick guider must not cost the caller the response"
        assert status.guider is None

    def test_the_failure_is_named_rather_than_swallowed(self):
        status = status_of(Stub(mount=Part(failure="PWI4 is not answering")))

        assert status.errors is not None
        offending = [e for e in status.errors if e.startswith("mount.status")]
        assert len(offending) == 1, status.errors
        assert "PWI4 is not answering" in offending[0], "the error must carry what actually went wrong"

    def test_the_unit_s_own_errors_are_kept(self):
        status = status_of(Stub(covers=Part(failure="ASCOM threw")))

        assert status.errors is not None
        assert STANDING_ERROR in status.errors

    def test_the_unit_s_own_error_list_is_not_mutated(self):
        """`self.errors` is the unit's standing state; a read that failed on one request has
        no business being appended to it."""
        stub = Stub(stage=Part(failure="the controller is gone"))

        status_of(stub)
        status_of(stub)

        assert stub.errors == [STANDING_ERROR]

    def test_the_other_components_still_report(self):
        stub = Stub(guider=Guider(failure="boom"))

        status = status_of(stub)

        assert status.errors is not None
        assert [e for e in status.errors if "unavailable" in e] == [
            e for e in status.errors if e.startswith("guider.status")
        ], "only the guider failed, so only the guider may be reported"


class TestTheTwoLiveProperties:
    """`is_guiding` reaches PHD2 over the RPC and `is_autofocusing` reads the connection state.
    They are live reads, not attribute lookups, and were on the same all-or-nothing path."""

    def test_a_failing_is_guiding_does_not_cost_the_guider_s_status(self):
        status = status_of(Stub(guider=Guider(guiding_failure="no reply from PHD2")))

        assert status.guiding is False, "the default has to be a usable value, not None"
        assert status.errors is not None
        assert any(e.startswith("guider.is_guiding") for e in status.errors)
        assert not any(e.startswith("guider.status") for e in status.errors), "the other read succeeded"


class TestEverythingSick:
    def test_a_status_still_comes_back(self):
        stub = Stub(
            guider=Guider(failure="g", guiding_failure="ig"),
            power_switch=Part(failure="ps"),
            mount=Part(failure="m"),
            imager=Part(failure="i"),
            covers=Part(failure="c"),
            focuser=Part(failure="f"),
            stage=Part(failure="s"),
        )

        status = status_of(stub)

        assert status.errors is not None
        assert len([e for e in status.errors if "unavailable" in e]) == 8
        assert STANDING_ERROR in status.errors
        assert status.date is not None, "the unit's own fields are unaffected"


@pytest.mark.parametrize("part", ["power_switch", "mount", "imager", "covers", "focuser", "stage"])
def test_every_component_is_guarded(part: str):
    status = status_of(Stub(**{part: Part(failure="down")}))

    assert status.errors is not None
    assert any(e.startswith(f"{part}.status") for e in status.errors)
