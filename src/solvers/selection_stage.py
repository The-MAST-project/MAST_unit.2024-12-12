"""The source-selection stage of `MastrometryDotNet._solve_in` (plans/mastrometry_source_selection_design.md, Stage 2).

Kept out of `mastrometry.py` so the solver's fragile path changes as little as possible: that file
asks this one three things -- *what mode are we in* (`resolve_settings`), *what do we do to the
cropped frame* (`prepare`), and *what do we say about it afterwards* (`StageResult.record`).

Modes (`solving.source_selection.mode`, MAST_common `SourceSelectionConfig`)
---------------------------------------------------------------------------
``original``     nothing happens; this module is not even consulted beyond `resolve_settings`.
``shadowed``     the fold-mirror shadow is detected and its band (umbra *and* penumbra) darkened
                 with `imaging.mirror_shadow.darken_shadow`; the image is then solved as before.
``selective``    the shadow is detected, `select_sources` chooses the sources on the *undarkened*
                 frame (it needs the shadow's dimming to reject stars inside it), and that list is
                 solved as an xylist. The image solve remains the fallback.

The shadow is looked for on every frame, whatever its phase. It is a band whose place depends on where
the stage puts the mirror: centred when the mirror is in, and with its edge at a vertical edge of the frame
at the `sky` position (a partial band; the production sky ROI, x ~3550 to 6550, overlaps it). It is looked
for in the frame itself, so a frame that has none is found to have none; the mirror state need not be told
(the design's Stage 3 asks for it from the stage position -- this is the image-based reading).

Before anything else the camera's hot pixels, if a map of them is known, are filled with the median of their
neighbours (`imaging/hot_pixels.py`; the map is `<this unit's folder on the share>/Calibration/hot_pixels.json`):
a hot pixel inside a star's window pulls its centroid. The solved image of `shadowed`, and the fallback
image of `selective`, are the filled ones; `original` is not touched.

Where the frame is cropped (the ROI) the selection runs on the cropped array, with the crop's own
centre as the optical centre -- the unit does not yet hold a calibrated optical centre or low-coma
radius, so the design's geometric defaults apply.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, fields, replace
from typing import Any, Literal

import numpy as np
from astropy.io import fits

from common.mast_logging import get_logger
from imaging.hot_pixels import HotPixels, calibration_path
from imaging.mirror_shadow import ShadowModel, darken_shadow, detect_mirror_shadow

from .source_selection import FIT_ORDER, Selection, SelectionParams, select_sources

logger = get_logger(__name__)

Mode = Literal["original", "shadowed", "selective"]
MODES = ("original", "shadowed", "selective")
SEARCH_FROM = ("fibre", "frame")


@dataclass(frozen=True)
class SourceSelectionSettings:
    mode: Mode = "original"
    params: SelectionParams = field(default_factory=SelectionParams)
    exclusion_half_width: float | None = None
    #: 1: `solve-field` reads xylist positions FITS 1-based (verified against image2xy, see survey_solve); 0: 0-based.
    xylist_origin: int = 1
    #: Where the shadow may be. None (the default): anywhere. The shadow is a band whose position depends
    #: on where the stage puts the mirror: centred when the mirror is in (a 4000 px wide band, depth ~0.5),
    #: and with its edge at the right edge of the frame at the `sky` position (a partial band, depth ~0.65).
    #: Restricting the search to the centre, as was tried, rejects every `sky` frame's shadow.
    shadow_max_center_offset: float | None = None
    #: require a trough lower than both shoulders. Off: a partial shadow at the frame's edge has no far
    #: shoulder, and would fail it.
    shadow_require_two_sided: bool = False
    #: the camera's hot pixels, filled before the frame is searched (None: none are known, nothing is filled)
    hot_pixels: HotPixels | None = None
    #: where the search for stars grows out from: ``"fibre"``, the fibre pixel (the point the solve reports), or ``"frame"``,
    #: the middle of the frame it is given, which is what the selection did until 2026-11-23 and what reproduces the numbers
    #: in `docs/mastrometry_stages.md`.
    search_from: Literal["fibre", "frame"] = "fibre"
    #: detect only in a window about the search centre that starts at the low-coma region and grows until there are enough
    #: stars, instead of over the whole frame first (`select_sources(windowed=True)`). Off: the whole frame, as before.
    windowed: bool = False

    def __post_init__(self):
        if self.mode not in MODES:
            raise ValueError(f"source selection mode '{self.mode}' is not one of {MODES}")
        if self.search_from not in SEARCH_FROM:
            raise ValueError(f"source selection search_from '{self.search_from}' is not one of {SEARCH_FROM}")

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
    if cfg is None:
        return SourceSelectionSettings()
    settings = SourceSelectionSettings.from_config(cfg)
    if settings.mode == "original":
        return settings
    return replace(settings, hot_pixels=load_machine_hot_pixels())


def load_machine_hot_pixels() -> HotPixels | None:
    """This unit's hot pixel map, from its own folder on the share; None if there is none or it cannot be read.

    A missing map is the normal state of a camera nobody has built one for, and a map that cannot be read must
    not stop a solve: either way the frame is simply not filled.
    """
    try:
        from common.filer import Filer  # noqa: PLC0415 -- only a MAST machine has a share

        path = calibration_path(Filer().shared.root)
        return HotPixels.load(path) if path.exists() else None
    except Exception as e:  # noqa: BLE001
        logger.warning(f"hot pixel map not loaded: {e!r}")
        return None


@dataclass
class StageResult:
    settings: SourceSelectionSettings
    #: the frame to solve as an image (darkened for `shadowed`); the input otherwise
    data: np.ndarray
    shadow: ShadowModel | None = None
    selection: Selection | None = None
    #: "xylist", "image" or "fallback" (the xylist solve failed, the image was solved)
    solved_from: str = "image"
    #: how many of the camera's hot pixels fell in this frame and were filled
    hot_pixels_filled: int = 0
    #: what each step cost, in seconds: `hot_pixels`, `shadow` (0.0 when the acquisition's cached model was used),
    #: `darken` (shadowed) or `select` (selective)
    timings: dict[str, float] = field(default_factory=dict)

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
            "hot_pixels_filled": self.hot_pixels_filled,
            "timings_s": {k: round(v, 3) for k, v in self.timings.items()},
        }
        if self.selection is not None:
            out.update(
                counts=self.selection.counts(),
                status=self.selection.status,
                radius_used_px=self.selection.radius_used_px,
                search_center_px=[float(v) for v in self.selection.optical_center],
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


def _fill_hot_pixels(
    data: np.ndarray, settings: SourceSelectionSettings, origin: tuple[int, int], frame_shape: tuple[int, int] | None
) -> tuple[np.ndarray, int]:
    """`data` with the camera's hot pixels filled, and how many fell inside it; unchanged if no map fits."""
    hp = settings.hot_pixels
    if hp is None or len(hp) == 0:
        return data, 0
    shape = frame_shape if frame_shape is not None else (int(data.shape[0]) + origin[1], int(data.shape[1]) + origin[0])
    if not hp.matches(shape):
        logger.warning(f"hot pixel map is for {hp.image_shape}, this frame is {shape}: not applied")
        return data, 0
    ny, nx = data.shape
    inside = (hp.x >= origin[0]) & (hp.x < origin[0] + nx) & (hp.y >= origin[1]) & (hp.y < origin[1] + ny)
    return hp.fill(data, origin), int(inside.sum())


def prepare(
    data: np.ndarray,
    header: Any,
    phase: str,
    settings: SourceSelectionSettings,
    origin: tuple[int, int] = (0, 0),
    frame_shape: tuple[int, int] | None = None,
    shadow: ShadowModel | None = None,
    center: tuple[float, float] | None = None,
) -> StageResult:
    """Run the stage on the (cropped, unbinned) frame. `settings.mode` must not be ``original``.

    `center` is the point the search for stars grows out from, in the pixels of `data`: **the fibre pixel**, the point
    whose position the solve reports (CRPIX). The selection starts at the low-coma radius about it and widens until it
    has enough stars. None: the frame's centre, as the selection falls back to.

    `shadow`, if given, is the shadow model for this (cropped) frame, found earlier -- the acquisition's cached
    one -- and is used instead of detecting again.

    `origin` is the crop's top-left pixel (x, y) in the full frame and `frame_shape` the full frame's (ny, nx):
    the hot pixel map is in full-frame pixels.
    """
    assert settings.mode != "original", "prepare() is not called for the original stage"
    result = StageResult(settings=settings, data=data)
    started = time.monotonic()
    data, result.hot_pixels_filled = _fill_hot_pixels(data, settings, origin, frame_shape)
    result.timings["hot_pixels"] = time.monotonic() - started
    result.data = data
    if result.hot_pixels_filled:
        logger.info(f"source selection: {result.hot_pixels_filled} hot pixels filled")

    started = time.monotonic()
    if shadow is not None:
        result.shadow = shadow
        logger.info(f"source selection: shadow present={shadow.present} (cached)")
    else:
        result.shadow = detect_mirror_shadow(
            data,
            max_center_offset=settings.shadow_max_center_offset,
            require_two_sided=settings.shadow_require_two_sided,
        )
        logger.info(f"source selection: shadow present={result.shadow.present}")
    result.timings["shadow"] = 0.0 if shadow is not None else time.monotonic() - started
    shadow_present = result.shadow is not None and result.shadow.present

    if settings.mode == "shadowed":
        started = time.monotonic()
        if shadow_present:
            result.data = _as_dtype(darken_shadow(data, result.shadow, include_penumbra=True), data.dtype)  # type: ignore[arg-type]
            logger.info("source selection: shadow band darkened (umbra and penumbra)")
        result.timings["darken"] = time.monotonic() - started
        return result

    started = time.monotonic()
    result.selection = select_sources(
        data,
        shadow=result.shadow if shadow_present else None,
        optical_center=center,
        params=settings.params,
        exclusion_half_width=settings.exclusion_half_width,
        header=header,
        windowed=settings.windowed,
    )
    result.timings["select"] = time.monotonic() - started
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
