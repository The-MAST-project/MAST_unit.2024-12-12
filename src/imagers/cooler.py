"""
Whether the guide camera's cooler reached its set point, logged once per cool-down.

``Imager.do_cool_down()`` watches the sensor after the cooler is switched on. It logs a verdict
when the sensor reaches the set point, or when it is still short of it after
``COOLING_SLOW_WARNING_SECONDS``, on one line that a scraper can key on::

    DEGRADED cooler=unreached temp=<C> set-point=<C> power=<%> after=<s>s <what it costs>   (WARNING)
    cooler=regulating temp=<C> set-point=<C> power=<%> after=<s>s                           (INFO)

``cooler=`` appears on every verdict, so the latest one per unit says whether its camera is
still short of the set point; a repaired camera reads as recovered at its next cool-down.
"""

from enum import StrEnum

from common.mast_logging import get_logger
from imagers.usb_link import DEGRADED_TAG

logger = get_logger(__name__)

COOLER_KEY = "cooler"
UNKNOWN = "unknown"


class CoolerState(StrEnum):
    Regulating = "regulating"
    Unreached = "unreached"


def log_cooler_verdict(
    state: CoolerState, temperature: float, set_point: float, power: float | None, elapsed_s: float
) -> None:
    """Log the cool-down's verdict; a set point not reached is logged as degraded."""
    power_text = UNKNOWN if power is None else f"{power:.0f}"
    verdict = (
        f"{COOLER_KEY}={state} temp={temperature:.1f} set-point={set_point:.1f} power={power_text} after={elapsed_s:.0f}s"
    )
    if state is CoolerState.Unreached:
        logger.warning(
            f"{DEGRADED_TAG} {verdict} frames are taken on a sensor warmer than its darks, "
            "with more dark current than the set point would give"
        )
    else:
        logger.info(verdict)
