#!/usr/bin/env python
"""Closed-loop simulation over an open-loop dry-check's drift -- the 'what if we had guided' run.

The open-loop dry-check (guide_dry_check.py) measured, per frame, how far the field had drifted from
the reference: the mount's real, UNCORRECTED motion D(t). Here that same D(t) is the disturbance, but
now a real GuideCycle + CorrectionPolicy commands a correction each step and a synthetic mount applies
it, so the loop closes. The output log's dx/dy is the GUIDED residual -- what the field would have done
had we actually been offsetting the mount on these frames. Open it in mast-guide-scope beside the
open-loop log to see the drift collapse toward zero.

Run (cwd = the unit's src):
    python ..\\tools\\guide_sim_guided.py <open-loop.jsonl> --out <guided.jsonl> [--max-scatter 2.0]
"""

from __future__ import annotations

import argparse
import math
import os
import sys

import numpy as np

from mast_guider.core import Measurement
from mast_guider.correction_policy import CorrectionPolicy
from mast_guider.guide_cycle import APPLIED, GuideCycle
from mast_guider.guide_log import GuideLog, read_guide_log


class LinearPixelToSky:
    def __init__(self, scale: float):
        self.scale = float(scale)

    def correction(self, dx: float, dy: float) -> tuple[float, float]:
        return (dx * self.scale, dy * self.scale)


class SimField:
    """A synthetic field driven by a real disturbance and corrected by the loop.

    ``increments`` are the per-step deltas of the open-loop drift (the mount's real motion between
    frames). ``measure`` advances the field by the next increment and returns the residual error;
    ``apply`` (a commanded offset, arcsec) moves the field back by the pixel-equivalent. Per-frame
    scatter / n_used are carried from the open-loop log so the hold gate behaves as it really would.
    """

    def __init__(self, increments, scatter, n_used, scale: float):
        self.incr = increments
        self.scatter = scatter
        self.n_used = n_used
        self.scale = float(scale)
        self.res = [0.0, 0.0]
        self.i = 0

    def measure(self, frame, *, predicted_offset: tuple[float, float] = (0.0, 0.0)) -> Measurement:
        self.res[0] += self.incr[self.i][0]
        self.res[1] += self.incr[self.i][1]
        n = max(1, int(self.n_used[self.i]))
        sc = float(self.scatter[self.i])
        self.i += 1
        offsets = np.tile([self.res[0], self.res[1]], (n, 1))
        return Measurement(
            dx=self.res[0], dy=self.res[1], error_px=(0.05, 0.05),
            scatter_px=sc, offsets=offsets, used=np.ones(n, bool),
        )

    def apply(self, correction_arcsec: tuple[float, float]) -> None:
        self.res[0] -= correction_arcsec[0] / self.scale
        self.res[1] -= correction_arcsec[1] / self.scale


def _rms(points) -> float:
    pts = [(x, y) for x, y in points if x is not None and y is not None and not math.isnan(x) and not math.isnan(y)]
    return math.sqrt(sum(x * x + y * y for x, y in pts) / len(pts)) if pts else float("nan")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="closed-loop sim over an open-loop dry-check log")
    ap.add_argument("open_loop_log")
    ap.add_argument("--out", required=True)
    ap.add_argument("--session", default=None)
    ap.add_argument("--max-scatter", type=float, default=2.0, help="hold gate; raise above the data's scatter floor")
    args = ap.parse_args(argv)

    records = read_guide_log(args.open_loop_log)
    begin = next((r for r in records if r.get("type") == "session_begin"), None)
    cfg = (begin or {}).get("config") or {}
    scale = float(cfg.get("scale_arcsec_per_px", 0.5232))
    cycles = [r for r in records if r.get("type") == "cycle"]

    # The disturbance drift(t): the open-loop measured drift, forward-filled over any star-lost gaps.
    drift: list[tuple[float, float]] = []
    sc_list: list[float] = []
    n_list: list[int] = []
    ts_list: list[str | None] = []  # the open-loop frame times, so the guided log keeps the real cadence
    last = (0.0, 0.0)
    for r in cycles:
        dx, dy = r.get("dx"), r.get("dy")
        last = (float(dx), float(dy)) if dx is not None and dy is not None else last
        drift.append(last)
        sc_list.append(float(r.get("scatter_px") or 0.3))
        n_list.append(int(r.get("n_used") or 12))
        ts_list.append(r.get("ts"))
    if len(drift) < 2:
        print("need >= 2 cycles in the open-loop log")
        return 1

    incr = [drift[0]] + [(drift[k][0] - drift[k - 1][0], drift[k][1] - drift[k - 1][1]) for k in range(1, len(drift))]
    sim = SimField(incr, sc_list, n_list, scale)
    gc = GuideCycle(sim, LinearPixelToSky(scale), CorrectionPolicy(), max_scatter_px=args.max_scatter)

    session = args.session or f"{(begin or {}).get('session_id', 'sim')}-guided"
    out_dir = os.path.dirname(args.out)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    if os.path.exists(args.out):
        os.remove(args.out)  # regenerate, not append (GuideLog opens the file append-only)

    counts: dict[str, int] = {}
    residuals: list[tuple[float, float]] = []
    with GuideLog(
        args.out,
        session,
        config={
            "source": args.open_loop_log, "scale_arcsec_per_px": scale,
            "mode": "closed-loop sim (guided)", "max_scatter_px": args.max_scatter,
        },
        guide_stars=(begin or {}).get("guide_stars"),
    ) as log:
        for i in range(len(drift)):
            r = gc.cycle(None)
            m = r.measurement
            if r.decision == APPLIED and r.correction_arcsec is not None:
                sim.apply(r.correction_arcsec)
            if m is not None:
                residuals.append((m.dx, m.dy))
            log.cycle(
                ts=ts_list[i],  # the open-loop frame's real time, so the guided log keeps the cadence
                decision=r.decision,
                dx=(m.dx if m is not None else None),
                dy=(m.dy if m is not None else None),
                error_arcsec=r.error_arcsec,
                correction_arcsec=r.correction_arcsec,
                scatter_px=r.scatter_px,
                n_used=(m.n_used if m is not None else None),
            )
            counts[r.decision] = counts.get(r.decision, 0) + 1
        log.end(reason="closed-loop sim complete")

    print(f"wrote {args.out}")
    print("decisions: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    print(f"unguided RMS (open-loop drift): {_rms(drift):6.3f} px")
    print(f"guided   RMS (this sim)       : {_rms(residuals):6.3f} px")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
