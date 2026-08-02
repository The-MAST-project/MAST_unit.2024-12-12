"""Invariant 4 of the endpoint contract: every routed handler returns a CanonicalResponse.

Covers the refusal paths remediated in MAST_unit #47 -- the cases where a handler
previously fell off the end (an HTTP ``null``) or bare-``return``ed (a success-shaped
empty body), so a caller could not tell "I refused" from "it worked".

The whole module is Windows-only: ``focuser``/``covers``/``mount`` import ``win32com`` at
module scope, so it skips on a dev machine the same way the rest of the suite does. No
hardware, Mongo or PWI4 is needed -- components are built with ``object.__new__`` and given
only the state the path under test reads, and the method under test is always the real one.

Until #71 lands, neither imager backend imports on any machine: ``zwo`` asks
``common.interfaces.imager`` for ``ImagerExposure``/``ImagerStatus`` and ``imagers.ascom``
for ``ImagerStatus``, all of which live in ``common.models.statuses``. Those cases
``importorskip`` rather than fail -- they are about the envelope, not about that import.
"""

from __future__ import annotations

import importlib
import platform
import tempfile
from pathlib import Path

import pytest

pytest.importorskip("win32com", reason="the unit's component modules are Windows-only")

from common.canonical import CanonicalResponse  # noqa: E402
from common.models.statuses import AscomDriverInfoModel, CoversState, CoverStatus  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _hermetic_filer():
    """``common.utils`` builds a module-level ``Filer`` at import; keep it off real storage.

    conftest shims this for Darwin only, where the import fails outright. On Windows the
    import succeeds but would reach for the unit's storage roots.
    """
    if platform.system() != "Windows":
        yield
        return

    import common.filer as filer_module

    tmp_root = Path(tempfile.mkdtemp(prefix="mast-envelope-tests-"))
    location = filer_module.Location(None, str(tmp_root))
    original_init = filer_module.Filer.__init__

    def _tmp_init(self, logger=None):
        self.local = location
        self.shared = location
        self.ram = location
        self.tops = {
            filer_module.FilerTop.Local: self.local,
            filer_module.FilerTop.Shared: self.shared,
            filer_module.FilerTop.Ram: self.ram,
        }
        self.logger = logger

    filer_module.Filer.__init__ = _tmp_init
    yield
    filer_module.Filer.__init__ = original_init


def import_backend_or_skip(module_name: str):
    """Import an imager backend, skipping if #71's stale import is still in the tree.

    ``pytest.importorskip`` cannot be used: since pytest 8.2 it deliberately re-raises
    when a module exists but fails internally, rather than masking a real break. The
    skip is narrow and temporary -- #71 removes it, and the envelope assertions below
    then run for both backends.
    """
    try:
        return importlib.import_module(module_name)
    except ImportError as exc:
        pytest.skip(f"{module_name}: stale ImagerExposure/ImagerStatus import, see #71 ({exc})")


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
    zwo = import_backend_or_skip("zwo")

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
    ascom_module = import_backend_or_skip("imagers.ascom")

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
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from covers import Covers

    status = _cover_status()

    class _StatusCovers(Covers):
        def status(self):
            return status

    class _RefusingCovers(Covers):
        @property
        def connected(self):
            return False

    app = FastAPI()
    app.add_api_route("/covers/status", endpoint=object.__new__(_StatusCovers).endpoint_status)
    app.add_api_route("/covers/open", endpoint=object.__new__(_RefusingCovers).endpoint_open, methods=["PUT"])
    return TestClient(app)


def test_component_status_is_enveloped_on_the_wire(client):
    """``endpoint_status`` wraps; ``status()`` keeps returning its bare typed model."""
    body = client.get("/covers/status").json()

    assert body["api_version"] == "1.0"
    assert body["value"]["state_verbal"] == "Closed"
    assert body["value"]["type"] == "basic"  # the typed model, intact inside the envelope


def test_refusal_reaches_the_wire_as_errors(client):
    body = client.put("/covers/open").json()

    assert body["api_version"] == "1.0"
    assert body["value"] is None
    assert body["errors"] == ["not connected"]
