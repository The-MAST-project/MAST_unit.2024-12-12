"""Build a camera's hot pixel map from frames of that camera, and save it on the share.

A hot pixel is at the same sensor pixel in every frame of one camera, whatever the sky (imaging/hot_pixels.py).
This reads many frames of one unit's camera, keeps the pixels that are isolated spikes in most of them, and writes

    <share>/MAST/<unit>/Calibration/hot_pixels.json

-- the unit's own folder on the operational share, where the solver finds it (`Filer().shared`). An earlier map in
that place is kept beside it as `hot_pixels-<its build time>.json`, not overwritten.

Usage:
    python tools/build_hot_pixels.py PATH [PATH ...] --unit mast01 [options]

    PATH   a FITS file, or a folder searched recursively (solved frames, `*,solver=*`, and the solver's
           `downsampled_*` scratch frames are skipped)

    --unit U              the unit whose camera took the frames (its folder gets the map)
    --share-root DIR      the share's MAST folder (default: this machine's `Filer().share_root`)
    --out FILE            write here instead (nothing is moved aside)
    --threshold-sigma S   how far above the local sky a hot pixel is, in robust sigmas (default 6)
    --min-fraction F      in what fraction of the frames (default 0.8)
    --temp-tolerance C    use only frames whose sensor temperature (CCD-TEMP) is within C degrees of its set point
                          (SET-TEMP): a hot pixel's strength follows the temperature, so warm or uncooled frames
                          hold another population and defeat the persistence test (default 2). Frames with no
                          CCD-TEMP are dropped if any frame has one; if none does, all are kept. 0 turns it off
    --last-nights N       use only the frames of the N most recent nights among those given (default: all). A
                          camera's hot pixels are not fixed over months -- on mast02 the pixels of September are
                          not those of November -- so a map belongs to the period it was built from
    --limit N             use at most N frames, spread over the list rather than the first N
    --dry-run             find them, print what would be written, write nothing

The frames should be of one camera, of different sky (stars that move are never persistent), and of one frame
size; use about 15 to 30, of similar exposure and sensor temperature -- a hot pixel's amplitude follows both.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

import numpy as np
from astropy.io import fits

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from imaging.hot_pixels import (  # noqa: E402
    DEFAULT_ISOLATION,
    DEFAULT_MIN_FRACTION,
    DEFAULT_THRESHOLD_SIGMA,
    HotPixels,
    calibration_path,
    find_hot_pixels,
)


def find_frames(paths: list[Path], limit: int | None) -> list[Path]:
    """The FITS frames named by `paths`, a folder's recursively; at most `limit`, evenly spread."""
    found: list[Path] = []
    for path in paths:
        candidates = sorted(path.rglob("*.fits")) if path.is_dir() else [path]
        found += [p for p in candidates if ",solver=" not in p.name and not p.name.startswith("downsampled_")]
    if limit and len(found) > limit:
        found = [found[int(i * len(found) / limit)] for i in range(limit)]
    return found


def same_size(frames: list[Path]) -> tuple[list[Path], tuple[int, int]]:
    """The frames of the most common size, and that size: a map is for one frame size."""
    sizes = {}
    for frame in frames:
        shape = fits.getheader(frame)
        sizes[frame] = (int(shape["NAXIS2"]), int(shape["NAXIS1"]))
    counts: dict[tuple[int, int], int] = {}
    for size in sizes.values():
        counts[size] = counts.get(size, 0) + 1
    best = max(counts, key=lambda s: counts[s])
    return [f for f in frames if sizes[f] == best], best


def at_operating_temperature(frames: list[Path], tolerance: float) -> tuple[list[Path], list[str]]:
    """The frames taken with the sensor at its set point, and a note for each one left out and why.

    A frame is at its set point if |CCD-TEMP - SET-TEMP| <= `tolerance`. If no frame carries a CCD-TEMP the
    camera does not report one and nothing can be judged, so all are kept.
    """
    temperatures = {}
    for frame in frames:
        header = fits.getheader(frame)
        temperatures[frame] = (header.get("CCD-TEMP"), header.get("SET-TEMP"))
    if tolerance <= 0 or all(t is None for t, _ in temperatures.values()):
        return frames, []
    kept, notes = [], []
    for frame in frames:
        actual, target = temperatures[frame]
        if actual is None:
            notes.append(f"{frame.name}: no CCD-TEMP")
        elif target is not None and abs(float(actual) - float(target)) > tolerance:
            notes.append(f"{frame.name}: CCD-TEMP {float(actual):.1f}, set point {float(target):.1f}")
        else:
            kept.append(frame)
    return kept, notes


def night_of(frame: Path) -> str:
    """The observing night of a frame: the date of its DATE-OBS (UTC) less 12 hours."""
    from datetime import datetime, timedelta  # noqa: PLC0415

    stamp = str(fits.getheader(frame).get("DATE-OBS", ""))
    try:
        return (datetime.fromisoformat(stamp) - timedelta(hours=12)).strftime("%Y-%m-%d")
    except ValueError:
        return "unknown"


def last_nights(frames: list[Path], count: int) -> list[Path]:
    """The frames of the `count` most recent nights among `frames`."""
    nights = {frame: night_of(frame) for frame in frames}
    recent = sorted({n for n in nights.values() if n != "unknown"})[-count:]
    return [f for f in frames if nights[f] in recent]


def share_root(given: str | None) -> Path:
    if given:
        return Path(given)
    from common.filer import Filer  # noqa: PLC0415 -- this machine's view of the share

    root = Filer().share_root
    if root is None:
        raise SystemExit("no share root on this machine: give --share-root")
    return Path(root.root)


def keep_earlier(destination: Path) -> Path | None:
    """Move an existing map aside as `hot_pixels-<its build time>.json`; the path it went to, or None."""
    if not destination.exists():
        return None
    try:
        built = str(HotPixels.load(destination).meta.get("built", "")).replace(":", "").replace("-", "")
    except (OSError, ValueError):
        built = ""
    aside = destination.with_name(f"{destination.stem}-{built or int(destination.stat().st_mtime)}{destination.suffix}")
    shutil.move(str(destination), str(aside))
    return aside


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--unit")
    parser.add_argument("--share-root")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--threshold-sigma", type=float, default=DEFAULT_THRESHOLD_SIGMA)
    parser.add_argument("--min-fraction", type=float, default=DEFAULT_MIN_FRACTION)
    parser.add_argument("--temp-tolerance", type=float, default=2.0)
    parser.add_argument("--last-nights", type=int)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if not args.out and not args.unit:
        print("give --unit (whose Calibration folder gets the map) or --out", file=sys.stderr)
        return 2

    frames = find_frames(args.paths, args.limit)
    if not frames:
        print("no frames found", file=sys.stderr)
        return 1
    frames, shape = same_size(frames)
    if args.last_nights:
        frames = last_nights(frames, args.last_nights)
    frames, left_out = at_operating_temperature(frames, args.temp_tolerance)
    print(
        f"{len(frames)} frames of {shape[1]} x {shape[0]}"
        + (f", {len(left_out)} left out (not at the set temperature)" if left_out else "")
    )
    for note in left_out:
        print(f"  left out: {note}")
    if not frames:
        print("no frames at the sensor's set temperature", file=sys.stderr)
        return 1
    found = find_hot_pixels(frames, args.threshold_sigma, args.min_fraction, DEFAULT_ISOLATION)
    if args.unit:
        found.meta["unit"] = args.unit
    found.meta["source_frames"] = [f.name for f in frames]

    print(
        f"{len(found)} hot pixels (isolated, > {args.threshold_sigma} sigma over the sky, "
        f"in >= {args.min_fraction:.0%} of the frames)"
    )
    if found.meta["persistent_in_half"] > 4 * max(len(found), 1) and found.meta["persistent_in_half"] > 20:
        print(
            f"WARNING: {found.meta['persistent_in_half']} pixels persist in half the frames but only {len(found)} in "
            f"{args.min_fraction:.0%} (the most persistent is in {found.meta['max_persistence']} of {len(frames)}): the "
            "frames look like different populations -- use --last-nights, or frames from one period and temperature"
        )
    if len(found):
        print(
            f"  excess: median {np.median(found.excess):.0f}, min {found.excess.min():.0f}, max {found.excess.max():.0f} ADU"
        )
        print(
            "  brightest:",
            ", ".join(f"({x},{y}) +{e:.0f}" for x, y, e in zip(found.x[:8], found.y[:8], found.excess[:8], strict=True)),
        )
    destination = args.out or calibration_path(share_root(args.share_root) / args.unit)
    if args.dry_run:
        print(f"dry run: would write {destination}")
        return 0
    aside = keep_earlier(destination) if not args.out else None
    found.save(destination)
    print(f"wrote {destination}" + (f" (the earlier map is now {aside.name})" if aside else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
