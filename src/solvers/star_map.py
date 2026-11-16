"""The stars one MASTrometry solve extracted and used, as a picture and a table.

astrometry.net finds its own sources in the (downsampled, cropped) frame it is given, matches
some of them to its index, and fits the WCS to the matches. None of that is visible afterwards:
the unit only keeps the solved FITS. This module reads the two files `solve-field` leaves in
the scratch folder and makes them visible.

- **Extracted stars**: the `X`, `Y` table of the `.axy` (or `.xyls`) that `image2xy` writes --
  every source astrometry.net found, matched or not.
- **Used stars**: the `--corr` correspondences file -- the field stars matched to index stars.

Both are in the pixel grid of the frame `solve-field` was given, FITS 1-based, so the picture is
drawn on that frame (`downsampled_*.fits`) and needs no conversion back to the full frame. This
module therefore does not touch the fragile coordinate surface (`solvers/CLAUDE.md`).

How much each used star mattered
--------------------------------
astrometry.net reports no per-star influence on the WCS. `match_weight` in the correspondences
file is the star's weight in the match, not its pull on the fit. What can be said from the
files is computed here, as a *proxy*:

- the **residual** of each used star: its measured position minus its catalogue position
  projected through the solved WCS;
- its **leverage** `h` in a least-squares polynomial fit of that order (astrometry.net's tweak
  default, 2) -- how much the fit is pulled toward the star simply because of where it sits.
  Stars far from the others, and in corners, have high leverage;
- **Cook's distance** -- residual and leverage together;
- the **fibre shift**: how far the reported point (CRPIX, where CRVAL is evaluated -- the fibre on
  the spec path) would move, in arcsec, if the star were left out. For a least-squares fit this
  is exact in closed form, `c_i * e_i / (1 - h_i)` with `c = (X'X)^-1 X'` on the constant term,
  and needs no refit.

The fit that astrometry.net actually makes is not this one (SIP, its own weights and clipping),
so the numbers rank stars; they do not reproduce the solver's arithmetic.
"""

from __future__ import annotations

import json
import warnings
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from astropy.io import fits
from astropy.wcs import WCS
from PIL import Image, ImageDraw, ImageFont

from common.mast_logging import get_logger

logger = get_logger(__name__)

#: astrometry.net's default tweak order; mastrometry.py passes no --tweak-order.
TWEAK_ORDER = 2

#: Orange and sky blue: distinguishable by the common colour-vision deficiencies. The shapes differ
#: as well (a tick against a circle), so the picture does not rest on colour alone.
EXTRACTED_COLOR = (230, 159, 0)
USED_COLOR = (86, 180, 233)
REPORTED_COLOR = (255, 255, 255)

MAX_DISPLAY_WIDTH = 2000
#: The stretch's white point, in noise sigmas above the sky: faint stars show, bright ones saturate.
SKY_WHITE_SIGMAS = 30.0
CAPTION_HEIGHT = 74
MIN_USED_RADIUS = 9
MAX_USED_RADIUS = 30
LABELLED_STARS = 3


@dataclass
class Matched:
    """The correspondences: one row per used star. Pixels are the solved frame's, FITS 1-based."""

    x: np.ndarray
    y: np.ndarray
    ra: np.ndarray
    dec: np.ndarray
    weight: np.ndarray | None


def _table_with(path: Path, required: tuple[str, ...]) -> dict[str, np.ndarray] | None:
    """The first table in `path` that has every `required` column (any case), as {lower name: values}."""
    try:
        with fits.open(path) as hdul:
            for hdu in hdul:
                data = getattr(hdu, "data", None)
                names = getattr(data, "names", None)
                if not names:
                    continue
                by_lower = {name.lower(): name for name in names}
                if all(column in by_lower for column in required):
                    return {lower: np.asarray(data[name], dtype=float) for lower, name in by_lower.items()}
                logger.warning(f"{path.name}: table has columns {list(names)}, wanted {list(required)}")
    except (OSError, ValueError) as e:
        logger.warning(f"cannot read '{path}' ({e!r})")
    return None


def read_extracted(scratch: Path) -> np.ndarray | None:
    """Every source astrometry.net extracted: an (n, 2) array of x, y. None if no list was kept."""
    for pattern in ("*.axy", "*.xyls"):
        for path in sorted(scratch.glob(pattern)):
            table = _table_with(path, ("x", "y"))
            if table is not None:
                return np.column_stack([table["x"], table["y"]])
    return None


def read_matched(corr_path: Path) -> Matched | None:
    """The stars the solution used, from the `--corr` file. None if it is missing or unreadable."""
    if not corr_path.exists():
        return None
    table = _table_with(corr_path, ("field_x", "field_y", "index_ra", "index_dec"))
    if table is None:
        return None
    return Matched(
        x=table["field_x"],
        y=table["field_y"],
        ra=table["index_ra"],
        dec=table["index_dec"],
        weight=table.get("match_weight"),
    )


def residuals(matched: Matched, solved_fits: Path) -> tuple[np.ndarray, np.ndarray, tuple[float, float], float] | None:
    """Per used star, measured minus catalogue-projected position (pixels), through the solved WCS.

    Returns `(rx, ry, crpix, pixel_scale_arcsec)`; None if the solved FITS carries no usable WCS.
    """
    try:
        header = fits.getheader(solved_fits, 0)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            wcs = WCS(header)
            if not wcs.has_celestial:
                return None
            px, py = wcs.all_world2pix(matched.ra, matched.dec, 1)
        crpix = (float(header["CRPIX1"]), float(header["CRPIX2"]))
        scale = float(np.sqrt(abs(np.linalg.det(wcs.pixel_scale_matrix))) * 3600.0)
    except Exception as e:  # noqa: BLE001 -- a WCS that will not project only costs the influence
        logger.warning(f"cannot project the used stars through '{solved_fits.name}' ({e!r})")
        return None
    return matched.x - px, matched.y - py, crpix, scale


def influence(
    x: np.ndarray,
    y: np.ndarray,
    rx: np.ndarray,
    ry: np.ndarray,
    crpix: tuple[float, float],
    pixel_scale_arcsec: float,
    order: int = TWEAK_ORDER,
) -> dict[str, np.ndarray] | None:
    """Leverage, Cook's distance and fibre shift of each used star (see the module docstring).

    `x, y` are the used stars' positions, `rx, ry` their residuals, in pixels. None when there
    are not enough stars for the fit (fewer than the polynomial's terms plus two).
    """
    n = len(x)
    exponents = [(i, j) for total in range(order + 1) for i in range(total + 1) for j in [total - i]]
    p = len(exponents)  # 6 at order 2; the constant term is first
    if n < p + 2:
        return None

    extent = max(float(np.max(np.abs(x - crpix[0]))), float(np.max(np.abs(y - crpix[1]))), 1.0)
    u = (x - crpix[0]) / extent
    v = (y - crpix[1]) / extent
    design = np.column_stack([u**i * v**j for i, j in exponents])
    try:
        normal_inverse = np.linalg.inv(design.T @ design)
    except np.linalg.LinAlgError:
        return None

    leverage = np.einsum("ij,jk,ik->i", design, normal_inverse, design)
    room = np.maximum(1.0 - leverage, 1e-9)  # a star that alone fixes a term has h -> 1
    squared = rx**2 + ry**2
    sigma2 = squared.sum() / (n - p)
    cooks = squared / (p * sigma2) * leverage / room**2 if sigma2 > 0 else np.zeros(n)
    # The constant term's coefficient on each observation: row 0 of (X'X)^-1 X'.
    constant_weight = normal_inverse[0, :] @ design.T
    shift_px = np.hypot(constant_weight * rx, constant_weight * ry) / room
    return {"leverage": leverage, "cooks_distance": cooks, "fibre_shift_arcsec": shift_px * pixel_scale_arcsec}


def _stretch(image: np.ndarray) -> tuple[np.ndarray, int]:
    """An 8-bit asinh stretch of `image`, block-averaged down to at most MAX_DISPLAY_WIDTH wide.

    Returns the stretched array and the integer factor it was reduced by.
    """
    height, width = image.shape
    factor = max(1, -(-width // MAX_DISPLAY_WIDTH))
    h, w = height // factor * factor, width // factor * factor
    small = image[:h, :w].astype(np.float32).reshape(h // factor, factor, w // factor, factor).mean(axis=(1, 3))
    # Anchored on the sky, not on percentiles: stars cover well under 1% of a frame, so a high
    # percentile is just the sky again and the whole background washes out to white.
    sample = small[:: max(1, small.shape[0] // 300), :: max(1, small.shape[1] // 300)]
    sky = float(np.median(sample))
    sigma = max(1.4826 * float(np.median(np.abs(sample - sky))), 1e-6 * max(abs(sky), 1.0))
    scaled = np.clip((small - (sky - sigma)) / (SKY_WHITE_SIGMAS * sigma), 0.0, None)
    stretched = np.arcsinh(scaled * 10.0) / np.arcsinh(10.0)
    return (np.clip(stretched, 0.0, 1.0) * 255).astype(np.uint8), factor


def _font(size: int):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow without a sized default font
        return ImageFont.load_default()


def render(
    image: np.ndarray,
    extracted: np.ndarray | None,
    matched: Matched,
    fibre_shift_arcsec: np.ndarray | None,
    crpix: tuple[float, float] | None,
    caption: list[str],
    out_path: Path,
) -> None:
    """Draw the frame, the extracted stars (orange ticks) and the used ones (blue circles).

    A used star's circle grows with its fibre shift, so the stars the solution leaned on stand out.
    """
    gray, factor = _stretch(image)
    picture = Image.fromarray(gray, mode="L").convert("RGB")
    canvas = Image.new("RGB", (picture.width, picture.height + CAPTION_HEIGHT), (0, 0, 0))
    canvas.paste(picture, (0, CAPTION_HEIGHT))
    draw = ImageDraw.Draw(canvas)

    def to_display(px: float, py: float) -> tuple[float, float]:
        # FITS 1-based pixel centres sit at (p - 0.5) in continuous coordinates.
        return (px - 0.5) / factor, (py - 0.5) / factor + CAPTION_HEIGHT

    if extracted is not None:
        for ex, ey in extracted:
            cx, cy = to_display(ex, ey)
            draw.line([(cx - 5, cy), (cx + 5, cy)], fill=EXTRACTED_COLOR, width=2)
            draw.line([(cx, cy - 5), (cx, cy + 5)], fill=EXTRACTED_COLOR, width=2)

    if fibre_shift_arcsec is not None and len(fibre_shift_arcsec) and float(np.max(fibre_shift_arcsec)) > 0:
        relative = fibre_shift_arcsec / float(np.max(fibre_shift_arcsec))
    else:
        relative = np.full(len(matched.x), 0.3)
    radii = MIN_USED_RADIUS + (MAX_USED_RADIUS - MIN_USED_RADIUS) * relative
    for sx, sy, radius in zip(matched.x, matched.y, radii, strict=True):
        cx, cy = to_display(sx, sy)
        draw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius], outline=USED_COLOR, width=2)

    if fibre_shift_arcsec is not None:
        label_font = _font(14)
        for k in np.argsort(fibre_shift_arcsec)[::-1][:LABELLED_STARS]:
            cx, cy = to_display(matched.x[k], matched.y[k])
            draw.text((cx + radii[k] + 3, cy - 8), f'{fibre_shift_arcsec[k]:.3f}"', fill=USED_COLOR, font=label_font)

    if crpix is not None:
        cx, cy = to_display(*crpix)
        draw.line([(cx - 14, cy), (cx + 14, cy)], fill=REPORTED_COLOR, width=1)
        draw.line([(cx, cy - 14), (cx, cy + 14)], fill=REPORTED_COLOR, width=1)

    caption_font = _font(15)
    for row, line in enumerate(caption):
        draw.text((8, 4 + row * 17), line, fill=(235, 235, 235), font=caption_font)
    canvas.save(out_path, "JPEG", quality=85)


def _finite(values: np.ndarray | None) -> list[float | None] | None:
    if values is None:
        return None
    return [float(v) if np.isfinite(v) else None for v in values]


def write_star_map(
    image_path: Path,
    corr_path: Path,
    solved_fits: Path,
    scratch: Path,
    jpeg_path: Path,
    json_path: Path,
) -> dict | None:
    """Write the annotated JPEG and the per-star JSON of one solve.

    `image_path` is the frame `solve-field` solved (the downsampled, cropped one), `corr_path` the
    `--corr` file, `solved_fits` the `--new-fits` output. Returns a summary (counts and the
    largest fibre shift), or None when there is nothing to draw -- no correspondences file, which
    is the case for a solve that failed. Never raises for a missing or odd input; the caller
    still has to guard against the unexpected.
    """
    matched = read_matched(corr_path)
    if matched is None or len(matched.x) == 0:
        return None
    extracted = read_extracted(scratch)

    with fits.open(image_path) as hdul:
        image = np.asarray(hdul[0].data, dtype=float)  # type: ignore[union-attr]

    projected = residuals(matched, solved_fits)
    shifts = None
    stars: dict[str, list[float | None] | None] = {}
    crpix = None
    scale = None
    rms_arcsec = None
    if projected is not None:
        rx, ry, crpix, scale = projected
        rms_arcsec = float(np.sqrt(np.mean(rx**2 + ry**2)) * scale)
        stars["residual_arcsec"] = _finite(np.hypot(rx, ry) * scale)
        measure = influence(matched.x, matched.y, rx, ry, crpix, scale)
        if measure is not None:
            shifts = measure["fibre_shift_arcsec"]
            stars["leverage"] = _finite(measure["leverage"])
            stars["cooks_distance"] = _finite(measure["cooks_distance"])
            stars["fibre_shift_arcsec"] = _finite(shifts)

    n_extracted = None if extracted is None else len(extracted)
    caption = [
        f"{solved_fits.stem}",
        f"extracted {n_extracted if n_extracted is not None else '?'} (orange +)   "
        f"used {len(matched.x)} (blue o, radius ~ fibre shift if left out)   reported point (white +)",
        (
            f'residual rms {rms_arcsec:.3f}"   largest fibre shift {float(np.max(shifts)):.3f}" (labelled)'
            if shifts is not None and rms_arcsec is not None
            else "influence not computed: too few used stars or no usable WCS"
        ),
    ]
    render(image, extracted, matched, shifts, crpix, caption, jpeg_path)

    record = {
        "frame": image_path.name,
        "pixel_scale_arcsec": scale,
        "reported_pixel": list(crpix) if crpix is not None else None,
        "extracted": n_extracted,
        "used": len(matched.x),
        "residual_rms_arcsec": rms_arcsec,
        "influence_model": (
            f"least-squares polynomial of order {TWEAK_ORDER} about the reported pixel; a proxy, "
            "not astrometry.net's own fit (see solvers/star_map.py)"
        ),
        "used_stars": {
            "x": _finite(matched.x),
            "y": _finite(matched.y),
            "ra_deg": _finite(matched.ra),
            "dec_deg": _finite(matched.dec),
            "match_weight": _finite(matched.weight),
            **stars,
        },
    }
    json_path.write_text(json.dumps(record, indent=1), encoding="utf-8")
    return {
        "extracted": n_extracted,
        "used": len(matched.x),
        "residual_rms_arcsec": rms_arcsec,
        "largest_fibre_shift_arcsec": None if shifts is None else float(np.max(shifts)),
    }
