"""What a calibration run leaves behind: a result file, a plot, and the frames.

Every phase writes its frames into a per-run folder on the **RAM disk**
(``PathMaker.make_calibration_folder``), which is fast but volatile -- it does
not survive a reboot and is not visible from other machines.  A run is only
useful later if its folder reaches the shared area, so that move is the last act
of every run, **successful or not**: a failed run's frames are exactly the ones
worth replaying offline (all five failures of 2026-07-21 were diagnosed from
saved frames, not from logs).

Three artifacts, mirroring what the ps3cli autofocus flow already produces:

* ``status.json`` -- the phase's result model, same convention as
  ``Autofocuser.save_analysis`` (``model_dump_json(indent=4)`` into
  ``status.json``), so downstream tooling reads one shape for both flows.
* ``vcurve.png`` -- the fitted curve over the measured samples, for any phase
  that produces one.  A plot is what makes a bad fit obvious at a glance; the
  numbers alone hid a 9px vertex excursion that only looked benign because the
  parabola averaged it out.
* the FITS frames themselves, already written by the phase.

Nothing here may raise.  The frames are the science; losing a plot or a JSON
dump must never fail a run that otherwise succeeded, and a failure to move must
be loud in the log but not fatal.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from calibration.logging_context import init_calibration_log
from common.filer import Filer

logger = logging.getLogger("mast.unit." + __name__)
init_calibration_log(logger)


def save_status(folder: str | None, status, errors: list[str] | None = None) -> None:
    """Write ``status.json`` into the run folder.

    ``status`` is any pydantic model (``HFDAutofocusStatus``,
    ``OpticalCenterResult``-carrying wrapper, ...) or a plain dict.  Deliberately
    the same filename and formatting as the ps3cli flow's
    ``Autofocuser.save_analysis`` -- one convention, two producers.
    """
    if not folder:
        return
    path = os.path.join(folder, "status.json")
    try:
        if hasattr(status, "model_dump_json"):
            payload = status.model_dump_json(indent=4)
        else:
            import json

            payload = json.dumps(status, indent=4, default=str)
        with open(path, "w", encoding="utf-8") as f:
            f.write(payload)
        logger.debug(f"wrote {path}")
    except Exception as ex:
        logger.warning(f"could not write {path}: {ex}")


def plot_vcurve(folder: str | None, result, best_position=None) -> None:
    """Plot the focus V-curve (``vcurve.png``) into the run folder.

    Plots the measured samples, the fitted parabola ``D^2 = a*x^2 + b*x + c``
    over the sampled range, the solved vertex and the tolerance band, so the
    quality of a fit is judgeable without re-deriving it.  Invalid samples are
    drawn distinctly rather than dropped -- *which* positions failed to measure
    is diagnostic.
    """
    if not folder or result is None:
        return
    samples = [s for s in (getattr(result, "focus_samples", None) or []) if s.focus_position is not None]
    if not samples:
        logger.debug("no focus samples to plot")
        return

    path = os.path.join(folder, "vcurve.png")
    try:
        import matplotlib

        matplotlib.use("Agg")  # headless: the unit has no display
        import matplotlib.pyplot as plt
        import numpy as np

        pos = np.array([s.focus_position for s in samples], dtype=float)
        hfd = np.array(
            [s.hfd_pixels if s.hfd_pixels is not None else np.nan for s in samples], dtype=float
        )
        valid = np.array([bool(s.is_valid) for s in samples])

        fig, ax = plt.subplots(figsize=(8, 5))
        ax.plot(pos[valid], hfd[valid], "o", color="tab:blue", label="measured (valid)")
        if (~valid).any():
            ax.plot(pos[~valid], hfd[~valid], "x", color="tab:red", label="invalid")

        # The fit is on D^2 (a parabola in squared diameter); draw it back in D.
        a, b, c = (getattr(result, k, None) for k in ("vcurve_a", "vcurve_b", "vcurve_c"))
        if None not in (a, b, c) and a:
            xs = np.linspace(pos.min(), pos.max(), 200)
            d2 = a * xs**2 + b * xs + c
            ax.plot(xs, np.sqrt(np.clip(d2, 0, None)), "-", color="tab:green", label="fit")

        best = best_position if best_position is not None else getattr(result, "best_focus_position", None)
        if best is not None:
            ax.axvline(best, color="tab:orange", lw=2, label=f"best = {best:.1f}")
            tol = getattr(result, "tolerance", None)
            if tol:
                ax.axvspan(best - tol, best + tol, color="tab:orange", alpha=0.12,
                           label=f"tolerance +/-{tol:.0f}")

        dmin = getattr(result, "best_focus_star_diameter", None)
        stars = getattr(result, "n_consistent_stars", None)
        subtitle = []
        if dmin is not None:
            subtitle.append(f"Dmin={dmin:.2f}px")
        if stars is not None:
            subtitle.append(f"{stars} consistent stars")
        ax.set_title("HFD focus V-curve" + (f"  ({', '.join(subtitle)})" if subtitle else ""))
        ax.set_xlabel("focuser position (ticks)")
        ax.set_ylabel("HFD (pixels)")
        ax.grid(alpha=0.3)
        ax.legend()
        fig.tight_layout()
        fig.savefig(path, dpi=110)
        plt.close(fig)
        logger.info(f"wrote {path}")
    except Exception as ex:
        logger.warning(f"could not plot the V-curve into {path}: {ex}")


def move_to_shared(folder: str | None) -> None:
    """Move the run folder off the RAM disk to the shared area.

    Called at the END of every run, success or failure -- a failed run's frames
    are the ones most worth keeping.  ``Filer.move_ram_to_shared`` preserves the
    path hierarchy below the root and moves in a background thread, so this
    returns immediately and does not delay the phase.
    """
    if not folder:
        return
    try:
        filer = Filer()
        if not filer.ram:
            logger.debug("no RAM disk configured; leaving the run folder in place")
            return
        # Compare POSIX-normalised: Filer's roots use forward slashes
        # ("D:/MAST/") while PathMaker hands back a native Windows path
        # ("D:\\MAST\\..."), so a naive startswith() is always False and
        # silently skips the move -- which is exactly what happened on
        # 2026-07-22 until eight runs' frames were found still on the RAM disk.
        # Filer.move_ram_to_shared itself is fine: it as_posix()es before
        # rewriting the root, so it is only this guard that needed normalising.
        if not Path(folder).as_posix().startswith(Path(filer.ram.root).as_posix()):
            logger.debug(f"{folder} is not on the RAM disk; leaving it in place")
            return
        filer.move_ram_to_shared(folder)
        logger.info(f"moving {folder} -> shared area (background)")
    except Exception as ex:
        logger.warning(f"could not move {folder} to the shared area: {ex}")
