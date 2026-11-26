"""The guide log: a nightly, append-only JSONL file, one JSON record per line, written local on the
unit and flushed a line at a time so a viewer can tail it as it grows.

One file per observing night; every record carries the guiding ``session_id`` (a night holds several
sessions, so the viewer filters by it). The file is written to a fast local path (a RAM disk) and
*copied* to the share at each session end -- the copy is the caller's job (`ship_to`), this class
owns the local file.

Record shape -- every record has ``type``, ``session_id`` and ``ts`` (UTC ISO), plus:

- ``session_begin``: ``started``, ``config``, ``guide_stars`` -- opens a session
- ``cycle``: one per `GuideCycle` result -- ``decision`` and the drift/error/correction/health fields
- ``event``: an out-of-band event -- ``event`` names the kind (see the ``*`` constants below); the
  validation kinds (`VALIDATION`, `REFERENCE_SHIFT`, `ALERT`) are first-class from the start so the
  validator logs into this same file (design 3.3)
- ``session_end``: ``ended``, ``reason`` -- closes a session

Stdlib only (json, datetime, pathlib, shutil), so the viewer reads it without the imaging stack and a
crash of the writer leaves every flushed line intact (a partial last line is skipped on read).
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# record types (the `type` field)
SESSION_BEGIN = "session_begin"
CYCLE = "cycle"
EVENT = "event"
SESSION_END = "session_end"

# event kinds (the `event` field of an EVENT record)
STAR_LOST = "star_lost"  # the cycle reported too few stars to measure
REACQUIRED = "reacquired"  # the global finder recovered the field after a loss
VALIDATION = "validation"  # a background validation solve completed (design 3.3)
REFERENCE_SHIFT = "reference_shift"  # validation found the reference has shifted on the sky
ALERT = "alert"  # the field is out of position / guiding is not holding


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _jsonable(o: Any) -> Any:
    """Last-resort encoder for numpy scalars and anything else json cannot take, so a stray value
    never costs a log line (the log must not fail the operation it records)."""
    item = getattr(o, "item", None)  # numpy generic -> python scalar
    if callable(item):
        try:
            return o.item()
        except Exception:  # noqa: BLE001 - fall through to str
            pass
    return str(o)


class GuideLog:
    """Append-only writer for one night's guide log, scoped to one ``session_id``.

    Open it at ``start_guiding`` (writes the ``session_begin``), call `cycle` / `event` through the
    session, and `end` at ``stop_guiding`` (writes the ``session_end`` and closes). Usable as a
    context manager; leaving the block ends the session if it is still open.
    """

    def __init__(
        self,
        path: str | Path,
        session_id: str,
        *,
        config: dict | None = None,
        guide_stars: dict | None = None,
    ):
        self.path = Path(path)
        self.session_id = str(session_id)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._f = open(self.path, "a", encoding="utf-8")  # noqa: SIM115 - closed in end()/__exit__
        self._closed = False
        self._write(SESSION_BEGIN, started=_utc_now(), config=config, guide_stars=guide_stars)

    def _write(self, record_type: str, **fields: Any) -> None:
        if self._closed:
            raise RuntimeError("guide log session already ended")
        record = {"type": record_type, "session_id": self.session_id, "ts": _utc_now(), **fields}
        self._f.write(json.dumps(record, default=_jsonable) + "\n")
        self._f.flush()  # a reader tailing the file sees the line at once; survives a process crash

    def cycle(
        self,
        *,
        decision: str,
        dx: float | None = None,
        dy: float | None = None,
        error_arcsec: tuple[float, float] | None = None,
        correction_arcsec: tuple[float, float] | None = None,
        scatter_px: float | None = None,
        n_used: int | None = None,
    ) -> None:
        """Record one guide cycle. ``decision`` is a `guide_cycle` outcome; the drift (``dx``/``dy``,
        binned px), the sky ``error_arcsec`` and the ``correction_arcsec`` sent (plain RA, Dec) are
        present as the decision warrants -- a held or lost cycle has no correction."""
        self._write(
            CYCLE,
            decision=decision,
            dx=dx,
            dy=dy,
            error_arcsec=list(error_arcsec) if error_arcsec is not None else None,
            correction_arcsec=list(correction_arcsec) if correction_arcsec is not None else None,
            scatter_px=scatter_px,
            n_used=n_used,
        )

    def event(self, kind: str, **fields: Any) -> None:
        """Record an out-of-band event (a ``*`` kind above, or any other), with arbitrary fields."""
        self._write(EVENT, event=kind, **fields)

    def end(self, reason: str = "") -> None:
        """Write the ``session_end`` and close the file. Idempotent -- a second call is a no-op, so a
        normal `end` followed by the context-manager exit does not raise."""
        if self._closed:
            return
        self._write(SESSION_END, ended=_utc_now(), reason=reason)
        self._closed = True
        self._f.close()

    def ship_to(self, dest_dir: str | Path) -> Path:
        """Copy the (closed) night file into ``dest_dir`` on the share, returning the destination
        path. Call after `end`. Overwrites a same-night file already there (the night file is a
        superset as it grows, so a re-ship of the same night is a refresh, not a loss)."""
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / self.path.name
        shutil.copy2(self.path, target)
        return target

    def __enter__(self) -> GuideLog:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.end(reason="closed" if exc_type is None else f"error:{exc_type.__name__}")


def read_guide_log(path: str | Path) -> list[dict]:
    """Every record in the file, in order -- the reader side (stdlib, no unit imports). A blank or
    partial trailing line (a crash caught mid-write) is skipped rather than raising."""
    records: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # a torn last line from a crash; everything before it is intact
    return records
