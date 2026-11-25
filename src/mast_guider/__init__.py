"""In-house guiding (clean-room from `plans/inhouse_guiding_design.md`).

- `MastGuider` (`guider.py`) -- the measurement core: choose a fixed set of guide stars from a
  reference frame, measure how far later frames have drifted, as a translation + a scatter health
  signal.
- `PixelToSky` (`pixel_to_sky.py`) -- turn that pixel drift at the fibre into a mount move
  (plain RA / Dec arcsec) via the reference frame's plate solution.
- `CorrectionPolicy` (`correction_policy.py`) -- the PHD2 guide algorithm (hysteresis, min-move,
  clamp) as a pure, stateful policy.

`MountCorrector` (`mount_corrector.py`) sends the move to the hardware; it is imported directly
where a mount is in hand (it pulls in `mount`), not re-exported here, so this package stays
importable without hardware.
"""

from .correction_policy import CorrectionPolicy
from .guider import GuideStars, MastGuider, Measurement, NoMeasurementError
from .pixel_to_sky import PixelToSky

__all__ = ["CorrectionPolicy", "GuideStars", "MastGuider", "Measurement", "NoMeasurementError", "PixelToSky"]
