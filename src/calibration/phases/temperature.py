"""Mirror / ambient temperature from PWI4.

The thermal focus-seed model wants the **primary mirror** temperature, which
predicts best focus better than ambient (the mirror lags ambient with a long
time constant).  PWI4 does not expose temperatures in ``/status``; they come
from a separate endpoint::

    GET http://localhost:8220/temperatures/pw1000

    temperature.primary=12.340
    temperature.ambient=14.210
    temperature.secondary=-999.000
    temperature.m3=-999.000

``-999`` is PWI4's "no sensor" sentinel and maps to ``None``.

Two rules, both deliberate:

* **Never fabricate a reading.**  ``None`` propagates so the focus-seed model
  falls back to a bare (non-thermal) seed instead of fitting against a constant
  that would look like real data.
* **The timestamp is taken at the call site**, not inside the reader, so the
  calibration record carries ``(temperature, read_time)`` and a stale reading
  can be rejected later.
"""

from __future__ import annotations

import logging

from calibration.logging_context import init_calibration_log

logger = logging.getLogger("mast.unit." + __name__)
init_calibration_log(logger)

NO_SENSOR = -999.0
_ENDPOINT = "/temperatures/pw1000"  # despite the name, the general temperature endpoint


def read_temperatures(pw) -> dict[str, float | None]:
    """All PWI4 temperature sensors as ``{sensor: degC | None}``.

    Returns ``{}`` if the endpoint cannot be reached or parsed -- a missing
    temperature must never fail a calibration run.
    """
    try:
        response = pw.request(_ENDPOINT)
    except Exception as ex:
        logger.warning(f"could not read {_ENDPOINT}: {ex}")
        return {}

    if isinstance(response, bytes):
        response = response.decode("utf-8", errors="replace")

    out: dict[str, float | None] = {}
    for line in str(response).splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key.startswith("temperature."):
            continue
        try:
            number = float(value.strip())
        except ValueError:
            continue
        out[key[len("temperature."):]] = None if number <= NO_SENSOR else number
    return out


def get_mirror_temperature(pw) -> float | None:
    """Primary-mirror temperature in degC, or ``None`` when unavailable."""
    value = read_temperatures(pw).get("primary")
    logger.debug(f"mirror (primary) temperature: {value}")
    return value


def get_ambient_temperature(pw) -> float | None:
    """Ambient temperature in degC, or ``None``.

    Logged alongside the mirror temperature so the open question -- whether
    mirror really does predict focus better than ambient -- can be settled from
    accumulated data rather than assumed.
    """
    return read_temperatures(pw).get("ambient")
