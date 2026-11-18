"""Solve the filtered list of one frame with `solve-field`, for the survey -- not the unit's solve.

`tools/source_survey.py --solve` hands the sources a `Selection` kept to astrometry.net as an
xylist, with `--corr`, and reads back which of them the solution *used*. That is the second set of
ticks on the survey picture: the list we fed, and what the solver made of it.

It does what Stage 2 of the source-filtering plan would do inside `MastrometryDotNet`, on saved
frames and apart from the unit: the same flags as `_solve_in` (tweak on, `--crpix-center`, a scale
window) with the xylist input, `--width` / `--height`, `--uniformize 0` and the correspondences
kept. Nothing here touches a running unit or the unit's solver.

The coordinate surface (`solvers/CLAUDE.md`)
--------------------------------------------
With `factor` > 1 the xylist is written on the binned grid, through `pixel_grid` -- the same
conversion a Stage 2 solve would use -- and the used stars are mapped back to the array's pixels
with its inverse. With `factor` == 1 the xylist is the array's own pixels, and there is no
conversion at all.

**Two things were assumed; the first is now verified, the second shown by use.**

- that astrometry.net reads xylist positions as FITS 1-based (the centre of the first pixel is
  1.0), as it does for the lists it makes itself (`origin=1`). **Verified 2026-11-18** on a
  traverse frame (mast01, 10 s): astrometry.net's own `image2xy` run on the 2x2 binned image
  put 36 of our 37 selected stars at a median of (-0.018, -0.033) binned px from where
  `to_solve_grid(..., origin=1)` puts them; `origin=0` was out by (+0.98, +0.97) px. So the
  conversion through `pixel_grid` is the solver's own convention. `origin=0` writes them 0-based
  instead, which changes the solved WCS by a constant pixel offset (0.74" at the unit's scale) and
  nothing else: which stars were used, and the residuals, are the same either way.
- that a file named `*.xyls` given as the input is read as a source list (it is: the solves ran).

Both show up in the `-solve.txt` log the survey keeps for each frame, with the full command and
`solve-field`'s own output.
"""

from __future__ import annotations

import math
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from astropy.io import fits

from common.mast_logging import get_logger

from .astrometry_dot_net import win_to_cygwin
from .pixel_grid import grid_to_orig_fits, orig_to_grid_fits
from .star_map import Matched, influence, read_matched, residuals

logger = get_logger(__name__)

#: The cygwin solve-field of the units; elsewhere `solve-field` is expected on the PATH.
CYGWIN_SOLVE_FIELD = "C:/cygwin64/usr/local/astrometry/bin/solve-field"
DEFAULT_PIXEL_SCALE_ARCSEC = 0.2616


#: A constant, so a test can exercise either form of the command on any platform.
ON_WINDOWS = os.name == "nt"


def default_solve_field() -> str:
    return CYGWIN_SOLVE_FIELD if ON_WINDOWS else "solve-field"


# ----- the grid ---------------------------------------------------------------------------------


def to_solve_grid(x: np.ndarray, y: np.ndarray, factor: int, origin: int = 1) -> tuple[np.ndarray, np.ndarray]:
    """Array coordinates (0-based) to the xylist values: FITS 1-based, binned by `factor`, then
    shifted down by one if `origin` is 0."""
    shift = 1 - origin
    gx = np.array([orig_to_grid_fits(v + 1.0, factor) for v in np.atleast_1d(x)], dtype=float) - shift
    gy = np.array([orig_to_grid_fits(v + 1.0, factor) for v in np.atleast_1d(y)], dtype=float) - shift
    return gx, gy


def from_solve_grid(gx: np.ndarray, gy: np.ndarray, factor: int, origin: int = 1) -> tuple[np.ndarray, np.ndarray]:
    """The inverse of :func:`to_solve_grid`."""
    shift = 1 - origin
    x = np.array([grid_to_orig_fits(v + shift, factor) for v in np.atleast_1d(gx)], dtype=float) - 1.0
    y = np.array([grid_to_orig_fits(v + shift, factor) for v in np.atleast_1d(gy)], dtype=float) - 1.0
    return x, y


def grid_size(shape: tuple[int, int], factor: int) -> tuple[int, int]:
    """(width, height) of the binned grid, as `MastrometryDotNet` crops and bins it."""
    ny, nx = shape
    return nx // factor, ny // factor


def write_xylist(path: Path, xylist: np.ndarray, factor: int, origin: int) -> None:
    """Write the (n, 3) array of x, y, flux (brightest first) as an xylist FITS table on the solve grid."""
    gx, gy = to_solve_grid(xylist[:, 0], xylist[:, 1], factor, origin)
    columns = [
        fits.Column(name="X", format="D", array=gx),
        fits.Column(name="Y", format="D", array=gy),
        fits.Column(name="FLUX", format="D", array=xylist[:, 2]),
    ]
    fits.BinTableHDU.from_columns(columns).writeto(path, overwrite=True)


# ----- the command ------------------------------------------------------------------------------


@dataclass
class SolveSettings:
    index_dir: str
    solve_field: str = field(default_factory=default_solve_field)
    pixel_scale_arcsec: float = DEFAULT_PIXEL_SCALE_ARCSEC  # of the array handed to the survey, unbinned
    factor: int = 2  # the binning of the solve grid, as `MastrometryDotNet`
    origin: int = 1
    cpulimit: int = 60
    ra_deg: float | None = None
    dec_deg: float | None = None
    radius_deg: float = 2.0


@dataclass
class SolveOutcome:
    status: str  # "ok", "skipped: ...", "failed: ..."
    seconds: float = 0.0
    command: str = ""
    log_path: Path | None = None
    #: used stars, in the array's pixels (0-based)
    used_x: np.ndarray = field(default_factory=lambda: np.empty(0))
    used_y: np.ndarray = field(default_factory=lambda: np.empty(0))
    weight: np.ndarray | None = None
    fibre_shift_arcsec: np.ndarray | None = None
    residual_rms_arcsec: float | None = None
    scale_arcsec: float | None = None
    crval: tuple[float, float] | None = None
    fed: int = 0
    #: with the unit's solver: "xylist", "image", or "fallback" (the xylist solve failed)
    solved_from: str = ""

    @property
    def used(self) -> int:
        return len(self.used_x)


def build_args(
    settings: SolveSettings, xylist: Path, wcs: Path, corr: Path, work: Path, grid: tuple[int, int], tweak_order: int
) -> list[str]:
    """The `solve-field` arguments: `_solve_in`'s, with an xylist for the input."""

    def p(path: Path | str) -> str:
        return win_to_cygwin(str(path)) if ON_WINDOWS else str(path)

    scale = settings.pixel_scale_arcsec * settings.factor
    args = [
        "--scale-units",
        "arcsecperpix",
        "--scale-low",
        f"{0.9 * scale}",
        "--scale-high",
        f"{1.1 * scale}",
        "--index-dir",
        p(settings.index_dir),
        "--no-plots",
        "--overwrite",
        "--cpulimit",
        str(settings.cpulimit),
        "--solved",
        "none",
        "--match",
        "none",
        "--rdls",
        "none",
        "--corr",
        p(corr),
        "--wcs",
        p(wcs),
        "--new-fits",
        "none",
        "--dir",
        p(work),
        "--temp-dir",
        p(work),
        # the source list, not an image
        "--width",
        str(grid[0]),
        "--height",
        str(grid[1]),
        "--x-column",
        "X",
        "--y-column",
        "Y",
        "--sort-column",
        "FLUX",
        "--uniformize",
        "0",  # the selection has already spread the stars
        "--crpix-center",
        "--tweak-order",
        str(tweak_order),  # tweak is left ON, as in production
    ]
    if settings.ra_deg is not None and settings.dec_deg is not None:
        args += ["--ra", f"{settings.ra_deg}", "--dec", f"{settings.dec_deg}", "--radius", f"{settings.radius_deg}"]
    args.append(p(xylist))
    return args


def _environment() -> dict[str, str]:
    env = os.environ.copy()
    if ON_WINDOWS:  # as `MastrometryDotNet`: cygwin1.dll and lapack, for the sub-tools
        env["PATH"] = r"C:\cygwin64\bin" + os.pathsep + "/usr/lib/lapack" + os.pathsep + env.get("PATH", "")
    return env


def _run(args: list[str], solve_field: str, timeout: float) -> subprocess.CompletedProcess:
    if ON_WINDOWS:  # a cygwin binary, started as the unit starts it
        return subprocess.run(
            " ".join([solve_field, *args]), capture_output=True, shell=True, env=_environment(), check=False, timeout=timeout
        )
    return subprocess.run([solve_field, *args], capture_output=True, env=_environment(), check=False, timeout=timeout)


# ----- the solve --------------------------------------------------------------------------------


def solve_filtered_list(
    xylist: np.ndarray,
    shape: tuple[int, int],
    fit_order: int,
    settings: SolveSettings,
    scratch: Path,
    log_path: Path,
    stem: str,
) -> SolveOutcome:
    """Solve the kept sources of one frame, and read back which of them the solution used.

    `xylist` is `Selection.xylist()`: x, y (the array's 0-based pixels), flux, brightest first.
    `scratch` is a folder this call may fill (it is the survey's own `--out` subfolder); `log_path`
    receives the command and `solve-field`'s output either way. Never raises for a `solve-field` that
    is missing or fails: the outcome says so.
    """
    outcome = SolveOutcome(status="ok", fed=len(xylist), log_path=log_path)
    if len(xylist) == 0:
        outcome.status = "skipped: no sources kept"
        return outcome

    scratch.mkdir(parents=True, exist_ok=True)
    xyls = scratch / f"{stem}.xyls"
    wcs = scratch / f"{stem}.wcs"
    corr = scratch / f"{stem}.corr"
    write_xylist(xyls, xylist, settings.factor, settings.origin)
    args = build_args(settings, xyls, wcs, corr, scratch, grid_size(shape, settings.factor), fit_order)
    outcome.command = " ".join([settings.solve_field, *args])

    resolved = shutil.which(settings.solve_field) or (settings.solve_field if Path(settings.solve_field).exists() else None)
    start = time.monotonic()
    completed: subprocess.CompletedProcess | None = None
    if resolved is None and not ON_WINDOWS:
        outcome.status = f"failed: '{settings.solve_field}' not found"
    else:
        try:
            completed = _run(args, settings.solve_field, timeout=settings.cpulimit * 3 + 60)
        except FileNotFoundError:
            outcome.status = f"failed: '{settings.solve_field}' not found"
        except subprocess.TimeoutExpired:
            outcome.status = "failed: solve-field timed out"
        except OSError as e:
            outcome.status = f"failed: could not start solve-field ({e})"
    outcome.seconds = time.monotonic() - start

    _write_log(log_path, outcome, completed)
    if completed is None:
        return outcome
    if completed.returncode != 0:
        outcome.status = f"failed: solve-field exited {completed.returncode}"
        return outcome

    matched = read_matched(corr)
    if matched is None or len(matched.x) == 0:
        outcome.status = "failed: no match (no usable correspondences file)"
        return outcome
    _read_back(outcome, matched, wcs, settings)
    return outcome


def _write_log(path: Path, outcome: SolveOutcome, completed: subprocess.CompletedProcess | None) -> None:
    lines = ["--- command ---", outcome.command, "", f"--- status ---\n{outcome.status}", ""]
    if completed is not None:
        lines += [
            f"--- returncode ---\n{completed.returncode}",
            "",
            "--- stdout ---",
            completed.stdout.decode(errors="replace"),
            "--- stderr ---",
            completed.stderr.decode(errors="replace"),
        ]
    lines += ["", f"--- timing ---\nelapsed: {outcome.seconds:.2f} seconds"]
    path.write_text("\n".join(lines), encoding="utf-8")


def _read_back(outcome: SolveOutcome, matched: Matched, wcs: Path, settings: SolveSettings) -> None:
    ux, uy = from_solve_grid(matched.x, matched.y, settings.factor, settings.origin)
    outcome.used_x, outcome.used_y, outcome.weight = ux, uy, matched.weight
    projected = residuals(matched, wcs)
    if projected is None:
        return
    rx, ry, crpix, scale = projected
    outcome.scale_arcsec = scale
    outcome.residual_rms_arcsec = float(math.sqrt(float(np.mean(rx**2 + ry**2)))) * scale
    measure = influence(matched.x, matched.y, rx, ry, crpix, scale)
    if measure is not None:
        outcome.fibre_shift_arcsec = measure["fibre_shift_arcsec"]
    try:
        header = fits.getheader(wcs, 0)
        outcome.crval = (float(header["CRVAL1"]), float(header["CRVAL2"]))
    except (OSError, KeyError, ValueError):
        pass


def match_used_to_sources(
    used_x: np.ndarray, used_y: np.ndarray, x: np.ndarray, y: np.ndarray, tolerance: float = 0.05
) -> np.ndarray:
    """For each source, the index of the used star at that position, or -1.

    The used positions went out on the xylist and came back through the same conversion, so they
    equal the sources' to floating-point error; `tolerance` is pixels.
    """
    index = np.full(len(x), -1, dtype=int)
    if len(used_x) == 0 or len(x) == 0:
        return index
    for k in range(len(used_x)):
        d = np.hypot(x - used_x[k], y - used_y[k])
        i = int(np.argmin(d))
        if d[i] <= tolerance:
            index[i] = k
    return index
