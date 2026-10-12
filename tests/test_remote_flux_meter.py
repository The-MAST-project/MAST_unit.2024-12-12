"""The flux meter over HTTP, against a mock of the service on mast-ns-spec.

No network: every request is answered by an `httpx.MockTransport`. What is under test is the
client's half of the contract -- that metadata, settings and frames cross the wire intact,
and that every way the service can fail reaches the session as a `FluxMeterError`, which is
the one exception the session turns into a clean failed run.
"""

from __future__ import annotations

import io
import json
from collections.abc import Callable
from types import SimpleNamespace

import httpx
import numpy as np
import pytest
from test_flux_metering import _make_session

import flux_metering.remote as remote
from flux_metering.flux_meter import FluxMeter, FluxMeterError
from flux_metering.remote import FLUX_METER_HOST, FLUX_METER_PORT, RemoteFluxMeter
from flux_metering.session import FluxMeteringSession

HOST = "mast-ns-spec"
PORT = 8123

INFO = {
    "model": "CS165MU",
    "serial_number": "34567",
    "saturation_level": 1023,
    "description": "CS165MU sn=34567 1440x1080 bit_depth=10",
}


def _npy(frame: np.ndarray) -> bytes:
    buffer = io.BytesIO()
    np.save(buffer, frame)
    return buffer.getvalue()


class FakeService:
    """The three endpoints, recording what was asked of them."""

    def __init__(self, frame: np.ndarray | None = None) -> None:
        self.frame = frame if frame is not None else np.arange(12, dtype=np.uint16).reshape(3, 4)
        self.configured: dict | None = None
        self.refusal: str | None = None
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        match (request.method, request.url.path):
            case ("GET", "/info"):
                return httpx.Response(200, json=INFO)
            case ("PUT", "/configure"):
                if self.refusal is not None:
                    return httpx.Response(422, json={"detail": self.refusal})
                self.configured = json.loads(request.content)
                return httpx.Response(200)
            case ("POST", "/expose"):
                return httpx.Response(200, content=_npy(self.frame), headers={"content-type": "application/octet-stream"})
        return httpx.Response(404, json={"detail": "Not Found"})


def _meter(service: Callable[[httpx.Request], httpx.Response]) -> RemoteFluxMeter:
    return RemoteFluxMeter(HOST, PORT, transport=httpx.MockTransport(service))


def _unreachable(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("[Errno 61] Connection refused", request=request)


# ---------------------------------------------------------------------- the client --


def test_it_is_a_flux_meter():
    assert isinstance(_meter(FakeService()), FluxMeter)


def test_info_is_read_on_construction_and_populates_the_metadata():
    service = FakeService()
    meter = _meter(service)

    assert [(r.method, r.url.path) for r in service.requests] == [("GET", "/info")]
    assert meter.model == INFO["model"]
    assert meter.serial_number == INFO["serial_number"]
    assert meter.saturation_level == INFO["saturation_level"]


def test_the_description_says_the_frames_came_over_the_network():
    description = _meter(FakeService()).description

    assert INFO["description"] in description
    assert f"remote@{HOST}:{PORT}" in description


def test_the_settings_reach_the_service_as_integers():
    service = FakeService()
    _meter(service).configure(exposure_us=2_000_000, gain=100, black_level=12)

    assert service.configured == {"exposure_us": 2_000_000, "gain": 100, "black_level": 12}


def test_a_refused_configure_carries_the_cameras_reason():
    service = FakeService()
    service.refusal = "exposure 10 us is outside the camera's range [64, 26843418] us"
    meter = _meter(service)

    with pytest.raises(FluxMeterError, match=r"outside the camera's range \[64, 26843418\]"):
        meter.configure(exposure_us=10, gain=0, black_level=0)


def test_a_non_json_error_still_carries_the_services_text():
    def broken(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/info":
            return httpx.Response(200, json=INFO)
        return httpx.Response(500, text="Internal Server Error")

    with pytest.raises(FluxMeterError, match="Internal Server Error"):
        _meter(broken).configure(exposure_us=1000, gain=0, black_level=0)


@pytest.mark.parametrize("dtype", [np.uint16, np.uint8, np.float32])
def test_a_frame_round_trips_intact(dtype):
    frame = (np.arange(1440 * 1080) % 1023).astype(dtype).reshape(1080, 1440)
    meter = _meter(FakeService(frame))
    meter.configure(exposure_us=1000, gain=0, black_level=0)

    got = meter.expose()

    assert got.dtype == frame.dtype
    assert got.shape == frame.shape
    assert np.array_equal(got, frame)


def test_the_expose_timeout_follows_the_configured_exposure():
    seen: list[dict] = []
    service = FakeService()

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions["timeout"])
        return service(request)

    meter = _meter(recording)
    exposure_us = 30_000_000
    meter.configure(exposure_us=exposure_us, gain=0, black_level=0)
    meter.expose()

    assert seen[-1]["read"] == exposure_us / remote.MICROSECONDS_PER_SECOND + remote.EXPOSE_SLACK_S


def test_exposing_before_configuring_is_refused():
    with pytest.raises(FluxMeterError, match="configure"):
        _meter(FakeService()).expose()


def test_an_unreachable_service_is_a_flux_meter_error():
    with pytest.raises(FluxMeterError, match="Connection refused"):
        _meter(_unreachable)


def test_a_timed_out_exposure_is_a_flux_meter_error():
    service = FakeService()

    def stalls_on_expose(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/expose":
            raise httpx.ReadTimeout("timed out", request=request)
        return service(request)

    meter = _meter(stalls_on_expose)
    meter.configure(exposure_us=1000, gain=0, black_level=0)

    with pytest.raises(FluxMeterError, match="timed out"):
        meter.expose()


def test_a_body_that_is_not_a_frame_is_a_flux_meter_error():
    def html(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/info":
            return httpx.Response(200, json=INFO)
        return httpx.Response(200, text="<html>proxy says hello</html>")

    meter = _meter(html)
    meter.configure(exposure_us=1000, gain=0, black_level=0)

    with pytest.raises(FluxMeterError):
        meter.expose()


def test_env_proxies_are_not_trusted(monkeypatch):
    """Some units carry the Weizmann proxy in their environment, and it answers in-site
    requests with an HTML error page, so a client that honored it would never reach
    mast-ns-spec.

    Asserted on the clients' construction rather than by setting `HTTP_PROXY` and watching
    the request: httpx consults env proxies only when no transport is given, so a test
    behind a `MockTransport` cannot observe them at all."""
    built: list[dict] = []
    _route_clients_to(monkeypatch, FakeService(), built=built)

    meter = RemoteFluxMeter(HOST, PORT)
    meter.configure(exposure_us=1000, gain=0, black_level=0)
    meter.expose()

    assert len(built) == 3
    assert all(kwargs.get("trust_env") is False for kwargs in built)


def test_close_does_not_talk_to_the_service():
    service = FakeService()
    meter = _meter(service)
    before = len(service.requests)

    meter.close()

    assert len(service.requests) == before


# ------------------------------------------------------------- config and session --


def _route_clients_to(
    monkeypatch, handler: Callable[[httpx.Request], httpx.Response], built: list[dict] | None = None
) -> None:
    """Every client the module builds is answered by `handler` rather than the network.
    The arguments each was built with are appended to `built`, before the transport is
    swapped in."""
    real_client = httpx.Client

    def client(**kwargs):
        if built is not None:
            built.append(dict(kwargs))
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(**kwargs)

    monkeypatch.setattr(remote.httpx, "Client", client)


def test_open_meter_builds_a_remote_meter_at_the_spec_host(monkeypatch):
    service = FakeService()
    _route_clients_to(monkeypatch, service)
    session = FluxMeteringSession(SimpleNamespace())  # type: ignore[arg-type]

    session._open_meter()

    assert isinstance(session._meter, RemoteFluxMeter)
    assert {(r.url.host, r.url.port) for r in service.requests} == {(FLUX_METER_HOST, FLUX_METER_PORT)}
    assert service.configured == {
        "exposure_us": session.params.flux_exposure_us,
        "gain": session.params.flux_gain,
        "black_level": session.params.flux_black_level,
    }
    assert all(r.extensions["timeout"]["connect"] == remote.CALL_TIMEOUT_S for r in service.requests)


def test_an_injected_meter_is_used_without_reaching_the_service(tmp_path, monkeypatch):
    def explode(request: httpx.Request) -> httpx.Response:
        raise AssertionError("the service was reached although a meter was injected")

    _route_clients_to(monkeypatch, explode)
    s, _unit, _mount, meter = _make_session(tmp_path)

    assert s.require_can_start() is None
    s._open_meter()
    assert s._meter is meter


def _uninjected(tmp_path):
    s, _unit, _mount, _meter = _make_session(tmp_path)
    s._injected_meter = None
    return s


def test_preflight_passes_when_the_service_answers(tmp_path, monkeypatch):
    _route_clients_to(monkeypatch, FakeService())

    assert _uninjected(tmp_path).require_can_start() is None


def test_preflight_refuses_an_unreachable_service(tmp_path, monkeypatch):
    _route_clients_to(monkeypatch, _unreachable)

    refusal = _uninjected(tmp_path).require_can_start()

    assert refusal is not None
    assert "flux meter" in refusal
    assert "Connection refused" in refusal


def test_preflight_uses_a_short_timeout(tmp_path, monkeypatch):
    seen: list[dict] = []
    service = FakeService()

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions["timeout"])
        return service(request)

    _route_clients_to(monkeypatch, recording)

    assert _uninjected(tmp_path).require_can_start() is None
    assert seen and all(t["connect"] == remote.PREFLIGHT_TIMEOUT_S for t in seen)
