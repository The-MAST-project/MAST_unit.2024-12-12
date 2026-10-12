"""The flux meter as a client of the flux-meter service on the site's spec host.

The Zelux hangs off mast-ns-spec, not off the unit, and a small HTTP service there opens it
at startup and holds it for its lifetime. This is the unit's side of that service: the same
`FluxMeter` the session already drives, with each call made over the network instead of
through the Thorlabs SDK.

The contract is three endpoints and nothing else -- no open, no claim, no token:

- `GET /info` -- `model`, `serial_number`, `saturation_level`, `description`.
- `PUT /configure` -- `exposure_us`, `gain`, `black_level`, as integers. A setting the camera
  will not take comes back as a 4xx whose `detail` is the camera's own refusal.
- `POST /expose` -- one frame, as the bytes of `np.save`.

Every way a call can fail -- refused connection, timeout, a non-2xx, a body that is not what
the endpoint promises -- is raised as `FluxMeterError` carrying the service's own words. That
is the one exception the session turns into a clean failed run, so nothing else may escape.

The host and port are constants here, not configuration: this is a stopgap until the run
itself moves off the unit, and a database entry for it would outlive the arrangement it
describes. The service's `--port` must match `FLUX_METER_PORT`.

`trust_env=False` on every client, as everywhere else in-site in MAST: some units carry the
Weizmann proxy in their environment, and it answers in-site requests with an HTML error
page instead of passing them on.
"""

from __future__ import annotations

import io

import httpx
import numpy as np
from pydantic import BaseModel, ValidationError

from common.mast_logging import get_logger
from flux_metering.flux_meter import FluxMeterError

logger = get_logger(__name__)

#: Where the flux-meter service runs. The camera is attached to the spec host.
FLUX_METER_HOST = "mast-ns-spec"
FLUX_METER_PORT = 8003

#: For `/info` and `/configure`, which do no camera I/O worth waiting for.
CALL_TIMEOUT_S = 10.0

#: For the preflight in `FluxMeteringSession.require_can_start`, which runs on the request
#: path: an unreachable service must be refused promptly, not after the full call timeout.
PREFLIGHT_TIMEOUT_S = 2.0

#: How long to wait for `/expose` beyond the exposure itself. The service waits the exposure
#: plus its own readout slack (`thorcam.POLL_SLACK_MS`, 5 s) before it gives up on a frame,
#: so this must exceed that, and also cover sending ~3 MB of frame over the site network.
EXPOSE_SLACK_S = 10.0

MICROSECONDS_PER_SECOND = 1_000_000

#: A frame is a 2-D array, as `FluxMeter.expose` promises.
FRAME_NDIM = 2

#: The longest stretch of a service's error text carried into a `FluxMeterError`. A FastAPI
#: refusal is one sentence; a proxy's or a crashed server's HTML page is not, and pasting
#: all of it into the run's `last_error` would bury the line that says what went wrong.
MAX_ERROR_TEXT = 500


class FluxMeterInfo(BaseModel):
    """What `GET /info` returns about the camera the service holds."""

    model: str
    serial_number: str
    saturation_level: int
    description: str


class RemoteFluxMeter:
    """The flux meter on the spec host, reached over HTTP.

    `GET /info` is called on construction, so a meter that exists has already proved the
    service reachable and holds the camera's metadata; the properties read that cache and
    never touch the network.

    `transport` is for tests, which answer with an `httpx.MockTransport`; production leaves
    it None and talks to the network.
    """

    def __init__(
        self,
        host: str,
        port: int,
        *,
        timeout_s: float = CALL_TIMEOUT_S,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._base_url = f"http://{host}:{port}"
        self._timeout_s = timeout_s
        self._transport = transport
        self._exposure_us: int | None = None
        self._info = self._read_info()
        logger.info(f"flux meter at {self._base_url}: {self._info.description}")

    @classmethod
    def at_spec_host(cls, *, timeout_s: float = CALL_TIMEOUT_S) -> RemoteFluxMeter:
        """The meter at `FLUX_METER_HOST`:`FLUX_METER_PORT`."""
        return cls(FLUX_METER_HOST, FLUX_METER_PORT, timeout_s=timeout_s)

    # ----------------------------------------------------------------- operation --

    def configure(self, exposure_us: int, gain: int, black_level: int) -> None:
        self._call(
            "PUT",
            "/configure",
            json={"exposure_us": exposure_us, "gain": gain, "black_level": black_level},
        )
        self._exposure_us = exposure_us

    def expose(self) -> np.ndarray:
        """One frame from the service.

        The read timeout is the exposure plus `EXPOSE_SLACK_S`, derived per configure() for
        the same reason `ThorCam` derives its poll timeout: the exposure follows the
        imager's and can be seconds long.
        """
        if self._exposure_us is None:
            raise FluxMeterError("the remote flux meter must be configured before it can expose")

        read_timeout_s = self._exposure_us / MICROSECONDS_PER_SECOND + EXPOSE_SLACK_S
        response = self._call("POST", "/expose", timeout=httpx.Timeout(self._timeout_s, read=read_timeout_s))
        try:
            frame = np.load(io.BytesIO(response.content), allow_pickle=False)
        except ValueError as ex:
            raise FluxMeterError(
                f"{self._base_url}/expose answered with something that is not a frame ({ex}): {self._clipped(response.text)}"
            ) from ex
        if frame.ndim != FRAME_NDIM:
            raise FluxMeterError(f"{self._base_url}/expose returned a {frame.ndim}-D array, not a frame")
        return frame

    def close(self) -> None:
        """Nothing to release: the service opened the camera and keeps it."""

    # ----------------------------------------------------------------- properties --

    @property
    def saturation_level(self) -> int:
        return self._info.saturation_level

    @property
    def description(self) -> str:
        return f"{self._info.description} remote@{self._host}:{self._port}"

    @property
    def model(self) -> str:
        return self._info.model

    @property
    def serial_number(self) -> str:
        return self._info.serial_number

    # ------------------------------------------------------------------- plumbing --

    def _read_info(self) -> FluxMeterInfo:
        response = self._call("GET", "/info")
        try:
            return FluxMeterInfo.model_validate_json(response.content)
        except ValidationError as ex:
            raise FluxMeterError(f"{self._base_url}/info did not describe a camera: {ex}") from ex

    def _call(self, method: str, path: str, **kwargs) -> httpx.Response:
        """One request, with every failure raised as `FluxMeterError`.

        A client per call, not one held open: the calls are seconds apart, and holding none
        is what lets `close()` have nothing to do.
        """
        url = f"{self._base_url}{path}"
        kwargs.setdefault("timeout", self._timeout_s)
        try:
            with httpx.Client(trust_env=False, transport=self._transport) as client:
                response = client.request(method, url, **kwargs)
        except httpx.HTTPError as ex:
            raise FluxMeterError(f"{method} {url} failed: {type(ex).__name__}: {ex}") from ex

        if not response.is_success:
            raise FluxMeterError(f"{method} {url} answered {response.status_code}: {self._reason(response)}")
        return response

    @classmethod
    def _reason(cls, response: httpx.Response) -> str:
        """The service's own words: FastAPI's `detail` when the body is its JSON refusal,
        otherwise the body's text -- a 500 from the server, or a page from something that
        is not the service at all."""
        try:
            return str(response.json()["detail"])
        except (ValueError, KeyError, TypeError):
            return cls._clipped(response.text)

    @staticmethod
    def _clipped(text: str) -> str:
        return text if len(text) <= MAX_ERROR_TEXT else f"{text[:MAX_ERROR_TEXT]}..."
