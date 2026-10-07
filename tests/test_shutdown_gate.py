"""While the unit shuts down, only status is answered (app.refuse_while_shutting_down).

Anything else -- an abort, a component's powerdown -- could interrupt what the shutdown is
doing: stop the covers part-way through closing, or power them off mid-close. The shutdown
aborts in-flight work itself, first.
"""

from __future__ import annotations

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

import app as app_module
from common.activities import UnitActivities

BASE = "/mast/api/v1/unit"


def _router(*routes: tuple[str, str]) -> APIRouter:
    router = APIRouter()
    for method, path in routes:
        router.add_api_route(path, endpoint=lambda p=path: {"value": p}, methods=[method])
    return router


class _Covers:
    api_router = _router(("GET", f"{BASE}/covers/status"), ("PUT", f"{BASE}/covers/powerdown"))


class _StubUnit:
    """What `create_app()` and the gate read: routers, component attributes, `is_active`."""

    def __init__(self, shutting_down: bool):
        self.active = {UnitActivities.ShuttingDown} if shutting_down else set()
        self.api_router = _router(
            ("GET", f"{BASE}/status"),
            ("GET", f"{BASE}/config"),
            ("PUT", f"{BASE}/startup"),
            ("PUT", f"{BASE}/shutdown"),
            ("PUT", f"{BASE}/abort"),
            ("PUT", f"{BASE}/powerdown"),
        )
        for attribute in app_module.COMPONENT_ATTRIBUTES:
            setattr(self, attribute, _Covers() if attribute == "covers" else None)

    def is_active(self, activity) -> bool:
        return activity in self.active

    def start_lifespan(self):
        pass

    def end_lifespan(self):
        pass


def _client(shutting_down: bool) -> TestClient:
    # No `with`: the lifespan (and its file sweep) is not what is under test.
    return TestClient(app_module.create_app(_StubUnit(shutting_down)))


REFUSED_WHILE_SHUTTING_DOWN = [
    ("PUT", f"{BASE}/startup"),
    ("PUT", f"{BASE}/shutdown"),
    ("PUT", f"{BASE}/abort"),
    ("PUT", f"{BASE}/powerdown"),
    ("PUT", f"{BASE}/covers/powerdown"),
    ("GET", f"{BASE}/config"),
]


@pytest.mark.parametrize("path", [f"{BASE}/status", f"{BASE}/covers/status"])
def test_status_is_answered_while_shutting_down(path):
    response = _client(shutting_down=True).get(path)

    assert response.status_code == 200
    assert response.json() == {"value": path}


@pytest.mark.parametrize(("method", "path"), REFUSED_WHILE_SHUTTING_DOWN)
def test_everything_else_is_refused_while_shutting_down(method, path):
    response = _client(shutting_down=True).request(method, path)

    body = response.json()
    assert body.get("errors"), body
    assert "shutting down" in body["errors"][0]
    assert path in body["errors"][0]


@pytest.mark.parametrize(("method", "path"), REFUSED_WHILE_SHUTTING_DOWN)
def test_nothing_is_refused_otherwise(method, path):
    response = _client(shutting_down=False).request(method, path)

    assert response.status_code == 200
    assert response.json() == {"value": path}


def test_the_bare_app_has_no_gate_to_trip():
    """`create_app(None)` -- what a test builds -- has no unit to ask."""
    response = TestClient(app_module.create_app(None)).get("/docs")

    assert response.status_code == 200
