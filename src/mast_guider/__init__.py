"""In-house guiding (clean-room from `plans/inhouse_guiding_design.md`).

`MastGuider` (`guider.py`) is the measurement core: it chooses a fixed set of guide stars from a
reference frame with the unit's own source selection, and measures how far later frames have
drifted from that set, as a single translation plus a per-star scatter health signal.
"""

from .guider import GuideStars, MastGuider, Measurement, NoMeasurementError

__all__ = ["GuideStars", "MastGuider", "Measurement", "NoMeasurementError"]
