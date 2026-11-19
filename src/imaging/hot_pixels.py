"""A camera's hot pixels: found by persistence across frames, filled before the frame is searched for stars.

A hot pixel is a property of the sensor, so it sits at the same (x, y) in every frame of one camera, whatever the
sky. That is how it is told from everything else that looks like a spike in a single frame: noise peaks come and
go, and a star moves with the field. The map is built from many frames of one camera (`find_hot_pixels`), saved on
the share in the unit's folder (`MAST/<unit>/Calibration/hot_pixels.json`), and applied by filling each masked pixel
with the median of its neighbours (`HotPixels.fill`) before detection -- so that a hot pixel inside a star's window
no longer pulls its centroid, and none is mistaken for a source.

What a hot pixel is, here: a pixel that is *isolated and far above its neighbourhood*, in most of the frames.

- far above: its excess over the local sky exceeds `threshold_sigma` times the frame's robust noise;
- isolated: its brightest neighbour is no more than `isolation` of its own excess above the sky. A star's
  peak pixel is high, but so are its neighbours; a bright star that sits at the same pixel in every frame
  (a static field) is therefore not mistaken for one. (The reference is the sky, not the neighbourhood's
  median: the 3x3 median of a compact star's peak is the star's own flux, and made it look isolated.)
- persistent: so in at least `min_fraction` of the frames. With the frames of one camera taken of different
  sky, nothing else qualifies.

The amplitude of a hot pixel follows the exposure and the sensor's temperature, so a map built from long, warm
frames lists more pixels than one from short, cold ones; the frames' exposure and temperature are kept in it.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
from astropy.io import fits
from scipy import ndimage as ndi

from common.mast_logging import get_logger

logger = get_logger(__name__)

#: where, in a unit's folder on the share, the map lives
CALIBRATION_FOLDER = "Calibration"
FILE_NAME = "hot_pixels.json"
VERSION = 1

#: a pixel is hot if it is this many robust sigmas above the local sky...
DEFAULT_THRESHOLD_SIGMA = 6.0
#: ...in at least this fraction of the frames...
DEFAULT_MIN_FRACTION = 0.8
#: ...and its brightest neighbour is no more than this fraction of its own excess above the sky
DEFAULT_ISOLATION = 0.3
#: fewer frames than this cannot tell a persistent pixel from a lucky coincidence
MIN_FRAMES = 5

_NEIGHBOURS = np.ones((3, 3), dtype=bool)
_NEIGHBOURS[1, 1] = False
#: the local sky is a median on a copy decimated by this factor (a hot pixel is sampled 1 time in 9 and cannot
#: move it), over a square of this many decimated pixels: 15 x 15 full-resolution pixels at the defaults
_SKY_STEP = 3
_SKY_BOX = 5


@dataclass
class HotPixels:
    """The hot pixels of one camera: 0-based (x, y) in the full, unbinned frame."""

    x: np.ndarray
    y: np.ndarray
    #: the pixel's excess over its neighbourhood, ADU (the median over the frames it was found in)
    excess: np.ndarray
    #: in how many of the frames it was found
    frames: np.ndarray
    image_shape: tuple[int, int]  # (ny, nx) of the frames the map was built from
    meta: dict[str, Any] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.x)

    def matches(self, image_shape: tuple[int, int]) -> bool:
        """Whether this map may be used for a frame of `image_shape`: the sensor's pixels are the same ones."""
        return tuple(self.image_shape) == tuple(image_shape)

    # -- applying ------------------------------------------------------------------------------------------

    def fill(self, data: np.ndarray, origin: tuple[int, int] = (0, 0), window: int = 5) -> np.ndarray:
        """`data` with each hot pixel replaced by the median of its neighbours that are not themselves hot.

        `data` may be a crop of the full frame whose top-left pixel is `origin` = (x, y) in it: the map's
        pixels outside the crop are ignored. A copy is returned (the input is left alone), of the same dtype.
        """
        return fill_hot_pixels(data, self.x - origin[0], self.y - origin[1], window)

    # -- storing -------------------------------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": VERSION,
            **self.meta,
            "image_shape": [int(self.image_shape[0]), int(self.image_shape[1])],
            "n_pixels": len(self),
            "x": self.x.astype(int).tolist(),
            "y": self.y.astype(int).tolist(),
            "excess": [round(float(v), 1) for v in self.excess],
            "frames": self.frames.astype(int).tolist(),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> HotPixels:
        if d.get("version") != VERSION:
            raise ValueError(f"hot pixel map of version {d.get('version')!r}, expected {VERSION}")
        meta = {k: v for k, v in d.items() if k not in ("x", "y", "excess", "frames", "image_shape", "n_pixels", "version")}
        return cls(
            x=np.asarray(d["x"], dtype=int),
            y=np.asarray(d["y"], dtype=int),
            excess=np.asarray(d["excess"], dtype=float),
            frames=np.asarray(d["frames"], dtype=int),
            image_shape=(int(d["image_shape"][0]), int(d["image_shape"][1])),
            meta=meta,
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=1) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> HotPixels:
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def calibration_path(unit_folder: Path | str) -> Path:
    """`<unit folder on the share>/Calibration/hot_pixels.json`."""
    return Path(unit_folder) / CALIBRATION_FOLDER / FILE_NAME


def fill_hot_pixels(data: np.ndarray, x: np.ndarray, y: np.ndarray, window: int = 5) -> np.ndarray:
    """A copy of `data` with the pixels (x, y) -- 0-based, in `data` -- replaced by the median of their neighbours.

    The neighbours are those in a `window` x `window` square that are not themselves in the list, so a cluster
    of hot pixels is filled from the sky around it. A pixel with no usable neighbour (a list that covers its whole
    window) is left alone. Pixels outside `data` are ignored.
    """
    out = data.copy()
    ny, nx = data.shape
    x = np.asarray(x, dtype=int)
    y = np.asarray(y, dtype=int)
    inside = (x >= 0) & (x < nx) & (y >= 0) & (y < ny)
    x, y = x[inside], y[inside]
    if x.size == 0:
        return out
    half = window // 2
    offsets = np.arange(-half, half + 1)
    dy, dx = np.meshgrid(offsets, offsets, indexing="ij")
    ny_idx = np.clip(y[:, None] + dy.ravel()[None, :], 0, ny - 1)
    nx_idx = np.clip(x[:, None] + dx.ravel()[None, :], 0, nx - 1)
    values = data[ny_idx, nx_idx].astype(float)
    hot_keys = y.astype(np.int64) * nx + x
    neighbour_keys = ny_idx.astype(np.int64) * nx + nx_idx
    values[np.isin(neighbour_keys, hot_keys)] = np.nan  # a hot neighbour (and the pixel itself) is no sky
    with np.errstate(all="ignore"):
        fill = np.nanmedian(values, axis=1)
    usable = np.isfinite(fill)
    value = np.rint(fill[usable]) if np.issubdtype(data.dtype, np.integer) else fill[usable]
    out[y[usable], x[usable]] = value.astype(data.dtype)
    return out


# -- finding ---------------------------------------------------------------------------------------------------


@dataclass
class _FrameFlags:
    index: np.ndarray  # flat index of each pixel flagged in this frame
    excess: np.ndarray  # its excess over the local sky
    sigma: float  # the frame's robust noise


def _local_sky(d: np.ndarray) -> np.ndarray:
    """The sky under every pixel: a median on a decimated copy, repeated back up to full size."""
    small = ndi.median_filter(d[::_SKY_STEP, ::_SKY_STEP], size=_SKY_BOX, mode="nearest")
    sky = np.repeat(np.repeat(small, _SKY_STEP, axis=0), _SKY_STEP, axis=1)
    return sky[: d.shape[0], : d.shape[1]]


def _flag_frame(data: np.ndarray, threshold_sigma: float, isolation: float) -> _FrameFlags:
    """The isolated spikes of one frame: pixels far above the sky whose brightest neighbour is not."""
    d = np.asarray(data, dtype=np.float32)
    sky = _local_sky(d)
    r = d - sky
    sigma = 1.4826 * float(np.median(np.abs(r - np.median(r))))
    sigma = max(sigma, 1e-6)
    brightest_neighbour = ndi.maximum_filter(d, footprint=_NEIGHBOURS)
    spike = (r > threshold_sigma * sigma) & ((brightest_neighbour - sky) < isolation * r)
    index = np.flatnonzero(spike)
    return _FrameFlags(index=index, excess=r.ravel()[index].astype(float), sigma=sigma)


def find_hot_pixels(
    frames: Iterable[np.ndarray | Path | str],
    threshold_sigma: float = DEFAULT_THRESHOLD_SIGMA,
    min_fraction: float = DEFAULT_MIN_FRACTION,
    isolation: float = DEFAULT_ISOLATION,
    min_frames: int = MIN_FRAMES,
) -> HotPixels:
    """The pixels that are isolated spikes in at least `min_fraction` of `frames` (arrays or FITS paths).

    The frames must be of one camera and one frame size. Each is read in turn and only its flagged pixels are
    kept, so a map can be built from many full frames without holding them.
    """
    flags: list[_FrameFlags] = []
    shape: tuple[int, int] | None = None
    headers: list[Any] = []
    for frame in frames:
        if isinstance(frame, np.ndarray):
            data, header = frame, None
        else:
            with fits.open(frame) as hdul:
                data, header = np.asarray(hdul[0].data), hdul[0].header  # type: ignore[union-attr]
        if shape is None:
            shape = (int(data.shape[0]), int(data.shape[1]))
        elif tuple(data.shape) != shape:
            raise ValueError(f"frames of different sizes: {tuple(data.shape)} and {shape}")
        flags.append(_flag_frame(data, threshold_sigma, isolation))
        headers.append(header)
        logger.info(f"hot pixels: frame {len(flags)}: {len(flags[-1].index)} isolated spikes (noise {flags[-1].sigma:.0f})")
    n = len(flags)
    if n < min_frames or shape is None:
        raise ValueError(f"{n} frames: at least {min_frames} are needed to tell a hot pixel from a coincidence")

    ny, nx = shape
    needed = math.ceil(min_fraction * n)
    index = np.concatenate([f.index for f in flags]) if flags else np.empty(0, dtype=np.int64)
    excess = np.concatenate([f.excess for f in flags]) if flags else np.empty(0)
    unique, inverse, counts = np.unique(index, return_inverse=True, return_counts=True)
    persistent = counts >= needed
    half = int(np.count_nonzero(counts >= math.ceil(0.5 * n)))
    medians = np.zeros(len(unique))
    for k in np.flatnonzero(persistent):
        medians[k] = float(np.median(excess[inverse == k]))
    keep = np.flatnonzero(persistent)
    order = keep[np.argsort(-medians[keep], kind="stable")]  # brightest first
    chosen = unique[order]
    return HotPixels(
        x=(chosen % nx).astype(int),
        y=(chosen // nx).astype(int),
        excess=medians[order],
        frames=counts[order].astype(int),
        image_shape=(ny, nx),
        meta={
            **_provenance(headers, n, threshold_sigma, min_fraction, isolation),
            # How many pixels are persistent in half the frames, and in how many frames the most persistent one is:
            # many of the first and none of the second means the frames are of different populations (sensor
            # temperature, or a hot pixel set that changed between nights), and no pixel can be called persistent.
            "persistent_in_half": half,
            "max_persistence": int(counts.max()) if counts.size else 0,
        },
    )


def _provenance(headers: list[Any], n: int, threshold_sigma: float, min_fraction: float, isolation: float) -> dict[str, Any]:
    """What the map was built from: the frames' camera, exposure and sensor temperature, and the thresholds."""
    meta: dict[str, Any] = {
        "built": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "n_frames": n,
        "threshold_sigma": threshold_sigma,
        "min_fraction": min_fraction,
        "isolation": isolation,
    }
    present = [h for h in headers if h is not None]
    if present:
        cameras = sorted({str(h["INSTRUME"]) for h in present if "INSTRUME" in h})
        if cameras:
            meta["camera"] = cameras[0] if len(cameras) == 1 else cameras
        for key, name in (("EXPOSURE", "exposure_s"), ("CCD-TEMP", "ccd_temp_c")):
            values = [float(h[key]) for h in present if key in h]
            if values:
                meta[name] = [min(values), max(values)]
    return meta
