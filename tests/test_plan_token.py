"""The unit's plan token (plans/control_acquisition_design.md 6.6).

The first accepted request carrying a `plan_id` makes the unit the plan's: `ExecutingPlan`
is raised and the token kept, and status reports it. Until an abort releases it, the
middleware refuses state-changing requests without that token; GET and abort pass.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from common.activities import UnitActivities
from common.plan_token import plan_ownership_middleware
from unit import Unit

PLAN, OTHER = "01PLAN", "01OTHER"


class Holder:
    """Enough of a Unit for its token methods: activities as a set."""

    claim_plan = Unit.claim_plan
    release_plan = Unit.release_plan
    plan_owner = Unit.plan_owner

    def __init__(self):
        self.activities: set = set()
        self.plan_id = None

    def is_active(self, activity):
        return activity in self.activities

    def start_activity(self, activity, **_):
        self.activities.add(activity)

    def end_activity(self, activity, **_):
        self.activities.discard(activity)


def test_a_request_with_a_token_makes_the_unit_the_plans():
    unit = Holder()
    unit.claim_plan(PLAN)
    assert unit.is_active(UnitActivities.ExecutingPlan)
    assert unit.plan_owner == PLAN


def test_one_without_does_not():
    unit = Holder()
    unit.claim_plan(None)
    assert unit.plan_owner is None and not unit.activities


def test_the_same_plan_again_keeps_it():
    unit = Holder()
    unit.claim_plan(PLAN)
    unit.claim_plan(PLAN)
    assert unit.plan_owner == PLAN


def test_released_it_is_nobodys():
    unit = Holder()
    unit.claim_plan(PLAN)
    unit.release_plan()
    assert unit.plan_owner is None and unit.plan_id is None
    assert not unit.is_active(UnitActivities.ExecutingPlan)


def test_abort_releases_it_last(monkeypatch):
    released = []

    class Aborted(Holder):
        autofocuser = None
        guider = None
        components: list = []
        flux_metering = type("F", (), {"is_active": False})()
        spiral = type("S", (), {"is_active": False})()

        def __init__(self):
            super().__init__()
            import threading

            self._expose_cancelled = threading.Event()

        def release_plan(self):
            released.append(True)
            Holder.release_plan(self)

    unit = Aborted()
    unit.claim_plan(PLAN)
    Unit.abort(unit)  # type: ignore[arg-type]
    assert released == [True] and unit.plan_owner is None


def test_over_http_the_owner_is_asked_live():
    unit = Holder()
    app = FastAPI()
    app.middleware("http")(plan_ownership_middleware(lambda: unit.plan_owner))

    @app.put("/mast/api/v1/unit/mount/park")
    def park():
        return {"api_version": "1.0", "value": "ok"}

    client = TestClient(app)
    assert client.put("/mast/api/v1/unit/mount/park").json()["value"] == "ok"
    unit.claim_plan(PLAN)
    assert "already working on plan 01PLAN" in client.put("/mast/api/v1/unit/mount/park").json()["errors"][0]
    assert client.put("/mast/api/v1/unit/mount/park", params={"plan_id": OTHER}).json()["errors"]
    assert client.put("/mast/api/v1/unit/mount/park", params={"plan_id": PLAN}).json()["value"] == "ok"
