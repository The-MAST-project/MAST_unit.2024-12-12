"""The source-selection stage of `MastrometryDotNet._solve_in` (plans/mastrometry_source_selection_design.md, Stage 2).

Kept out of `mastrometry.py` so the solver's fragile path changes as little as possible: that file
asks this one three things -- *what mode are we in* (`resolve_settings`), *what do we do to the
cropped frame* (`prepare`), and *what do we say about it afterwards* (`StageResult.record`).

Modes (`solving.source_selection.mode`, MAST_common `SourceSelectionConfig`)
---------------------------------------------------------------------------
``off``          nothing happens; this module is not even consulted beyond `resolve_settings`.
``shadow_only``  the fold-mirror shadow is detected and its band (umbra *and* penumbra) darkened
                 with `imaging.mirror_shadow.darken_shadow`; the image is then solved as before.
``full``         the shadow is detected, `select_sources` chooses the sources on the *undarkened*
                 frame (it needs the shadow's dimming to reject stars inside it), and that list is
                 solved as an xylist. The image solve remains the fallback.

The shadow is looked for on every frame but the sky phase's, where the mirror is retracted. It is
looked for in the frame itself, so a mirror-out `spec` frame is found to have none; the mirror state
need not be told (the design's Stage 3 asks for it from the stage position -- this is the
image-based reading, adequate until then).

Where the frame is cropped (the ROI) the selection runs on the cropped array, with the crop's own
centre as the optical centre -- the unit does not yet hold a calibrated optical centre or low-coma
radius, so the design's geometric defaults apply.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any, Literal

import numpy as np
from astropy.io import fits

from common.mast_logging import get_logger
from imaging.mirror_shadow import ShadowModel, darken_shadow, detect_mirror_shadow

from .source_selection import FIT_ORDER, Selection, SelectionParams, select_sources

logger = get_logger(__name__)

Mode = Literal["off", "shadow_only", "full"]
MODES = ("off", "shadow_only", "full")


@dataclass(frozen=True)
class SourceSelectionSettings:
    mode: Mode = "off"
    params: SelectionParams = field(default_factory=SelectionParams)
    exclusion_half_width: float | None = None
    #: 1: `solve-field` reads xylist positions FITS 1-based; 0: 0-based. Unsettled -- see survey_solve.
    xylist_origin: int = 1
    #: the shadow is looked for only this close to the frame centre, pixels. The fold mirror is in the
    #: middle of the beam; an off-centre "shadow" is the frame's edge roll-off. None: anywhere.
    shadow_max_center_offset: float | None = 1200.0
    #: the shadow must be two-sided: it is a valley between two shoulders: the trough lies at least min_depth
    #: below the lower of the profile maxima on either side. A roll-off has a plateau on one side only.
    shadow_require_two_sided: bool = True

    def __post_init__(self):
        if self.mode not in MODES:
            raise ValueError(f"source selection mode '{self.mode}' is not one of {MODES}")

    @classmethod
    def from_config(cls, cfg: Any) -> SourceSelectionSettings:
        """From MAST_common's `SourceSelectionConfig`: its non-None parameters over `SelectionParams`' defaults."""
        known = {f.name for f in fields(SelectionParams)}
        given = {k: v for k, v in cfg.model_dump().items() if k in known and v is not None}
        return cls(
            mode=cfg.mode,
            params=SelectionParams(**given),
            exclusion_half_width=getattr(cfg, "exclusion_half_width", None),
        )


def resolve_settings(unit_conf: Any, override: SourceSelectionSettings | None) -> SourceSelectionSettings:
    """The caller's settings if it gave any (the survey does), else the unit's configuration, else off."""
    if override is not None:
        return override
    cfg = getattr(getattr(unit_conf, "solving", None), "source_selection", None)
    return SourceSelectionSettings.from_config(cfg) if cfg is not None else SourceSelectionSettings()


@dataclass
class StageResult:
    settings: SourceSelectionSettings
    #: the frame to solve as an image (darkened for `shadow_only`); the input otherwise
    data: np.ndarray
    shadow: ShadowModel | None = None
    selection: Selection | None = None
    #: "xylist", "image" or "fallback" (the xylist solve failed, the image was solved)
    solved_from: str = "image"

    @property
    def failed(self) -> str | None:
        """Why the frame must not be solved at all, or None."""
        if self.selection is not None and self.selection.status != "ok":
            return self.selection.status
        return None

    def record(self) -> dict[str, Any]:
        """What the selection did, for `SolvingResult.selection` and the log."""
        out: dict[str, Any] = {
            "mode": self.settings.mode,
            "shadow_present": bool(self.shadow.present) if self.shadow is not None else False,
            "solved_from": self.solved_from,
        }
        if self.selection is not None:
            out.update(
                counts=self.selection.counts(),
                status=self.selection.status,
                radius_used_px=self.selection.radius_used_px,
                fit_order=self.selection.fit_order,
                fwhm_seeing_px=self.selection.fwhm_seeing_px,
            )
        return out


def _as_dtype(image: np.ndarray, dtype: np.dtype) -> np.ndarray:
    """`darken_shadow` returns floats; the frame goes on as it came."""
    if np.issubdtype(dtype, np.integer):
        info = np.iinfo(dtype)
        return np.clip(np.rint(image), info.min, info.max).astype(dtype)
    return image.astype(dtype)


def prepare(data: np.ndarray, header: Any, phase: str, settings: SourceSelectionSettings) -> StageResult:
    """Run the stage on the (cropped, unbinned) frame. `settings.mode` must not be ``off``."""
    assert settings.mode != "off", "prepare() is not called when the selection is off"
    result = StageResult(settings=settings, data=data)

    if phase != "sky":  # the mirror is retracted for sky frames: there is no shadow to look for
        result.shadow = detect_mirror_shadow(
            data,
            max_center_offset=settings.shadow_max_center_offset,
            require_two_sided=settings.shadow_require_two_sided,
        )
        logger.info(f"source selection: shadow present={result.shadow.present}")
    shadow_present = result.shadow is not None and result.shadow.present

    if settings.mode == "shadow_only":
        if shadow_present:
            result.data = _as_dtype(darken_shadow(data, result.shadow, include_penumbra=True), data.dtype)  # type: ignore[arg-type]
            logger.info("source selection: shadow band darkened (umbra and penumbra)")
        return result

    result.selection = select_sources(
        data,
        shadow=result.shadow if shadow_present else None,
        params=settings.params,
        exclusion_half_width=settings.exclusion_half_width,
        header=header,
    )
    result.solved_from = "xylist"
    logger.info(f"source selection: {result.selection.counts()}, status={result.selection.status}")
    return result


def solved_fits_from_wcs(image_path, wcs_path, out_path) -> None:
    """The solved frame of an xylist solve: the image that was not solved, plus the WCS that was.

    An xylist solve has no image for `--new-fits` to annotate, but everything downstream reads the
    solved FITS (the WCS, the star map), so it is made here from the frame solve-field was never given.
    """
    skip = {"SIMPLE", "BITPIX", "EXTEND", "BZERO", "BSCALE", "COMMENT", "HISTORY", ""}
    with fits.open(image_path) as image, fits.open(wcs_path) as wcs:
        header = image[0].header.copy()  # type: ignore[union-attr]
        for card in wcs[0].header.cards:  # type: ignore[union-attr]
            if card.keyword in skip or card.keyword.startswith("NAXIS"):
                continue
            header[card.keyword] = (card.value, card.comment)
        fits.writeto(out_path, image[0].data, header, overwrite=True)  # type: ignore[union-attr]


__all__ = [
    "FIT_ORDER",
    "SourceSelectionSettings",
    "StageResult",
    "prepare",
    "resolve_settings",
    "solved_fits_from_wcs",
]
