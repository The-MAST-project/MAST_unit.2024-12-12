#!/usr/bin/env python
"""Guide-log viewer -- a standalone graphical tool for the mast guider's nightly JSONL logs.

Reads the guide log written by ``src/mast_guider/guide_log.py`` (session_begin / cycle / event /
session_end records, one JSON object per line). Two modes:

- **Archived**: a tree picker (unit -> night -> session) built from *.jsonl files discovered under a
  chosen root (e.g. the share, ``Z:\\MAST``).
- **Live**: tail a running log file, polling for appended lines and updating the plots.

**Standalone by design.** It imports only the standard library, ttkbootstrap and matplotlib -- never
the mast/imaging stack. The guide log is stdlib JSONL precisely so the viewer needs nothing else; the
tiny reader below mirrors ``guide_log.read_guide_log`` (torn-trailing-line tolerant) rather than
importing it, so loading ``mast_guider``'s package __init__ (which pulls hardware/numpy) is avoided.

Run the GUI:   pythonw guide_log_viewer.py
Headless check: python guide_log_viewer.py --check [ROOT]
"""

from __future__ import annotations

import json
import math
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# ---- record schema (mirrors mast_guider/guide_log.py) --------------------------------------------
SESSION_BEGIN = "session_begin"
CYCLE = "cycle"
EVENT = "event"
SESSION_END = "session_end"

# decision -> colour; unknown decisions fall back to the default
DECISION_COLOURS = {
    "applied": "#2ecc71",
    "deadband": "#95a5a6",
    "held_scatter": "#e67e22",
    "star_lost": "#e74c3c",
}
DECISION_DEFAULT = "#3498db"

# event kind -> marker colour on the timeline
EVENT_COLOURS = {
    "star_lost": "#e74c3c",
    "reacquired": "#2ecc71",
    "validation": "#3498db",
    "reference_shift": "#9b59b6",
    "alert": "#e74c3c",
}

UNIT_RE = re.compile(r"(mast\d{2})", re.IGNORECASE)
NIGHT_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


# ---- reading -------------------------------------------------------------------------------------
def read_records(path: str | Path, *, start_offset: int = 0) -> tuple[list[dict], int]:
    """Read JSONL records from ``path`` starting at byte ``start_offset``.

    Returns ``(records, next_offset)``. Only complete lines are parsed; a torn trailing line (a write
    interrupted mid-append) is left for next time -- ``next_offset`` stops at the last newline. Robust
    to a line that fails to parse (skipped), so a corrupt record never aborts a read.
    """
    records: list[dict] = []
    path = Path(path)
    try:
        data = path.read_bytes()
    except OSError:
        return records, start_offset
    chunk = data[start_offset:]
    last_nl = chunk.rfind(b"\n")
    if last_nl == -1:
        return records, start_offset  # no complete line yet
    complete = chunk[: last_nl + 1]
    for raw in complete.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(obj, dict):
            records.append(obj)
    return records, start_offset + last_nl + 1


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _magnitude(value: Any) -> float | None:
    """A scalar from a value that may be a scalar or a 2-sequence (ra, dec)."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        parts = [float(v) for v in value if isinstance(v, (int, float))]
        return math.hypot(*parts) if len(parts) == 2 else (parts[0] if parts else None)
    if isinstance(value, (int, float)):
        return float(value)
    return None


# ---- discovery -----------------------------------------------------------------------------------
def looks_like_guide_log(path: Path) -> bool:
    """Cheap check: the first non-empty line is a JSON object with a known record ``type``."""
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as fp:
            for line in fp:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                return isinstance(obj, dict) and obj.get("type") in {
                    SESSION_BEGIN,
                    CYCLE,
                    EVENT,
                    SESSION_END,
                }
    except (OSError, ValueError):
        return False
    return False


def sessions_in(path: Path) -> list[str]:
    """Distinct session_ids in a log file, in first-seen order."""
    seen: list[str] = []
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as fp:
            for line in fp:
                line = line.strip()
                if not line:
                    continue
                try:
                    sid = json.loads(line).get("session_id")
                except ValueError:
                    continue
                if sid is not None and sid not in seen:
                    seen.append(sid)
    except OSError:
        pass
    return seen


def discover(root: str | Path, *, max_files: int = 2000) -> dict[str, dict[str, dict[str, list[str]]]]:
    """Scan ``root`` for guide-log *.jsonl files. Returns unit -> night -> file_path -> [session_ids].

    Unit and night are taken from the file's path (a ``mastNN`` component and a ``YYYY-MM-DD``
    component); both fall back to ``"(unknown)"`` when the path does not carry them.
    """
    tree: dict[str, dict[str, dict[str, list[str]]]] = {}
    root = Path(root)
    count = 0
    for path in sorted(root.rglob("*.jsonl")):
        if count >= max_files:
            break
        count += 1
        if not looks_like_guide_log(path):
            continue
        s = str(path)
        um = UNIT_RE.search(s)
        nm = NIGHT_RE.search(s)
        unit = um.group(1).lower() if um else "(unknown)"
        night = nm.group(1) if nm else "(unknown)"
        tree.setdefault(unit, {}).setdefault(night, {})[str(path)] = sessions_in(path)
    return tree


# ---- model ---------------------------------------------------------------------------------------
class SessionData:
    """The derived series for one session (or a whole file when session_id is None)."""

    def __init__(self, records: list[dict], session_id: str | None = None):
        self.session_id = session_id
        self.begin: dict | None = None
        self.end: dict | None = None
        self.events: list[dict] = []
        self.t: list[float] = []  # seconds since the first record
        self.dx: list[float] = []
        self.dy: list[float] = []
        self.err: list[float] = []  # |error| arcsec
        self.corr: list[float] = []  # |correction| arcsec (nan when none)
        self.scatter: list[float] = []
        self.n_used: list[float] = []
        self.decision: list[str] = []
        self.event_t: list[float] = []
        self.event_kind: list[str] = []
        self.x_label: str = "seconds since start"
        self._ingest(records)

    def _ingest(self, records: list[dict]) -> None:
        rel = [r for r in records if self.session_id is None or r.get("session_id") == self.session_id]
        t0 = next((_parse_ts(r.get("ts")) for r in rel if _parse_ts(r.get("ts"))), None)
        begin = next((r for r in rel if r.get("type") == SESSION_BEGIN), None)
        scale = None  # arcsec per px, if the session recorded one (lets |error| be filled on held frames)
        if begin:
            s = (begin.get("config") or {}).get("scale_arcsec_per_px")
            scale = float(s) if isinstance(s, (int, float)) else None

        def secs(r: dict) -> float:
            ts = _parse_ts(r.get("ts"))
            return (ts - t0).total_seconds() if ts and t0 else 0.0

        for r in rel:
            kind = r.get("type")
            if kind == SESSION_BEGIN:
                self.begin = r
            elif kind == SESSION_END:
                self.end = r
            elif kind == EVENT:
                self.event_t.append(secs(r))
                self.event_kind.append(str(r.get("event", "?")))
                self.events.append(r)
            elif kind == CYCLE:
                self.t.append(secs(r))
                dx = _coerce(r.get("dx"))
                dy = _coerce(r.get("dy"))
                self.dx.append(dx)
                self.dy.append(dy)
                err = _magnitude(r.get("error_arcsec"))
                if err is None and scale is not None and not (math.isnan(dx) or math.isnan(dy)):
                    err = math.hypot(dx, dy) * scale  # a held frame logs no error; derive it from the drift
                self.err.append(err if err is not None else math.nan)
                self.corr.append(_magnitude(r.get("correction_arcsec")) or math.nan)
                self.scatter.append(_coerce(r.get("scatter_px")))
                self.n_used.append(_coerce(r.get("n_used")))
                self.decision.append(str(r.get("decision", "?")))

        # Degenerate timestamps (an offline sim writes every record in one instant) collapse the
        # seconds axis -- fall back to the cycle index so the series still reads across the plot.
        if len(self.t) > 1 and (max(self.t) - min(self.t)) < 1.0:
            self.t = [float(i) for i in range(len(self.t))]
            self.event_t = []  # their seconds are meaningless once the axis is an index
            self.x_label = "cycle #"

    @property
    def rms(self) -> float | None:
        pts = [(x, y) for x, y in zip(self.dx, self.dy) if not math.isnan(x) and not math.isnan(y)]
        if not pts:
            return None
        return math.sqrt(sum(x * x + y * y for x, y in pts) / len(pts))

    def decision_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for d in self.decision:
            counts[d] = counts.get(d, 0) + 1
        return counts


def _coerce(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return math.nan


# ---- headless check ------------------------------------------------------------------------------
def run_check(root: str | None) -> int:
    import matplotlib  # noqa: F401
    import ttkbootstrap  # noqa: F401

    print("deps OK: ttkbootstrap, matplotlib import")
    if root:
        tree = discover(root)
        nfiles = sum(len(f) for u in tree.values() for f in u.values())
        print(f"discovered {nfiles} guide-log file(s) under {root}")
        for unit, nights in sorted(tree.items()):
            for night, files in sorted(nights.items()):
                for path, sids in files.items():
                    print(f"  {unit} {night}: {Path(path).name}  sessions={sids}")
    return 0


# ---- GUI -----------------------------------------------------------------------------------------
def launch_gui(selftest: bool = False) -> None:
    import tkinter as tk
    from tkinter import filedialog

    import ttkbootstrap as tb
    from matplotlib.backends.backend_tkagg import (
        FigureCanvasTkAgg,
        NavigationToolbar2Tk,
    )
    from matplotlib.figure import Figure
    from matplotlib.patches import Circle

    DEFAULT_ROOT = _default_root()

    app = tb.Window(title="mast-guide-scope", themename="darkly", size=(1280, 820))

    state: dict[str, Any] = {"path": None, "session": None, "offset": 0, "records": [], "live": False}

    # --- layout: left controls+tree | right notebook ----------------------------------------------
    paned = tb.Panedwindow(app, orient="horizontal")
    paned.pack(fill="both", expand=True, padx=6, pady=6)

    left = tb.Frame(paned, padding=4)
    paned.add(left, weight=1)
    right = tb.Frame(paned, padding=4)
    paned.add(right, weight=4)

    root_var = tk.StringVar(value=DEFAULT_ROOT)
    tb.Label(left, text="Archived logs", font="-weight bold").pack(anchor="w")
    row = tb.Frame(left)
    row.pack(fill="x", pady=(2, 4))
    tb.Entry(row, textvariable=root_var).pack(side="left", fill="x", expand=True)
    tb.Button(row, text="…", width=3, bootstyle="secondary", command=lambda: _choose_root()).pack(side="left", padx=(4, 0))
    tb.Button(left, text="Scan", bootstyle="info-outline", command=lambda: _scan()).pack(fill="x")

    tree = tb.Treeview(left, show="tree", bootstyle="dark", height=20)
    tree.pack(fill="both", expand=True, pady=6)

    tb.Separator(left).pack(fill="x", pady=4)
    live_row = tb.Frame(left)
    live_row.pack(fill="x")
    tb.Button(live_row, text="Tail a live log…", bootstyle="warning-outline", command=lambda: _open_live()).pack(side="left", fill="x", expand=True)
    live_lbl = tb.Label(left, text="", bootstyle="warning")
    live_lbl.pack(anchor="w", pady=(2, 0))

    nb = tb.Notebook(right)
    nb.pack(fill="both", expand=True)

    # Plots tab
    plots = tb.Frame(nb)
    nb.add(plots, text="Plots")
    fig = Figure(figsize=(9, 7), layout="constrained")
    ax_drift = fig.add_subplot(3, 1, 1)
    ax_quality = fig.add_subplot(3, 1, 2, sharex=ax_drift)
    ax_bull = fig.add_subplot(3, 1, 3)
    canvas = FigureCanvasTkAgg(fig, master=plots)
    canvas.get_tk_widget().pack(fill="both", expand=True)
    NavigationToolbar2Tk(canvas, plots)

    # Summary tab
    summary = tb.Frame(nb, padding=10)
    nb.add(summary, text="Summary")
    summary_text = tk.Text(summary, wrap="word", height=10, bg="#2b2b2b", fg="#ddd", insertbackground="#ddd", relief="flat")
    summary_text.pack(fill="both", expand=True)

    # Raw tab
    raw = tb.Frame(nb)
    nb.add(raw, text="Raw")
    raw_cols = ("t", "type", "detail")
    raw_tree = tb.Treeview(raw, columns=raw_cols, show="headings", bootstyle="dark")
    for c, w in (("t", 90), ("type", 120), ("detail", 900)):
        raw_tree.heading(c, text=c)
        raw_tree.column(c, width=w, anchor="w")
    raw_tree.pack(fill="both", expand=True)

    status = tb.Label(app, text="Ready", anchor="w", bootstyle="secondary")
    status.pack(fill="x", side="bottom")

    node_files: dict[str, tuple[str, str | None]] = {}  # tree item id -> (path, session_id)

    def _choose_root() -> None:
        d = filedialog.askdirectory(initialdir=root_var.get() or ".")
        if d:
            root_var.set(d)
            _scan()

    def _scan() -> None:
        tree.delete(*tree.get_children())
        node_files.clear()
        root = root_var.get()
        status.config(text=f"Scanning {root} …")
        app.update_idletasks()
        try:
            found = discover(root)
        except Exception as ex:  # noqa: BLE001 -- surface any scan error in the status bar
            status.config(text=f"Scan failed: {ex}")
            return
        total = 0
        for unit in sorted(found):
            u_node = tree.insert("", "end", text=unit, open=True)
            for night in sorted(found[unit]):
                n_node = tree.insert(u_node, "end", text=night, open=True)
                for path, sids in sorted(found[unit][night].items()):
                    f_node = tree.insert(n_node, "end", text=Path(path).name, open=False)
                    if not sids:
                        node_files[f_node] = (path, None)
                    for sid in sids:
                        leaf = tree.insert(f_node, "end", text=f"session {sid}")
                        node_files[leaf] = (path, sid)
                        total += 1
        status.config(text=f"{root}: {total} session(s)")

    def _on_select(_evt: Any) -> None:
        sel = tree.selection()
        if not sel or sel[0] not in node_files:
            return
        path, sid = node_files[sel[0]]
        state.update(live=False, path=path, session=sid, offset=0)
        live_lbl.config(text="")
        records, _ = read_records(path)
        state["records"] = records
        _render()

    tree.bind("<<TreeviewSelect>>", _on_select)

    def _open_live() -> None:
        f = filedialog.askopenfilename(filetypes=[("JSONL", "*.jsonl"), ("All", "*.*")])
        if not f:
            return
        state.update(live=True, path=f, session=None, offset=0, records=[])
        live_lbl.config(text=f"● tailing {Path(f).name}")
        _poll_live()

    def _poll_live() -> None:
        if not state["live"] or state["path"] is None:
            return
        new, off = read_records(state["path"], start_offset=state["offset"])
        if new:
            state["records"].extend(new)
            state["offset"] = off
            _render()
        app.after(1000, _poll_live)

    def _render() -> None:
        records = state["records"]
        data = SessionData(records, state["session"])
        _draw_plots(data)
        _draw_summary(data)
        _draw_raw(data, records)
        rms = data.rms
        src = "LIVE" if state["live"] else "archived"
        status.config(
            text=f"[{src}] {Path(state['path']).name if state['path'] else ''}  "
            f"cycles={len(data.t)}  events={len(data.events)}  "
            f"RMS={rms:.3f} px" if rms is not None else f"[{src}] {len(data.t)} cycles"
        )

    def _draw_plots(data: SessionData) -> None:
        for ax in (ax_drift, ax_quality, ax_bull):
            ax.clear()
        # drift
        ax_drift.axhline(0, color="#555", lw=0.8)
        ax_drift.plot(data.t, data.dx, label="dx (px)", color="#3498db", lw=1)
        ax_drift.plot(data.t, data.dy, label="dy (px)", color="#e67e22", lw=1)
        ax_drift.set_ylabel("drift (px)")
        ax_drift.legend(loc="upper right", fontsize=8)
        ax_drift.grid(True, alpha=0.2)
        # scatter (px) + error / correction (arcsec) -- one axis; similar magnitudes for a monitor.
        # |correction| is marked, so the sparse applied-frame commands show as points, not a flat line.
        ax_quality.plot(data.t, data.scatter, label="scatter (px)", color="#9b59b6", lw=1)
        ax_quality.plot(data.t, data.err, label="|error| (\")", color="#1abc9c", lw=1)
        ax_quality.plot(data.t, data.corr, label="|correction| (\")", color="#f1c40f", lw=1, marker="o", ms=3)
        ax_quality.set_ylabel("scatter (px) · err/corr (\")")
        ax_quality.set_xlabel(data.x_label)
        ax_quality.legend(loc="upper right", fontsize=8)
        ax_quality.grid(True, alpha=0.2)
        # event markers on both time axes
        for ax in (ax_drift, ax_quality):
            for et, ek in zip(data.event_t, data.event_kind):
                ax.axvline(et, color=EVENT_COLOURS.get(ek, "#777"), lw=0.8, ls="--", alpha=0.7)
        # bullseye
        cols = [DECISION_COLOURS.get(d, DECISION_DEFAULT) for d in data.decision]
        ax_bull.axhline(0, color="#555", lw=0.6)
        ax_bull.axvline(0, color="#555", lw=0.6)
        ax_bull.scatter(data.dx, data.dy, c=cols, s=14, alpha=0.8)
        rms = data.rms
        if rms is not None:
            ax_bull.add_patch(Circle((0, 0), rms, fill=False, color="#e74c3c", ls="--", lw=1))
            ax_bull.set_title(f"residual bullseye — RMS {rms:.3f} px", fontsize=9)
        ax_bull.set_xlabel("dx (px)")
        ax_bull.set_ylabel("dy (px)")
        ax_bull.set_aspect("equal", adjustable="datalim")
        ax_bull.grid(True, alpha=0.2)
        canvas.draw_idle()

    def _draw_summary(data: SessionData) -> None:
        summary_text.delete("1.0", "end")
        lines = []
        if data.begin:
            lines.append(f"session_id : {data.begin.get('session_id')}")
            lines.append(f"started    : {data.begin.get('started')}")
            lines.append(f"config     : {json.dumps(data.begin.get('config'), indent=2)}")
            lines.append(f"guide_stars: {json.dumps(data.begin.get('guide_stars'))}")
        lines.append(f"\ncycles     : {len(data.t)}")
        for d, n in sorted(data.decision_counts().items()):
            lines.append(f"  {d:14s}: {n}")
        rms = data.rms
        lines.append(f"RMS        : {rms:.3f} px" if rms is not None else "RMS        : n/a")
        if data.events:
            lines.append(f"\nevents     : {len(data.events)}")
            kinds: dict[str, int] = {}
            for e in data.events:
                k = str(e.get("event"))
                kinds[k] = kinds.get(k, 0) + 1
            for k, n in sorted(kinds.items()):
                lines.append(f"  {k:16s}: {n}")
        if data.end:
            lines.append(f"\nended      : {data.end.get('ended')}  reason={data.end.get('reason')!r}")
        summary_text.insert("1.0", "\n".join(lines))

    def _draw_raw(data: SessionData, records: list[dict]) -> None:
        raw_tree.delete(*raw_tree.get_children())
        rel = [r for r in records if state["session"] is None or r.get("session_id") == state["session"]]
        for r in rel[-2000:]:  # cap for responsiveness
            detail = {k: v for k, v in r.items() if k not in ("type", "ts", "session_id")}
            raw_tree.insert("", "end", values=(r.get("ts", "")[-12:], r.get("type", ""), json.dumps(detail)))

    # No auto-scan at startup: scanning a large share root (Z:\\MAST) is slow and could appear to
    # hang, so scanning is user-initiated (the Scan button). In self-test, build the UI, let it
    # render one frame, then exit -- a headless check of widget construction with no mainloop hang.
    if selftest:
        app.after(300, app.destroy)
    app.mainloop()


def _default_root() -> str:
    # The Filer's local root -- where the mast guider writes guide logs before they ship to the
    # share. Matches common.filer.Filer.local.root: C:/MAST on a Windows unit, /Storage/mast-share
    # /MAST on Linux. Falls back to a bundled sample, then home. (The viewer stays standalone, so
    # these are the known constants rather than an import of Filer.)
    beside = Path(__file__).resolve().parent / "sample"
    for cand in (Path(r"C:\MAST"), Path("/Storage/mast-share/MAST"), beside, Path.home()):
        if cand.exists():
            return str(cand)
    return str(Path.home())


def main(argv: list[str]) -> int:
    if "--check" in argv:
        i = argv.index("--check")
        root = argv[i + 1] if i + 1 < len(argv) else None
        return run_check(root)
    selftest = "--selftest-gui" in argv
    try:
        launch_gui(selftest=selftest)
    except BaseException:
        import traceback

        try:
            Path(__file__).with_name("viewer-error.log").write_text(traceback.format_exc(), encoding="utf-8")
        except OSError:
            pass
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
