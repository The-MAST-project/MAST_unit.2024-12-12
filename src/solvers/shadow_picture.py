"""A picture of the fold-mirror shadow a solve detected: the frame with the umbra and penumbra tinted.

Written beside a solve's other artifacts when the solver is asked to keep them in a folder (the survey), so that
every detection can be looked at. A `shadowed` solve shows the frame twice, as it was and with the band darkened.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from imaging.mirror_shadow import ShadowModel

from .star_map import MAX_DISPLAY_WIDTH, font

UMBRA_TINT = (204, 121, 167)  # reddish purple
PENUMBRA_TINT = (0, 114, 178)  # blue
CAPTION_HEIGHT = 74
#: width of a panel when two are shown side by side
PAIR_PANEL_WIDTH = 1200


def tint(rgb: np.ndarray, mask: np.ndarray, color: tuple[int, int, int], alpha: float) -> None:
    rgb[mask] = ((1 - alpha) * rgb[mask] + alpha * np.array(color)).astype(np.uint8)


def zscale_display(image: np.ndarray, max_width: int = MAX_DISPLAY_WIDTH) -> tuple[np.ndarray, int]:
    """An 8-bit linear display of `image` between its zscale limits (as ds9's scale zscale), at most `max_width`
    wide, reduced by block *maximum*.

    Two things hid the faint stars of the old picture: the asinh stretch is anchored on the sky and saturates a few
    sigma above it, and the block *mean* divides a point source's peak by the block's area (25 at the usual factor
    of 5). The zscale range (from a subsample, as ds9 samples) shows the faint stars, and the block maximum keeps
    them -- and, as in ds9, the hot pixels.
    """
    from astropy.visualization import ZScaleInterval  # noqa: PLC0415

    height, width = image.shape
    factor = max(1, -(-width // max_width))
    h, w = height // factor * factor, width // factor * factor
    z1, z2 = ZScaleInterval().get_limits(image[::4, ::4])
    small = image[:h, :w].astype(np.float32).reshape(h // factor, factor, w // factor, factor).max(axis=(1, 3))
    scaled = np.clip((small - z1) / max(float(z2 - z1), 1e-6), 0.0, 1.0)
    return (scaled * 255).astype(np.uint8), factor


def _panel(image: np.ndarray, shadow: ShadowModel | None, max_width: int) -> np.ndarray:
    gray, factor = zscale_display(image, max_width)
    rgb = np.stack([gray, gray, gray], axis=-1)
    if shadow is not None and shadow.present:
        ny, nx = image.shape
        # at display resolution: the display pixel (i, j) stands for the full-resolution pixel at its centre
        xs = (np.arange(rgb.shape[1]) * factor + (factor - 1) / 2.0)[None, :]
        ys = (np.arange(rgb.shape[0]) * factor + (factor - 1) / 2.0)[:, None]
        s = -(xs - (nx - 1) / 2.0) * np.sin(shadow.angle) + (ys - (ny - 1) / 2.0) * np.cos(shadow.angle)
        d = np.abs(s - shadow.offset)
        umbra = d <= shadow.umbra_half_width
        tint(rgb, (d <= shadow.penumbra_half_width) & ~umbra, PENUMBRA_TINT, 0.30)
        tint(rgb, umbra, UMBRA_TINT, 0.40)
    return rgb


def caption_lines(shadow: ShadowModel | None, title: str, seconds: float | None = None) -> list[str]:
    if shadow is None or not shadow.present:
        found = "no shadow found"
    else:
        found = (
            f"tilt {shadow.tilt_deg:.1f} deg   centre {shadow.offset:+.0f} px from the frame centre   "
            f"umbra +-{shadow.umbra_half_width:.0f} px   penumbra +-{shadow.penumbra_half_width:.0f} px   "
            f"depth {shadow.depth:.2f}   prominence {shadow.prominence:.1f}"
        )
    return [title, found, "umbra purple, penumbra blue" + (f"   detection {seconds:.1f} s" if seconds is not None else "")]


def write_shadow_picture(
    image: np.ndarray,
    shadow: ShadowModel | None,
    path: Path,
    caption: list[str],
    darkened: np.ndarray | None = None,
) -> None:
    """`image` with `shadow`'s band tinted, as a JPEG at `path`. With `darkened`, that frame is shown beside it
    (untinted: the band should have gone)."""
    if darkened is None:
        rgb = _panel(image, shadow, MAX_DISPLAY_WIDTH)
    else:
        before = _panel(image, shadow, PAIR_PANEL_WIDTH)
        after = _panel(darkened, None, PAIR_PANEL_WIDTH)
        rows = min(before.shape[0], after.shape[0])
        rgb = np.concatenate([before[:rows], np.full((rows, 8, 3), 255, np.uint8), after[:rows]], axis=1)
    canvas = Image.new("RGB", (rgb.shape[1], rgb.shape[0] + CAPTION_HEIGHT), (0, 0, 0))
    canvas.paste(Image.fromarray(rgb), (0, CAPTION_HEIGHT))
    draw = ImageDraw.Draw(canvas)
    caption_font = font(15)
    for row, line in enumerate(caption):
        draw.text((8, 4 + row * 17), line, fill=(235, 235, 235), font=caption_font)
    canvas.save(path, "JPEG", quality=85)
