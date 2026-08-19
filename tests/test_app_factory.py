"""`app.py` is importable, and `create_app()` owns the route table (#114, prerequisite for #52).

Three defects are pinned here, each of which made the HTTP surface untestable:

1. **Router mounting lived in the `if __name__ == "__main__"` block**, so an imported
   `app` carried one route (`/favicon.ico`) and none of the unit's 74 registered
   operations could be reached. `test_create_app_mounts_*` is the guard.
2. **Importing the module started processes** -- PWI4, PWShutter, ps3cli -- and blocked
   up to 30 s waiting for PWI4. The guard is structural rather than an assertion: this
   module imports `app` at top level, and `conftest._block_external_processes` raises on
   any spawn, so re-introducing a module-level `ensure_process_is_running` turns this
   file into a collection error.
3. **`lifespan` called `Unit()` off a global** that only the `__main__` block bound, so
   on every other entry point it raised `NameError` into a bare `except Exception` and
   the unit's lifespan hooks were skipped in silence. `test_lifespan_*` is the guard.

Deliberately cross-platform: nothing here imports a component module, so it runs on a
dev Mac as well as on Windows. That is the point -- an importable `app` is what makes the
rest of #52's suite platform-independent, and a Windows-only guard could not show it.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.testclient import TestClient

import app as app_module
from common.endpoints import TIER_TAGS, Tier

# Defect 2's real assertion: reaching this line means importing `app` spawned nothing.
# `conftest` installs the process guard before collection, so a module-level spawn would
# raise `ProcessLaunchError` above rather than fail a test below.


def _router(path: str) -> APIRouter:
    router = APIRouter()
    router.add_api_route(path, endpoint=lambda: {"path": path})
    return router


class _StubUnit:
    """The shape `create_app()` reads: a unit router plus the five component attributes.

    Not a `Unit`. Building a real one needs Windows, hardware drivers and Mongo -- the
    coupling `create_app()` exists to keep out of the app-construction path -- and the
    factory only ever touches `.api_router` and the component names.
    """

    def __init__(self, **absent: bool):
        self.api_router = _router("/unit/status")
        for attribute in app_module.COMPONENT_ATTRIBUTES:
            component = None if absent.get(attribute) else _Component(attribute)
            setattr(self, attribute, component)
        self.calls: list[str] = []

    def start_lifespan(self):
        self.calls.append("start")

    def end_lifespan(self):
        self.calls.append("end")


class _Component:
    def __init__(self, name: str):
        self.api_router = _router(f"/unit/{name}/status")


def _paths(app) -> set[str]:
    """The app's HTTP surface, read from the OpenAPI schema rather than from `app.routes`.

    FastAPI 0.139 does not flatten an `include_router()` call into `app.routes`: it appends
    one opaque `_IncludedRouter` wrapper whose whole interface is private, so the mounted
    paths are simply not enumerable there. `app.openapi()["paths"]` is the public reading,
    and it is also the one consumers see in Swagger.

    The caveat to carry into #52's anchor test: a route registered with
    `include_in_schema=False` is invisible here. Nothing in the unit sets it (verified on
    `65a1b96`), but an anchor test asserting routes-versus-manifest exactness has to fail
    on such a route rather than silently omit it from both sides.
    """
    return set(app.openapi()["paths"])


def test_create_app_mounts_the_unit_and_component_routers():
    paths = _paths(app_module.create_app(_StubUnit()))

    assert "/unit/status" in paths
    for attribute in app_module.COMPONENT_ATTRIBUTES:
        assert f"/unit/{attribute}/status" in paths, f"{attribute} router not mounted"


def test_create_app_without_a_unit_mounts_no_component_routes():
    """The bare app still has to be constructible -- it is what a schema-only caller wants."""
    paths = _paths(app_module.create_app())

    assert "/favicon.ico" in paths
    assert not any(path.startswith("/unit") for path in paths)


def test_a_component_that_failed_to_build_is_skipped():
    """A unit missing a component serves the rest, rather than refusing to start."""
    paths = _paths(app_module.create_app(_StubUnit(focuser=True)))

    assert "/unit/focuser/status" not in paths
    assert "/unit/mount/status" in paths
    assert "/unit/status" in paths


def test_the_mounted_routes_answer():
    """Mounting is real, not merely present in the route table."""
    with TestClient(app_module.create_app(_StubUnit())) as client:
        assert client.get("/unit/mount/status").json() == {"path": "/unit/mount/status"}


def test_lifespan_runs_the_unit_hooks():
    unit = _StubUnit()

    with TestClient(app_module.create_app(unit)):
        assert unit.calls == ["start"]

    assert unit.calls == ["start", "end"]


def test_lifespan_without_a_unit_starts_and_stops_cleanly():
    """No unit, no hooks -- and the app still completes its startup and shutdown."""
    with TestClient(app_module.create_app()) as client:
        # The favicon route redirects into `/static`, which nothing mounts here; the
        # redirect itself is the evidence that the route is wired and serving.
        assert client.get("/favicon.ico", follow_redirects=False).status_code == 307


def test_the_root_redirects_to_the_swagger_page():
    """#170: the bare root used to 404 -- no route was registered for it at all.

    Asserted through the client rather than through `_paths()`: the route is
    `include_in_schema=False`, so it is invisible in the OpenAPI schema by design.
    """
    with TestClient(app_module.create_app()) as client:
        response = client.get("/", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/docs"


def test_redoc_is_not_served():
    """#170: `redocs_url=None` was a misspelling of `redoc_url`, so ReDoc stayed up.

    FastAPI absorbs an unknown keyword into `**extra` without complaint, which is why
    nothing objected for as long as the line stood. This is the check that would have
    caught it, and the one that keeps the corrected spelling from rotting back.
    """
    with TestClient(app_module.create_app()) as client:
        assert client.get("/redoc").status_code == 404


def test_the_schema_declares_the_tier_groups_in_display_order():
    """#39: Swagger groups by contract tier, most depended-upon first, each with its promise."""
    schema = app_module.create_app().openapi()

    assert [group["name"] for group in schema["tags"]] == [TIER_TAGS[tier] for tier in Tier]
    assert all(group["description"] for group in schema["tags"])
