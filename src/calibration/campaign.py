"""Does focus calibration converge from far out of focus, both ways?

Run it from the unit machine while the unit service is up::

    python -m calibration.campaign --reference 12031            # dry run: prints the plan
    python -m calibration.campaign --reference 12031 --go
    python -m calibration.campaign --reference 12031 --go       # resumes; skips what is done

It drives the LIVE ``/calibrate/focuser`` endpoint over HTTP rather than importing
the phase.  Three reasons, all operational: the service keeps running (no restart
in the middle of a night), ``POST /calibrate/abort`` still works because it is the
same service the campaign is talking to, and the campaign process can be killed
without taking the run with it.

**Why this exists.**  Everything the focus phase does far from focus rests on
evidence from one side only.  ``analysis/sharpness.py`` says as much itself --
the metric falls monotonically across the frames we have, but *"we have no frames
near focus, so the claim 'it peaks AT focus' is still untested"* -- and
``near_hfd_max_px`` / ``max_best_hfd_px`` carry a matching warning that they are
estimates until measured on sky.  The in/out sign is resolved by a differential
donut move, which is the right physics, but has never been tested from the inside
arm at all.

**What it measures.**  Starting each run at ``reference + offset`` gives three
answers from one night:

* **capture range** -- the largest offset that still converges, per sign;
* **accuracy** -- the spread of solved positions across runs that did converge;
* **asymmetry** -- whether inside-focus and outside-focus behave differently,
  which the differential method is supposed to have removed.

**Products land wherever the campaign can actually write.**  The default root is
``Filer().accessible_shared_root()``, which PROBES.  With the share down but ``Z:``
still mapped -- the state on 2026-09-29 -- ``Filer.shared.root`` is still
``Z:/MAST/<host>/`` and writes go nowhere, while the probe correctly yields
``C:/MAST/``.  ``--products-root`` pins it regardless.

Results accumulate in ``<root>/Campaigns/focus-convergence/<night>/runs.jsonl``,
one JSON object per run, appended as each finishes.  Append-per-run, not a summary
at the end, because a campaign that is interrupted -- clouds, an abort, a killed
process -- must keep every run it already paid sky time for.  That file is also
what makes ``--go`` resumable.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from common.config import Config
from common.filer import Filer
from common.mast_logging import get_logger

logger = get_logger(__name__)

RUNS_FILE = "runs.jsonl"


@dataclass
class RunRecord:
    """One attempt, as it is written to `runs.jsonl`."""

    index: int
    offset: int  # signed, ticks from the reference
    seed: int  # reference + offset, what the phase was told to start from
    reference: int
    started_utc: str
    duration_seconds: float
    converged: bool
    best_position: float | None = None
    error_ticks: float | None = None  # best_position - reference
    star_diameter: float | None = None
    tolerance: float | None = None
    tries_used: int | None = None
    regime: str | None = None
    n_consistent_stars: int | None = None
    errors: list[str] = field(default_factory=list)
    note: str | None = None


def plan_offsets(settings) -> list[int]:
    """The signed offsets to visit, in the order they should be attempted.

    Interleaved by default (+500, -500, +2000, -2000, ...) so a night cut short
    still carries BOTH signs at every scale it reached -- the comparison the
    campaign exists to make.  Without interleaving, losing half a night loses one
    whole sign and the asymmetry question with it.
    """
    out: list[int] = []
    for magnitude in settings.offsets:
        pair = [magnitude, -magnitude]
        out.extend(pair)
    if not settings.interleave:
        out.sort(key=lambda v: (v < 0, abs(v)))
    return [o for o in out for _ in range(max(1, settings.repeats))]


def campaign_folder(settings, night: str) -> Path:
    root = settings.products_root or Filer().accessible_shared_root()
    folder = Path(root) / "Campaigns" / "focus-convergence" / night
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def load_done(folder: Path) -> list[dict]:
    """Records already written, so a resumed campaign does not repeat sky time."""
    path = folder / RUNS_FILE
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            logger.warning(f"{path}: skipping a malformed line")
    return out


def append_record(folder: Path, record: RunRecord) -> None:
    """Append one run. Flushed immediately: the next run may never finish."""
    with open(folder / RUNS_FILE, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(asdict(record)) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def summarise(records: list[dict]) -> str:
    """Capture range, accuracy and asymmetry -- the three questions, as text."""
    if not records:
        return "no runs yet"
    lines = [f"{'offset':>8} {'seed':>8} {'converged':>10} {'best':>10} {'error':>8} {'tries':>6} {'regime':>7}"]
    for r in records:
        best = f"{r['best_position']:.1f}" if r.get("best_position") is not None else "-"
        err = f"{r['error_ticks']:+.1f}" if r.get("error_ticks") is not None else "-"
        lines.append(
            f"{r['offset']:+8d} {r['seed']:8d} {str(r['converged']):>10} {best:>10} "
            f"{err:>8} {str(r.get('tries_used') or '-'):>6} {str(r.get('regime') or '-'):>7}"
        )

    solved = [r for r in records if r.get("converged") and r.get("error_ticks") is not None]
    lines.append("")
    for sign, name in ((1, "outward (+)"), (-1, "inward  (-)")):
        side = [r for r in records if (r["offset"] > 0) == (sign > 0)]
        ok = [r for r in side if r.get("converged")]
        reach = max((abs(r["offset"]) for r in ok), default=0)
        failed_at = min((abs(r["offset"]) for r in side if not r.get("converged")), default=None)
        lines.append(
            f"  {name}: converged {len(ok)}/{len(side)}, capture range >= {reach} ticks"
            + (f", first failure at {failed_at}" if failed_at is not None else "")
        )
    if solved:
        errs = [r["error_ticks"] for r in solved]
        spread = max(errs) - min(errs)
        mean = sum(errs) / len(errs)
        lines.append(f"  accuracy: {len(solved)} solved, mean error {mean:+.1f} ticks, spread {spread:.1f}")
        # Asymmetry is the whole point of the differential-sign method; if the two
        # sides land in different places, that method is not doing its job.
        out_e = [r["error_ticks"] for r in solved if r["offset"] > 0]
        in_e = [r["error_ticks"] for r in solved if r["offset"] < 0]
        if out_e and in_e:
            d = sum(out_e) / len(out_e) - sum(in_e) / len(in_e)
            lines.append(f"  asymmetry: outward mean - inward mean = {d:+.1f} ticks")
    return "\n".join(lines)


DEFAULT_UNIT_PORT = 8000


def _unit_api(host: str | None, port: int | None):
    """Talk to the local unit, with or without a reachable configuration database.

    `Config()` is the usual source of the port, but it must not be REQUIRED here.
    The controller carries both the config database and the share, so the night
    this campaign is most needed -- the controller down, products going to C: --
    is exactly the night `Config()` raises.  The unit app itself keeps serving
    from its boot cache, so the campaign should too.
    """
    from common.api import UnitApi

    resolved = port
    if resolved is None:
        try:
            service = Config().get_service("unit")
            resolved = service.port if service else DEFAULT_UNIT_PORT
        except Exception as ex:  # noqa: BLE001 -- a missing DB must not stop a night's work
            logger.warning(f"could not read the unit service port from the config DB ({ex}); "
                           f"using {DEFAULT_UNIT_PORT}. Pass --port to override.")
            resolved = DEFAULT_UNIT_PORT
    return UnitApi(ipaddr=host or "127.0.0.1", port=resolved, timeout=30)


def run_once(api, index: int, offset: int, reference: int, settings, ra, dec) -> RunRecord:
    """One calibration from `reference + offset`, waited out and recorded."""
    seed = reference + offset
    started = datetime.now(UTC)
    t0 = time.monotonic()
    rec = RunRecord(
        index=index,
        offset=offset,
        seed=seed,
        reference=reference,
        started_utc=started.isoformat(timespec="seconds"),
        duration_seconds=0.0,
        converged=False,
    )

    params = {"force": True, "seed": seed}
    if ra is not None:
        params["ra"] = ra
    if dec is not None:
        params["dec"] = dec

    logger.info(f"run {index}: seed={seed} (reference {reference} {offset:+d})")
    try:
        response = api.put(method="calibrate/focuser", params=params)
        if response is None or getattr(response, "failed", False):
            rec.errors = list(getattr(response, "errors", None) or ["no response"])
            rec.note = "the phase refused to start"
            rec.duration_seconds = time.monotonic() - t0
            return rec
    except Exception as ex:  # noqa: BLE001 -- one failed run must not end the campaign
        rec.errors = [repr(ex)]
        rec.note = "exception starting the run"
        rec.duration_seconds = time.monotonic() - t0
        return rec

    # Poll rather than block: the endpoint returns as soon as the phase thread starts.
    deadline = time.monotonic() + settings.run_timeout_seconds
    status = None
    while time.monotonic() < deadline:
        time.sleep(5.0)
        try:
            status = api.get(method="calibrate/status")
        except Exception as ex:  # noqa: BLE001 -- a dropped poll is not a failed run
            logger.debug(f"run {index}: status poll failed ({ex}); retrying")
            continue
        if status is not None and not getattr(status, "calibrating", False):
            break
    else:
        rec.note = f"timed out after {settings.run_timeout_seconds:.0f}s"
        rec.errors = ["run_timeout"]
        rec.duration_seconds = time.monotonic() - t0
        return rec

    rec.duration_seconds = time.monotonic() - t0
    latest = getattr(getattr(status, "latest", None), "focuser", None) if status else None
    result = getattr(latest, "analysis_result", None) if latest else None
    if result is not None and getattr(result, "has_solution", False):
        rec.converged = True
        rec.best_position = float(result.best_focus_position)
        rec.error_ticks = rec.best_position - reference
        rec.star_diameter = getattr(result, "best_focus_star_diameter", None)
        rec.tolerance = getattr(result, "tolerance", None)
        rec.n_consistent_stars = getattr(result, "n_consistent_stars", None)
    rec.tries_used = getattr(latest, "tries_used", None) if latest else None
    rec.regime = getattr(latest, "regime", None) if latest else None
    rec.errors = list(getattr(status, "errors", None) or []) if status else []
    return rec


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "--reference",
        type=int,
        required=True,
        help="in-focus position offsets are measured from (e.g. a known-good calibration)",
    )
    ap.add_argument("--go", action="store_true", help="actually run; without it, print the plan and exit")
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--ra", type=float, default=None, help="override the configured calibration target")
    ap.add_argument("--dec", type=float, default=None)
    ap.add_argument("--products-root", default=None, help="override where results land")
    ap.add_argument("--offsets", type=int, nargs="*", default=None, help="override the configured magnitudes")
    ap.add_argument("--repeats", type=int, default=None)
    ap.add_argument("--summary", action="store_true", help="summarise an existing campaign and exit")
    a = ap.parse_args(argv)

    # The configuration is a nice-to-have, not a requirement. The controller holds
    # both the config database and the share, so the night the campaign matters most
    # is the night this raises -- and every setting below has a usable default.
    conf = None
    try:
        conf = Config().get_unit()
    except Exception as ex:  # noqa: BLE001 -- see above; defaults carry the run
        logger.warning(f"config database unreachable ({ex}); running on defaults")
    if conf is None:
        logger.info("no unit configuration -- campaign settings fall back to their defaults")
    # unit_conf.calibration.settings.campaign -- the same path the Calibrator reads
    # its per-phase settings from. Absent on a unit whose config predates the
    # campaign block, so fall back to the defaults rather than refusing to run.
    cal = getattr(conf, "calibration", None)
    cal_settings = getattr(cal, "settings", None) if cal else None
    settings = getattr(cal_settings, "campaign", None) if cal_settings else None
    if settings is None:
        from common.config.calibration import FocusConvergenceCampaignSettings

        logger.info("no calibration.settings.campaign in the unit config -- using defaults")
        settings = FocusConvergenceCampaignSettings()
    if a.offsets:
        settings = settings.model_copy(update={"offsets": a.offsets})
    if a.repeats:
        settings = settings.model_copy(update={"repeats": a.repeats})
    if a.products_root:
        settings = settings.model_copy(update={"products_root": a.products_root})

    night = datetime.now(UTC).strftime("%Y-%m-%d")
    folder = campaign_folder(settings, night)
    done = load_done(folder)

    if a.summary:
        print(summarise(done))
        return 0

    planned = plan_offsets(settings)
    remaining = planned[len(done) :]
    print(f"campaign folder : {folder}")
    print(f"reference       : {a.reference}")
    print(f"planned runs    : {len(planned)}  ({len(done)} done, {len(remaining)} remaining)")
    print(f"offsets         : {[f'{o:+d}' for o in remaining]}")
    print(
        f"run timeout     : {settings.run_timeout_seconds:.0f}s each -> worst case "
        f"{len(remaining) * settings.run_timeout_seconds / 3600:.1f}h"
    )
    if not a.go:
        print("\ndry run -- pass --go to execute")
        return 0

    api = _unit_api(a.host, a.port)
    consecutive = 0
    for i, offset in enumerate(remaining, start=len(done)):
        rec = run_once(api, i, offset, a.reference, settings, a.ra, a.dec)
        append_record(folder, rec)
        done.append(asdict(rec))
        logger.info(
            f"run {i}: converged={rec.converged} best={rec.best_position} "
            f"error={rec.error_ticks} in {rec.duration_seconds:.0f}s"
        )
        consecutive = 0 if rec.converged else consecutive + 1
        if consecutive >= settings.max_consecutive_failures:
            logger.error(
                f"stopping: {consecutive} consecutive failures -- this is the sky, the focuser or the mount, not the offset"
            )
            break

    if settings.restore_focus_on_exit:
        try:
            api.put(method="focuser/position", params={"position": a.reference})
            logger.info(f"focuser returned to the reference position {a.reference}")
        except Exception as ex:  # noqa: BLE001 -- best effort; the campaign is already recorded
            logger.warning(f"could not return the focuser to {a.reference}: {ex}")

    print()
    print(summarise(done))
    print(f"\n{folder / RUNS_FILE}")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
    raise SystemExit(main())
