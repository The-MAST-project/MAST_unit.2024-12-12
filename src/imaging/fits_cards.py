"""The unit's FITS cards: the project-standard ones (`common.fits_headers`) plus the unit's own.

Both ways the unit's frames are written use it: `imagers.saving.save_to_fits_file` (ZWO,
ASCOM) sets the cards on the header it builds, and the PHD2 connector stamps them into the
file PHD2 wrote. Never raises: a header card must never cost a frame.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from common.fits_headers import Card, standard_fits_cards
from common.mast_logging import get_logger

if TYPE_CHECKING:
    from unit import Unit  # type: ignore[import-untyped]

logger = get_logger(__name__)


def _read(what: str, read) -> Any:
    try:
        return read()
    except Exception as ex:  # noqa: BLE001 -- a component that cannot answer loses its card, not the frame
        logger.debug(f"fits cards: could not read {what} ({ex})")
        return None


def unit_fits_cards(unit: Unit | None) -> dict[str, Card]:
    """The standard cards, with this unit's plan and sky quality, and its focuser and stage."""
    if unit is None:
        return _read("the standard cards", standard_fits_cards) or {}
    cards = _read(
        "the standard cards",
        lambda: standard_fits_cards(plan_id=unit.plan_owner, sky_quality=unit._latest_sky_quality()),
    )
    cards = cards or {}
    focuser = _read("the focuser position", lambda: unit.focuser.position if unit.focuser else None)
    if focuser is not None:
        cards["FOCUSPOS"] = (focuser, "focuser position")
    stage = _read("the stage position", lambda: unit.stage.position if unit.stage else None)
    if stage is not None:
        cards["STAGEPOS"] = (stage, "FCU stage position")
    return cards
