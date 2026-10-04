"""The calibration slew gate, shared by the phases that point the mount.

Exists because the obvious spelling is wrong in a way that costs a whole night::

    mount.goto_ra_dec_j2000(ra, dec)
    while mount.is_moving:  # <-- hangs forever
        time.sleep(0.5)

``Mount.is_moving`` is axis following-error (``axis0.rms_error > 3.0"`` or
``axis1.rms_error > 1.0"``), recomputed on the mount's timer thread.  Those
thresholds are deliberate -- they mirror PWI4's GUI, which shows an axis green
only below ~1.0" -- but that makes the flag a **tracking-quality** indicator,
not a motion one, and the two answer different questions.

The failure is therefore worse than a plain hang: it is weather-dependent.
Sampled on mast02 over 20s while tracking ON TARGET (``is_slewing=False``),
axis0 rms swung 1.36-3.81" and axis1 1.00-6.40" -- so ``is_moving`` read True in
6 of 7 samples, clearing only in a lull.  A ``while mount.is_moving`` loop thus
passes instantly on a calm night and stalls for minutes, or indefinitely, in
wind.  On 2026-07-21 it held ``/calibrate/focuser`` right after "started
activity Slewing" until a gust-free moment.

Both the focuser and stage phases had this, independently -- hence one helper
rather than two comments.

The correct gate is ``Mount.wait_until_settled(SettleMode.SLEW)``: it waits on
PWI4's authoritative ``is_slewing``, guards the start-of-slew race with a grace
window, then settles on following-distance -- and it has a timeout, so the
worst case is a warning rather than a hung run.

See the repo ``CLAUDE.md`` ("Mount offsetting: settle on the channel you
commanded, never on ``is_moving``") for the full account.
"""

from __future__ import annotations

import logging

from calibration.logging_context import init_calibration_log
from mount import SettleMode

logger = logging.getLogger("mast.unit." + __name__)
init_calibration_log(logger)

#: Following-distance tolerance for a calibration slew.
#:
#: Deliberately far looser than ``wait_until_settled``'s 0.5" default, because
#: on-target following distance is WIND-DRIVEN, not a fixed offset: sampled on
#: mast02 over 20s while tracking on target (is_slewing=False), per-axis
#: ``dist_to_target`` swung between 0.06" and 5.89".  A 0.5" tolerance with
#: ``stable_samples=2`` cannot survive a gust, so the settle would burn its full
#: timeout on any breezy night and only pass in a lull.
#:
#: No calibration phase needs better -- focus wants the star somewhere on a full
#: frame, the shadow solve wants a star field.  This asks "did we arrive?", not
#: "is tracking quiet?", and those are different questions (the second is what
#: PWI4's green/yellow/red RMS indicator answers).
SLEW_SETTLE_TOLERANCE_ARCSEC = 10.0

#: Cap on a calibration slew, so a mount that never settles costs one timeout
#: and a warning instead of hanging the phase forever.
SLEW_SETTLE_TIMEOUT_SECONDS = 180.0


def slew_and_settle(mount, ra_j2000_hours: float, dec_j2000_degs: float, op: str) -> None:
    """Slew to ``(ra, dec)`` and wait for the mount to arrive.

    Returns when the mount has arrived *or* when the settle timed out -- the
    timeout is deliberately NOT an error the caller must handle.  By then the
    slew itself has cleared (that phase has its own timeout and logs its own
    failure); what can still time out is the tight following-distance settle,
    and calibrating at a pointing that is a few arcsec off is fine for every
    phase here.  Making it fatal would abandon a run for a non-problem.
    """
    logger.info(f"{op}: slewing to ra={ra_j2000_hours}h dec={dec_j2000_degs}deg")
    mount.goto_ra_dec_j2000(ra_j2000_hours, dec_j2000_degs)
    settled = mount.wait_until_settled(
        SettleMode.SLEW,
        dist_tolerance_arcsec=SLEW_SETTLE_TOLERANCE_ARCSEC,
        timeout_seconds=SLEW_SETTLE_TIMEOUT_SECONDS,
    )
    if not settled:
        logger.warning(f"{op}: slew did not fully settle -- continuing at this pointing")
