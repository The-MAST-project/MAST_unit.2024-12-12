"""Send a guide correction to the mount as a **gradual ramp**: the mount slides onto the new offset at
a bounded rate, then we settle on the commanded channels.

A thin adapter over the unit's PWI4 client and `Mount` (design §3, §5). The correction is a sky offset
(plain RA / Dec arcsec); it is applied with ``ra/dec_add_gradual_offset_arcsec`` at a rate that scales
with the move's size (`_gradual_rate`), so a large delta slides on *smoothly at a capped speed* rather
than jerking the mount in one step -- "spread large deltas, don't apply as-is". This mirrors
`solving.solve_and_correct`'s proven ``GRADUAL_BY_RATE`` path, including its settle:
`Mount.wait_until_settled(OFFSET_GRADUAL, channels=("ra","dec"))`, which waits on the commanded
channels' **signed** ``gradual_offset_progress`` reaching ±1.0 (never ``is_moving``, which cannot see a
ramp the servo follows -- see `src/mount.py`).

Still safe to leave mid-flight: ``*_add_gradual_offset_arcsec`` ramps toward a *bounded* total and
stops there, so a process that dies during the ramp converges to the commanded offset, it does not run
away as a rate could. `apply` returns only once the ramp has settled. Not pure (it drives hardware),
so it is not unit-tested offline; it stays deliberately tiny, isolating the ramp choice from the loop.
"""

from __future__ import annotations

from mount import SettleMode


def _gradual_rate(delta_arcsec: float) -> float:
    """The ramp rate (arcsec/sec) for a move of this size: a bigger move slews faster but is always
    rate-limited (so it is spread over time, not jerked), a small one creeps on smoothly. The tiers
    mirror the ``GRADUAL_BY_RATE`` rule in `solving.solve_and_correct`; the floor keeps a near-zero
    axis from commanding a zero (never-completing) ramp."""
    d = abs(delta_arcsec)
    if d > 10.0:
        return d * 0.2  # ~5 s to cover the move, whatever its size
    if d > 1.0:
        return 2.0
    return 0.2


class MountCorrector:
    """Apply guide corrections as gradual ``mount_offset`` ramps, settling on ra/dec."""

    def __init__(self, pw, mount):
        self.pw = pw  # the PWI4 client (``unit.pw``)
        self.mount = mount  # the unit's ``Mount`` (``unit.mount``)

    def apply(self, ra_plain_arcsec: float, dec_arcsec: float, *, settle: bool = True) -> None:
        """Ramp the mount by ``(ra_plain_arcsec, dec_arcsec)`` at a size-scaled rate and, unless
        ``settle`` is false, wait for both ramps to complete."""
        self.pw.mount_offset(
            ra_add_gradual_offset_arcsec=ra_plain_arcsec,
            ra_gradual_offset_rate=_gradual_rate(ra_plain_arcsec),
            dec_add_gradual_offset_arcsec=dec_arcsec,
            dec_gradual_offset_rate=_gradual_rate(dec_arcsec),
        )
        if settle:
            self.mount.wait_until_settled(SettleMode.OFFSET_GRADUAL, channels=("ra", "dec"))
