"""A plan's products, from the unit's side (plans/control_acquisition_design.md 5.10, 6.5).

With a `plan_ulid`, `start_acquisition_and_guiding` and `focus_on_star` label the products
folder they create with a `plan_id` file and tell control where it is, by stage. Without one
-- an operator's call -- they do neither. `execute_assignment`, which used to send these
notifications, is gone.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import common.notifications as notifications_module
import plan_products as plan_products_module
from acquirer import Acquirer
from common.canonical import CanonicalResponse
from common.filer import Filer
from common.models.plans import PLAN_ID_FILE, PlanStage
from common.notifications import NotificationInitiator
from plan_products import announce_plan_products
from unit import Unit

ULID = "01JAD8Q4ZK7V3M2XG9T6R5N0HB"


@pytest.fixture
def sent(monkeypatch) -> list:
    notifications: list = []
    monkeypatch.setattr(
        plan_products_module,
        "Notifier",
        lambda: SimpleNamespace(assignment_notification=notifications.append),
    )
    monkeypatch.setattr(Filer, "move_ram_to_shared", lambda self, paths: None)
    # A notification names its sender from the bootstrap config, which a test has none of.
    monkeypatch.setattr(
        notifications_module,
        "_build_initiator",
        lambda: NotificationInitiator(site="ns", type="unit", hostname="mast01", project="mast"),
    )
    return notifications


@pytest.fixture
def folder(request) -> str:
    """A products folder on the RAM area, as a producer makes one."""
    ram = Filer().ram
    assert ram is not None
    path = Path(ram.root, "2026-10-25", "Acquisitions", f"seq={abs(hash(request.node.name)) % 10000:04d}")
    path.mkdir(parents=True, exist_ok=True)
    return path.as_posix()


def _label(folder: str) -> str | None:
    path = os.path.join(folder, PLAN_ID_FILE)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return f.read().strip()


class TestAnnounce:
    def test_it_labels_the_folder_and_notifies_by_stage(self, sent, folder):
        assert announce_plan_products(ULID, PlanStage.Acquisition, folder)

        assert _label(folder) == ULID
        (n,) = sent
        assert n.assignment_id == ULID and n.state == "in-progress"
        assert n.stage is PlanStage.Acquisition
        assert n.shared_top == Filer().area_of(folder)[1], "relative to this machine's folder on the share"
        assert n.shared_subpath == "acquisition", "still sent, for a control that predates `stage`"

    def test_focus_on_star_is_the_autofocus_stage(self, sent, folder):
        announce_plan_products(ULID, PlanStage.Autofocus, folder)
        assert sent[0].stage is PlanStage.Autofocus and sent[0].shared_subpath == "focus_on_star"

    def test_without_a_plan_nothing_is_written_or_sent(self, sent, folder):
        assert not announce_plan_products(None, PlanStage.Acquisition, folder)
        assert _label(folder) is None and sent == []

    def test_without_a_folder_nothing_is_sent(self, sent):
        assert not announce_plan_products(ULID, PlanStage.Autofocus, None)
        assert sent == []

    def test_a_folder_under_no_filing_area_is_labelled_but_not_reported(self, sent, tmp_path):
        assert not announce_plan_products(ULID, PlanStage.Acquisition, tmp_path.as_posix())
        assert _label(tmp_path.as_posix()) == ULID
        assert sent == []


class TestStartAcquisition:
    @pytest.fixture
    def acquirer(self, monkeypatch, folder):
        a = Acquirer.__new__(Acquirer)
        monkeypatch.setattr(Acquirer, "prepare_acquisition", lambda self, **kw: SimpleNamespace(folder=folder))
        monkeypatch.setattr(Acquirer, "run_acquisition", lambda self, acquisition: None)
        return a

    def test_with_a_plan_the_folder_is_announced(self, acquirer, sent, folder):
        Acquirer.endpoint_start_acquisition_and_guiding(acquirer, plan_ulid=ULID)

        assert _label(folder) == ULID
        assert [n.stage for n in sent] == [PlanStage.Acquisition]

    def test_by_hand_nothing_is(self, acquirer, sent, folder):
        Acquirer.endpoint_start_acquisition_and_guiding(acquirer)
        assert _label(folder) is None and sent == []

    def test_a_refused_request_announces_nothing(self, monkeypatch, sent):
        a = Acquirer.__new__(Acquirer)
        monkeypatch.setattr(Acquirer, "prepare_acquisition", lambda self, **kw: CanonicalResponse(errors=["no"]))

        response = Acquirer.endpoint_start_acquisition_and_guiding(a, plan_ulid=ULID)

        assert response.failed and sent == []


class TestFocusOnStar:
    @pytest.fixture
    def unit(self, folder):
        started = SimpleNamespace(folder=folder)
        return SimpleNamespace(unit_conf=None, focus_on_star=SimpleNamespace(start=lambda params: started))

    def test_with_a_plan_the_run_folder_is_announced(self, unit, sent, folder):
        Unit.endpoint_focus_on_star(unit, plan_ulid=ULID)  # type: ignore[arg-type]

        assert _label(folder) == ULID
        assert [n.stage for n in sent] == [PlanStage.Autofocus]

    def test_by_hand_nothing_is(self, unit, sent, folder):
        Unit.endpoint_focus_on_star(unit)  # type: ignore[arg-type]
        assert _label(folder) is None and sent == []


def test_execute_assignment_is_gone():
    assert not hasattr(Unit, "endpoint_execute_assignment")
    assert not hasattr(Unit, "do_execute_assignment")
    assert not hasattr(Acquirer, "start_acquisition_and_guiding_for_assignment")
