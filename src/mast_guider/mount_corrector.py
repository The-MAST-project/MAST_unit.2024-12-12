"""Send a guide correction to the mount: a discrete sky offset, then a settle on the commanded
channels.

A thin adapter over the unit's PWI4 client and `Mount` (design §3, §5). `mount_offset` takes sky
coordinates (plain RA / Dec arcsec) and a discrete step is idempotent and stateless -- a step that
fails to complete cannot leave the mount drifting, as a rate could. It mirrors
`solving.solve_and_correct`'s ``DISCRETE_STEP`` path (the proven pattern): offset, then
`Mount.wait_until_settled(OFFSET_STEP)` -- never ``is_moving``, which is a slew detector and does not
see a small offset (see `src/mount.py`). Not pure (it drives hardware), so it is not unit-tested
offline; it is kept deliberately tiny, isolating the discrete-vs-rate choice from the guide loop.
"""

from __future__ import annotations

from mount import SettleMode


class MountCorrector:
    """Apply guide corrections as discrete ``mount_offset`` steps, settling on ra/dec."""

    def __init__(self, pw, mount):
        self.pw = pw  # the PWI4 client (``unit.pw``)
        self.mount = mount  # the unit's ``Mount`` (``unit.mount``)

    def apply(self, ra_plain_arcsec: float, dec_arcsec: float, *, settle: bool = True) -> None:
        """Offset the mount by ``(ra_plain_arcsec, dec_arcsec)`` and, unless ``settle`` is false, wait
        for the discrete step to register and settle on the ra/dec channels."""
        self.pw.mount_offset(ra_add_arcsec=ra_plain_arcsec, dec_add_arcsec=dec_arcsec)
        if settle:
            self.mount.wait_until_settled(SettleMode.OFFSET_STEP)
