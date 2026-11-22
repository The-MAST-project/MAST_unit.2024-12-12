"""Record the sensor temperature an exposure was taken at, into its FITS header.

Backend-neutral: it takes the cooler's status before and after an exposure (``get_cooler_status`` payloads), so any
imager that can report its cooler can stamp its frames with it. It lived in ``phd2/fits_header.py``, which keeps the
PHD2 rationale and re-exports it (`plans/inhouse_guiding_design.md`, Stage 0).

**Bracketed, not mid-exposure.**  Sampling immediately before and immediately after an exposure needs nothing from the
camera while it is exposing: both queries happen while it is idle.  For a few-second exposure on a cooled sensor's thermal
mass the mean of the two is as good as a true mid-point sample, and the pair carries something a single number cannot --
``CCDTDELT``, how far the sensor moved *during* the frame.

Nothing here may raise: a missing or unwritable temperature must never fail an exposure that otherwise succeeded.
"""

from __future__ import annotations

import logging

from common.fits_headers import stamp_fits_file
from common.mast_logging import init_log

logger = logging.getLogger("mast.unit." + __name__)
init_log(logger)


def stamp_cooling(path: str | None, before: dict | None, after: dict | None) -> None:
    """Write bracketed cooler readings into ``path``'s FITS header.

    ``before`` / ``after`` are ``get_cooler_status`` payloads, or ``None`` where
    a read failed.  ``CCD-TEMP`` is the mean of whatever samples arrived -- the
    standard keyword, so ordinary tooling finds it -- with the raw pair kept
    alongside so the average can always be taken apart again.

    Fields are read individually rather than through the ``CoolerStatus`` model:
    every field on that model is required, so a camera reporting a temperature
    but no cooler power would fail validation and we would stamp NOTHING.  For
    best-effort telemetry, tolerance beats an all-or-nothing type.
    """
    if not path:
        return  # nothing was saved, so nothing to stamp
    samples = [s for s in (before, after) if s]
    if not samples:
        logger.debug(f"no cooler reading to stamp into {path}")
        return

    temps = [s["temperature"] for s in samples if s.get("temperature") is not None]
    last = samples[-1]
    cards: dict[str, tuple] = {}
    if temps:
        cards["CCD-TEMP"] = (sum(temps) / len(temps), "[C] sensor temp, mean of pre/post exposure")
    if before and before.get("temperature") is not None:
        cards["CCDTEMP1"] = (before["temperature"], "[C] sensor temp before exposure")
    if after and after.get("temperature") is not None:
        cards["CCDTEMP2"] = (after["temperature"], "[C] sensor temp after exposure")
    if len(temps) == 2:
        # Drift DURING the frame -- the quality figure a single reading hides.
        cards["CCDTDELT"] = (temps[1] - temps[0], "[C] sensor temp drift during exposure")
    if last.get("setpoint") is not None:
        cards["SET-TEMP"] = (last["setpoint"], "[C] cooler set-point")
    if last.get("coolerOn") is not None:
        cards["COOLERON"] = (bool(last["coolerOn"]), "cooler enabled")
    if last.get("power") is not None:
        # 100% while far from setpoint means the sensor is NOT regulated and is
        # tracking ambient -- which invalidates matching this frame to a dark.
        cards["COOLPOWR"] = (last["power"], "[%] cooler power")

    if stamp_fits_file(path, cards):
        logger.debug(f"stamped {len(cards)} cooling cards into {path}")
