"""Every assignment acquires; autofocus is an optional step before it (#345).

`do_execute_assignment` nested the acquisition inside `if assignment.plan.autofocus:`,
so a plan with the default `autofocus=False` was accepted and then did nothing. On
the control side that surfaced only as "did not reach 'guiding' within 600 seconds".

The acquisition goes through `Acquirer.start_acquisition_and_guiding_for_assignment`:
the configured exposure, automatic hand-over to guiding, and a products path measured
from the ram root, where the folder is when the notification is sent.
"""

from __future__ import annotations

import os
import types

import pytest

import acquirer as acquirer_module
from acquirer import Acquirer
from common.canonical import CanonicalResponse, CanonicalResponse_Ok
from common.filer import Filer

RA_HOURS = 5.5
DEC_DEGREES = -12.25
ULID = "01JTESTASSIGNMENT0000000000"


class Autofocuser:
    def __init__(self, calls: list[str], result: object | None):
        self.calls = calls
        self.result = result
        self.latest_result: object | None = None

    def start_autofocus(self, ra_j2000_hours, dec_j2000_degs) -> None:
        self.calls.append("autofocus")
        self.latest_result = self.result


class AssignmentAcquirer:
    def __init__(self, calls: list[str]):
        self.calls = calls
        self.assignments: list = []

    def start_acquisition_and_guiding_for_assignment(self, assignment) -> CanonicalResponse:
        self.calls.append("acquire")
        self.assignments.append(assignment)
        return CanonicalResponse_Ok


def _unit(autofocus_result: object | None = object()):
    from unit import Unit

    calls: list[str] = []
    unit = object.__new__(Unit)
    unit.autofocuser = Autofocuser(calls, autofocus_result)
    unit.acquirer = AssignmentAcquirer(calls)
    unit.guider = object()
    unit.imager = types.SimpleNamespace(latest_settings=None)
    unit.is_active = lambda activity: False
    return unit, calls


def _assignment(autofocus: bool | None, ulid: str | None = None):
    target = types.SimpleNamespace(ra_hours=RA_HOURS, dec_degrees=DEC_DEGREES)
    return types.SimpleNamespace(plan=types.SimpleNamespace(autofocus=autofocus, target=target, ulid=ulid))


# ------------------------------------------------------------------ do_execute_assignment


@pytest.mark.parametrize("autofocus", [False, None])
def test_a_plan_without_autofocus_acquires(autofocus):
    unit, calls = _unit()
    assignment = _assignment(autofocus)

    unit.do_execute_assignment(assignment)

    assert calls == ["acquire"]
    assert unit.acquirer.assignments == [assignment]


def test_a_plan_with_autofocus_focuses_then_acquires():
    unit, calls = _unit()

    unit.do_execute_assignment(_assignment(True))

    assert calls == ["autofocus", "acquire"]


def test_a_failed_autofocus_does_not_acquire():
    unit, calls = _unit(autofocus_result=None)

    unit.do_execute_assignment(_assignment(True))

    assert calls == ["autofocus"]


def test_the_autofocus_notification_is_measured_from_the_ram_root(monkeypatch):
    import unit as unit_module

    notifications: list = []

    class _Notifier:
        def assignment_notification(self, notification):
            notifications.append(notification)

    monkeypatch.setattr(unit_module, "Notifier", _Notifier)
    monkeypatch.setattr(unit_module, "AssignmentNotification", types.SimpleNamespace)
    ram = unit_module.filer.ram
    assert ram is not None
    image_path = os.path.join(ram.root, "2026-10-19", "Autofocus", "seq=0001", "frame.fits")
    unit, calls = _unit()
    unit.imager = types.SimpleNamespace(latest_settings=types.SimpleNamespace(image_path=image_path))

    unit.do_execute_assignment(_assignment(True, ulid=ULID))

    assert calls == ["autofocus", "acquire"]
    [notification] = notifications
    assert notification.shared_top == os.path.join("2026-10-19", "Autofocus", "seq=0001")
    assert notification.shared_subpath == "autofocus"


# ------------------------------------------------------------------ the assignment helper


class Recorder:
    def __init__(self):
        self.threads: list = []
        self.notifications: list = []


@pytest.fixture
def recorder(monkeypatch):
    rec = Recorder()

    class _Thread:
        def __init__(self, name, target, args):
            self.args = args

        def start(self):
            rec.threads.append(self.args)

    class _Notifier:
        def assignment_notification(self, notification):
            rec.notifications.append(notification)

    monkeypatch.setattr(acquirer_module, "Thread", _Thread)
    monkeypatch.setattr(acquirer_module, "Notifier", _Notifier)
    # The real model stamps its initiator from the unit's config file on construction.
    monkeypatch.setattr(acquirer_module, "AssignmentNotification", types.SimpleNamespace)
    return rec


def _helper_acquirer(prepared):
    a = object.__new__(Acquirer)
    a.prepare_calls = []

    def prepare_acquisition(**kwargs):
        a.prepare_calls.append(kwargs)
        return prepared

    a.prepare_acquisition = prepare_acquisition
    return a


def test_the_helper_prepares_with_the_configured_exposure_and_hands_over(recorder):
    ram = Filer().ram
    assert ram is not None
    folder = os.path.join(ram.root, "2026-10-19", "Acquisitions", "seq=0001")
    acquisition = types.SimpleNamespace(folder=folder)
    a = _helper_acquirer(acquisition)

    response = a.start_acquisition_and_guiding_for_assignment(_assignment(False, ulid=ULID))

    assert not response.errors
    assert a.prepare_calls == [
        {
            "seconds": None,
            "ra_j2000_hours": RA_HOURS,
            "dec_j2000_degs": DEC_DEGREES,
            "handover_automatically_to_guider": True,
        }
    ]
    assert recorder.threads == [[acquisition]]
    [notification] = recorder.notifications
    assert notification.assignment_id == ULID
    assert notification.shared_top == os.path.join("2026-10-19", "Acquisitions", "seq=0001")
    assert notification.shared_subpath == "acquisition"


def test_a_refused_acquisition_is_returned_and_nothing_starts(recorder):
    refusal = CanonicalResponse(errors=["no target coordinates were determined"])
    a = _helper_acquirer(refusal)

    response = a.start_acquisition_and_guiding_for_assignment(_assignment(False, ulid=ULID))

    assert response is refusal
    assert recorder.threads == []
    assert recorder.notifications == []
