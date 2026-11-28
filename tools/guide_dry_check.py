#!/usr/bin/env python
"""Dry-check the mast guider over an archived exposure sequence -- cycles only.

Runs MastGuiderCore + GuideCycle across a folder of same-field FITS frames (an unguided tracking
burst) and writes a **cycles-only** guide log (the §3.3 validation is deliberately NOT run). No
camera, no mount, no plate solve: a linear pixel->sky with the frame's binned pixel scale stands in
for a WCS, so each logged ``correction_arcsec`` is simply the drift times the scale. The reference is
the first frame; every later frame is measured with the star search seeded by the running drift (the
real GuideCycle seeding), so the growing unguided drift is tracked rather than lost.

The output guide log is a nightly JSONL the mast-guide-scope viewer can open.

Run (on a unit, cwd = the unit's ``src``):
    python ..\\tools\\guide_dry_check.py <frames_dir> --out <path.jsonl> [--scale 0.5232]
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import sys

from astropy.io import fits

from mast_guider import MastGuiderCore
from mast_guider.correction_policy import CorrectionPolicy
from mast_guider.guide_cycle import GuideCycle
from mast_guider.guide_log import GuideLog
from solvers.source_selection import SelectionParams


class LinearPixelToSky:
    """A stand-in for the real WCS-backed PixelToSky: drift (px) -> sky error (arcsec), linear.

    Enough for a dry-check -- GuideCycle only calls ``.correction(dx, dy)``. The real loop builds this
    from the acquisition solve; here there is no solve, so the burst's binned pixel scale is used.
    """

    def __init__(self, scale_arcsec_per_px: float):
        self.scale = float(scale_arcsec_per_px)

    def correction(self, dx: float, dy: float) -> tuple[float, float]:
        return (dx * self.scale, dy * self.scale)


def _seq_key(path: str):
    m = re.search(r"seq=(\d+)", os.path.basename(path))
    return (int(m.group(1)), path) if m else (1 << 30, path)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="cycles-only mast-guider dry-check over a FITS sequence")
    ap.add_argument("frames_dir", help="folder of same-field *.fits frames (sorted by seq=NNNN)")
    ap.add_argument("--out", required=True, help="guide-log JSONL to write")
    ap.add_argument("--session", default="dryrun", help="session_id for the log")
    ap.add_argument("--scale", type=float, default=0.5232, help="arcsec per binned pixel (default 0.5232)")
    ap.add_argument("--low-coma", type=float, default=None, help="low-coma radius px (default: frame half-diagonal)")
    ap.add_argument("--min-snr", type=float, default=None, help="guide-star SNR floor (default: selection's 15)")
    args = ap.parse_args(argv)

    paths = sorted(glob.glob(os.path.join(args.frames_dir, "*.fits")), key=_seq_key)
    if len(paths) < 2:
        print(f"need >= 2 fits in {args.frames_dir!r}, found {len(paths)}")
        return 1
    print(f"{len(paths)} frames from {args.frames_dir}")

    def load(p: str):
        return fits.getdata(p).astype(float)

    ref = load(paths[0])
    ny, nx = ref.shape[:2]
    center = (nx / 2.0, ny / 2.0)
    low_coma = args.low_coma if args.low_coma is not None else 0.5 * (nx * nx + ny * ny) ** 0.5

    sp_kw: dict = {"full_scale": 65535.0}
    if args.min_snr is not None:
        sp_kw["min_snr"] = args.min_snr
        sp_kw["relaxed_min_snr"] = args.min_snr
    core = MastGuiderCore(params=SelectionParams(**sp_kw))
    rs = core.reference(ref, optical_center=center, low_coma_radius=low_coma)
    print(f"reference ({os.path.basename(paths[0])}): {rs.n} guide stars, sigma_window {rs.sigma_window:.2f} px")

    gc = GuideCycle(core, LinearPixelToSky(args.scale), CorrectionPolicy())

    out_dir = os.path.dirname(args.out)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    if os.path.exists(args.out):
        os.remove(args.out)  # regenerate, not append (GuideLog opens the file append-only)

    counts: dict[str, int] = {}
    with GuideLog(
        args.out,
        args.session,
        config={"source": args.frames_dir, "scale_arcsec_per_px": args.scale, "frames": len(paths), "mode": "dry-check (cycles only)"},
        guide_stars={"n": rs.n},
    ) as log:
        for i, p in enumerate(paths[1:], start=1):
            result = gc.cycle(load(p))
            m = result.measurement
            log.cycle(
                decision=result.decision,
                dx=(m.dx if m is not None else None),
                dy=(m.dy if m is not None else None),
                error_arcsec=result.error_arcsec,
                correction_arcsec=result.correction_arcsec,
                scatter_px=result.scatter_px,
                n_used=(m.n_used if m is not None else None),
            )
            counts[result.decision] = counts.get(result.decision, 0) + 1
            dxy = f"dx={m.dx:+6.2f} dy={m.dy:+6.2f} scatter={result.scatter_px:.2f} n={m.n_used}" if m else "(no measurement)"
            print(f"  {i:03d} {os.path.basename(p):52s} {result.decision:12s} {dxy}")
        log.end(reason="dry-check complete")

    print(f"\nwrote {args.out}")
    print("decisions: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
