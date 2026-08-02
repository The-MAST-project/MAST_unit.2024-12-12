"""Record the sensor temperature an exposure was taken at, into its FITS header.

PHD2 writes the frame and its header records **no temperature at all** -- not
`CCD-TEMP`, not the setpoint, not cooler power -- even though the ASI294MM *Pro*
is cooled.  Without it a frame cannot be matched to a dark, and a dark library
cannot be validated: you cannot tell whether two frames were taken at the same
sensor temperature.  Since PHD2 both writes the file and can report the cooler,
it stamps its own frames -- so every consumer (acquisition, guiding, autofocus,
calibration) gets the keywords, not just whichever one asked for them.

**Bracketed, not mid-exposure.**  The obvious design -- a timer that fires at
t/2 and samples -- rests on an unverified assumption: that PHD2 answers RPC
*while* a capture is in flight.  If its RPC handling is serialised with the
capture loop, that query either returns after the frame (a post-exposure reading
wearing a mid-exposure label -- worse than no reading, because it looks right) or
delays the capture itself.  Sampling immediately before and immediately after
sidesteps the question entirely: both queries happen while the camera is idle.

For a 5s exposure on a cooled sensor's thermal mass the mean of the two is as
good as a true mid-point sample, and the pair carries something a single number
cannot -- ``CCDTDELT``, how far the sensor moved *during* the frame.  That
matters here: measured 2026-07-21, the cooler was at 100% power with the sensor
at 23.9C against a 5.0C setpoint, i.e. not regulating and drifting with ambient.
The delta is what makes that visible per-frame instead of hidden in an average.

Nothing here may raise: a missing or unwritable temperature must never fail an
exposure that otherwise succeeded.
"""

from __future__ import annotations

import logging

from astropy.io import fits

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

    try:
        with fits.open(path, mode="update", memmap=False) as hdul:
            header = hdul[0].header
            for key, (value, comment) in cards.items():
                header[key] = (value, comment)
        logger.debug(f"stamped {len(cards)} cooling cards into {path}")
    except Exception as ex:
        # The frame itself is fine and already on disk; losing the header stamp
        # must not lose the exposure.
        logger.warning(f"could not stamp cooling header into {path}: {ex}")
