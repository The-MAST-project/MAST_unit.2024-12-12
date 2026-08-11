"""Invariant 9: a thread-dispatched operation is named `do_<operation>` (#42, #81).

The correspondence is the useful half -- it makes a dispatch site auditable without reading
the body of what it dispatches. `Thread(target=self.do_expose)` says what is now running;
`Thread(target=self.run_acquisition)` requires opening `run_acquisition` to find out.

This check exists because that invariant regressed *within hours of being written down*.
#102 introduced `run_acquisition` on 2026-08-09 as a deliberate thread entry point wrapping
`do_acquire` in try/except/finally, so a failed acquisition still releases its ram-disk
folder -- a good fix, with a docstring explaining exactly why an escaping exception on a
thread is invisible. It broke a convention nothing could check. That is the argument for this
file, made by the codebase rather than by an author, and it is why #42's invariant 9 no
longer claims the tree is already compliant.

## Scope

**The unit tree only.** `common` runs long-lived infrastructure threads -- the filer's
deferred-move sweeper, the notification worker, the process watcher and its log-stream
pumps. Those are not request-dispatched operations, and invariant 9 is about operations.
Judging them here would produce ten findings that should never be "fixed".

**Line numbers are not part of the identity.** #81's inventory recorded `zwo.py:148` and
`imagers/ascom.py:806`; both had drifted to 151 and 812 by the time this was written, with
nothing about the dispatch sites having changed. The known lists key on module and target.
"""

from __future__ import annotations

import ast

from . import astscan

OPERATION_PREFIX = "do_"

# Threads that are not a request-dispatched operation. Each stays a bare `Thread` on
# purpose, and each is listed so a later sweep does not read its survival as an oversight
# (#81 names this set).
OUT_OF_SCOPE = {
    # Long-lived PHD2 event-loop worker, not fire-and-forget.
    ("phd2/phd2.py", "self._worker"),
    # Analysis plotter, spawned after the operation it illustrates has finished.
    ("autofocusing.py", "plot_autofocus_analysis"),
    # Visualizer fan-out; one thread per registered viewer.
    ("imagers/ascom.py", "visualizer.func"),
    # Post-solve file cleanup. #102 gave this one a deliberate comment about the excepthook,
    # so its Thread is a considered choice rather than an oversight.
    ("solvers/mastrometry.py", "self._run_logged"),
}

# Dispatch sites whose target is not `do_`-named, keyed to the issue that owns the rename.
# All four are #81's; the two in `acquirer.py` are the ones #102 introduced.
KNOWN_MISNAMED = {
    ("acquirer.py", "self.run_acquisition"): "MAST_unit#81",
    ("guiding.py", "self.start_guiding"): "MAST_unit#81",
    ("zwo.py", "self.save_in_thread"): "MAST_unit#81",
}


def _dispatch_target(call: ast.Call) -> str | None:
    """The callable a dispatch hands to another thread, or None if this is not a dispatch."""
    name = astscan.called_name(call)
    if name == "Thread":
        for keyword in call.keywords:
            if keyword.arg == "target":
                return ast.unparse(keyword.value)
        return None
    # asyncio.to_thread(fn, ...) / asyncio.create_task(coro) -- the shape #81 moves to.
    if name in ("to_thread", "create_task") and call.args:
        return ast.unparse(call.args[0])
    return None


def dispatch_sites(modules) -> list[astscan.Site]:
    sites = []
    for module, tree in modules:
        for call in astscan.calls(tree):
            target = _dispatch_target(call)
            if target is not None:
                sites.append(astscan.Site(module, call.lineno, target))
    return sites


def detect(modules) -> set[astscan.Site]:
    """Dispatch sites in `modules` whose target is not `do_`-named and not allowlisted.

    Parameterized so the detector can be exercised over a synthetic source below -- the real
    assertion is "no unexpected findings", which a broken detector satisfies perfectly.
    """
    found = set()
    for site in dispatch_sites(modules):
        if site.key in OUT_OF_SCOPE:
            continue
        if site.detail.rsplit(".", 1)[-1].startswith(OPERATION_PREFIX):
            continue
        found.add(site)
    return found


def test_thread_dispatch_targets_are_do_named():
    astscan.report(detect(astscan.modules(astscan.UNIT_SRC)), KNOWN_MISNAMED, "dispatch-naming")


SYNTHETIC = """
import asyncio
import threading

def dispatch(self):
    threading.Thread(target=self.do_proper, name="ok").start()
    threading.Thread(target=self.improper, name="bad").start()
    asyncio.create_task(asyncio.to_thread(self.do_awaited))
    asyncio.to_thread(self.also_improper)
    threading.Thread(name="no target at all").start()
"""


def test_the_detector_flags_both_dispatch_shapes():
    """`Thread(target=...)` and `to_thread(...)`, and neither of the compliant ones."""
    found = detect([("synthetic.py", ast.parse(SYNTHETIC))])

    assert {site.detail for site in found} == {"self.improper", "self.also_improper"}


def test_the_out_of_scope_threads_still_exist():
    """An allowlist entry for a thread that is gone hides nothing and misleads a reader."""
    present = {site.key for site in dispatch_sites(astscan.modules(astscan.UNIT_SRC))}
    stale = sorted(OUT_OF_SCOPE - present)
    assert not stale, f"OUT_OF_SCOPE names threads that no longer exist: {stale}"


def test_the_scan_finds_the_compliant_sites_too():
    """Six `do_`-named dispatches exist; finding none would mean the scan is broken."""
    sites = dispatch_sites(astscan.modules(astscan.UNIT_SRC))
    compliant = [site for site in sites if site.detail.rsplit(".", 1)[-1].startswith(OPERATION_PREFIX)]
    assert len(compliant) >= 6, f"expected the do_-named dispatches, found {compliant}"
