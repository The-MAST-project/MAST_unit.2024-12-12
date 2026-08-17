"""Invariant 4 of the endpoint contract: every routed handler returns a CanonicalResponse.

Covers the refusal paths remediated in MAST_unit #47 -- the cases where a handler
previously fell off the end (an HTTP ``null``) or bare-``return``ed (a success-shaped
empty body), so a caller could not tell "I refused" from "it worked".

The whole module is Windows-only: ``focuser``/``covers``/``mount`` import ``win32com`` at
module scope, so it skips on a dev machine the same way the rest of the suite does. No
hardware, Mongo or PWI4 is needed -- components are built with ``object.__new__`` and given
only the state the path under test reads, and the method under test is always the real one.

Both imager backends import again as of #72, so the ZWO and ASCOM envelope cases below
run rather than skip; the temporary ``import_backend_or_skip`` helper that stood in while
#71 was open is gone with it.
"""

from __future__ import annotations

import importlib

import pytest

pytest.importorskip("win32com", reason="the unit's component modules are Windows-only")

from common.canonical import CanonicalResponse
from common.models.statuses import AscomDriverInfoModel, CoversState, CoverStatus


def assert_refused(response, *, expected: str | None = None) -> None:
    assert isinstance(response, CanonicalResponse), f"expected a CanonicalResponse, got {type(response).__name__}"
    assert response.errors, f"expected errors, got {response!r}"
    if expected:
        assert any(expected in err for err in response.errors), f"{expected!r} not in {response.errors}"


def assert_ok(response) -> None:
    assert isinstance(response, CanonicalResponse), f"expected a CanonicalResponse, got {type(response).__name__}"
    assert not response.errors, f"expected success, got errors={response.errors}"


# --------------------------------------------------------------------------- focuser


def _unpowered_focuser():
    from focuser import Focuser

    class _Focuser(Focuser):
        def is_on(self):
            return False

        @property
        def connected(self):
            return False

    return object.__new__(_Focuser)


def test_focuser_goto_position_refuses_when_not_powered():
    assert_refused(_unpowered_focuser().goto_position(1000), expected="not-powered or not-connected")


def test_focuser_set_position_endpoint_refuses_instead_of_reporting_ok():
    """The refusal lives in the ``position`` setter, which cannot return anything; the
    endpoint used to answer Ok while nothing moved."""
    assert_refused(_unpowered_focuser().endpoint_set_position("1000"))


def test_focuser_set_position_names_what_it_rejected():
    """A non-numeric position is refused as a position, not as a ``ValueError``.

    The annotation is ``int | str``, so pydantic passes any string through and the ``int()``
    underneath is what decides. Before the guard it raised, and the registration wrapper
    reported the refusal as ``ValueError: invalid literal for int()`` -- naming the exception
    type rather than the input. Matches ``stage.move_absolute``, the sibling absolute move.
    """
    assert_refused(_unpowered_focuser().endpoint_set_position("kaboom"), expected="'kaboom' is not a position")


# ---------------------------------------------------------------------------- covers


def _disconnected_covers():
    from covers import Covers

    class _Covers(Covers):
        powered_off = False

        @property
        def connected(self):
            return False

        def power_off(self):
            self.powered_off = True

    return object.__new__(_Covers)


@pytest.mark.parametrize("verb", ["open", "close"])
def test_covers_motion_refuses_when_disconnected(verb):
    """Both used a bare ``return`` -- over HTTP an empty body that reads as success."""
    assert_refused(getattr(_disconnected_covers(), verb)(), expected="not connected")


def test_covers_shutdown_succeeds_when_disconnected():
    """Powering off IS the shutdown for a disconnected cover, so this one is not a refusal."""
    covers = _disconnected_covers()
    assert_ok(covers.shutdown())
    assert covers.powered_off


# ------------------------------------------------------------------- imager backends


def _zwo(connected: bool, exposing: bool = False):
    zwo = importlib.import_module("zwo")

    class _Parent:
        def is_active(self, _):
            return exposing

    backend = object.__new__(zwo.ZWOImager)
    backend._connected = connected
    backend.errors = []
    backend.cam_id = 0
    backend.parent_imager = _Parent()
    return backend


@pytest.mark.parametrize("verb", ["abort", "stop_exposure", "abort_exposure"])
def test_zwo_verbs_refuse_when_not_connected(verb):
    assert_refused(getattr(_zwo(connected=False), verb)(), expected="not connected")


def test_zwo_abort_refuses_when_not_exposing():
    assert_refused(_zwo(connected=True, exposing=False).abort(), expected="not exposing")


def test_ascom_abort_exposure_refuses_when_not_connected():
    ascom_module = importlib.import_module("imagers.ascom")

    class _Ascom(ascom_module.ASCOMImager):
        @property
        def connected(self):
            return False

    backend = object.__new__(_Ascom)
    backend.errors = []
    backend.parent_imager = None
    assert_refused(backend.abort_exposure(), expected="not connected")


def test_phd2_abort_returns_an_envelope():
    from phd2.phd2 import PHD2Connector

    assert_ok(object.__new__(PHD2Connector).abort())


# ------------------------------------------------- the startup / shutdown chain (#73)
#
# Before #73 all of these returned None: ASCOM's shutdown fell off the end, ZWO's pair
# delegated through super() to `Component`'s abstract (empty) stub, and PHD2's startup
# was a bare `pass`. "The imager started" and "the imager did nothing" were therefore
# the same answer on the wire, which is what kept `-> CanonicalResponse | None` on the
# four wrapper methods.


def test_ascom_shutdown_refuses_when_not_connected():
    ascom_module = importlib.import_module("imagers.ascom")

    class _Ascom(ascom_module.ASCOMImager):
        @property
        def connected(self):
            return False

    backend = object.__new__(_Ascom)
    backend.errors = []
    backend.parent_imager = None
    assert_refused(backend.shutdown(), expected="not connected")
    assert backend._was_shut_down, "a refused shutdown still records that it was shut down"


def test_zwo_startup_returns_an_envelope():
    """`startup` used to `return super().startup()` -- `Component`'s abstract method,
    with an empty body -- so it did nothing and returned None.

    Only `startup` is covered here. `shutdown` brackets itself in
    `start_activity`/`end_activity`, which reach the notification path and therefore
    `load_local_config()`, so exercising it needs a fake `Config` (via `MAST_CONFIG`)
    rather than an `object.__new__` stand-in. That fixture is #52's Phase 1; until it
    exists, ZWO's shutdown envelope is covered by the hardware pass rather than here.
    """
    zwo = importlib.import_module("zwo")

    class _Zwo(zwo.ZWOImager):
        def set_control(self, *_args, **_kwargs):
            """Stubbed: the real one talks to the ASI SDK."""

    backend = object.__new__(_Zwo)
    backend.cam_id = 0
    backend.errors = []
    assert_ok(backend.startup())


def test_phd2_startup_returns_ok_because_init_already_started_it():
    """Not a stub reporting false success: `PHD2Connector.__init__` launches phd2.exe
    and connects the equipment, so by the time this runs there is nothing left to do
    and `Ok` is the honest answer. It must not repeat that work -- doing so launches a
    second phd2.exe (#84)."""
    from phd2.phd2 import PHD2Connector

    assert_ok(object.__new__(PHD2Connector).startup())


# ------------------------------------------------------------------ wire shape (HTTP)


def _cover_status() -> CoverStatus:
    return CoverStatus(
        ascom=AscomDriverInfoModel(name="stub", description="stub", version="0", connected=True),
        state=CoversState.Closed,
        state_verbal="Closed",
        date="2026-08-02",
    )


@pytest.fixture
def client():
    from fastapi import APIRouter, FastAPI
    from fastapi.testclient import TestClient

    from common.endpoints import add_api_route, register_component_endpoints
    from covers import Covers

    status = _cover_status()

    class _StatusCovers(Covers):
        def status(self):
            return status

    class _RefusingCovers(Covers):
        @property
        def connected(self):
            return False

    # Registered through common.endpoints.add_api_route, not FastAPI's own: since #34 stage 3
    # the envelope is applied at REGISTRATION, so a route wired up directly would bypass the
    # very thing this test asserts. That is also what tests/contract's
    # test_no_route_bypasses_the_declaring_helper enforces for src/.
    router = APIRouter()
    # /covers/status comes from the generator now (#40), which is how a component gets it.
    register_component_endpoints(router, object.__new__(_StatusCovers), "/covers")
    add_api_route(router, "/covers/open", endpoint=object.__new__(_RefusingCovers).open, methods=["PUT"])
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_component_status_is_enveloped_on_the_wire(client):
    """Registration wraps; ``status()`` keeps returning its bare typed model.

    The wrapping happens in the registration helper, and for the interface verbs the route
    itself comes from the generator (#40), so this asserts the wire shape through the real
    path both are on.
    """
    body = client.get("/covers/status").json()

    assert body["api_version"] == "1.0"
    assert body["value"]["state_verbal"] == "Closed"
    assert body["value"]["type"] == "basic"  # the typed model, intact inside the envelope


def test_refusal_reaches_the_wire_as_errors(client):
    body = client.put("/covers/open").json()

    assert body["api_version"] == "1.0"
    assert body["value"] is None
    assert body["errors"] == ["covers: not connected"]
