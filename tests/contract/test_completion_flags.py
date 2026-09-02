"""A declared completion flag is one the handler actually starts (#43 stage 3, check 2).

The other completion checks ask whether a route declares *something* and whether a component
declares the *same* thing. This one asks whether the declaration is **true**, which is the
failure that hurts: `completion=MountActivities.Slewing` on a handler whose chain never starts
`Slewing` publishes a signal a client waits on forever. That is strictly worse than declaring
nothing, because nothing is visibly nothing.

## Scope, deliberately limited (Eli's call, 2026-08-17)

The walk follows `self.<method>` within the handler's own class -- including a method used as a
thread target, which is the fire-and-flag idiom -- and **stops at the module boundary**. It does
not follow `self.acquirer.…`, `self._backend.…` or `self.guider.…`.

Verifying those would need a cross-module call graph, which the suite would then have to
maintain, and a call graph that is subtly wrong is worse than one that is absent. So a handler
whose flag could be started beyond the boundary is reported as **unverifiable** and listed here
by name, rather than silently counted as passing. What the check guarantees is therefore narrow
and honest: of the declarations it can see all of, none is a lie.

The lie case fails the test. The blind spot is **reported, not asserted** (MAST_unit#178 R2):
a refactor that moves a flag-start behind a delegate grows the unverifiable set without
anything being wrong, and #81's dispatch conversion does precisely that at eleven sites.
Pinning the set by equality means that work fails a test in a file about something else, so the
set is warned about instead and read from pytest's warnings summary.
"""

from __future__ import annotations

import ast
import re
import warnings

from . import astscan
from .astscan import (
    CROSS_MODULE_OWNERS,
    GENERATED_INTERFACE_MODULES,
    GENERATED_INTERFACE_VERBS,
    ROUTER_MODULES,
)

#: Declarations the static walk cannot confirm, because the flag is started past a boundary it
#: does not cross. Not violations -- unknowns, listed so the limit is visible rather than
#: implied. Each names where the chain leaves.
KNOWN_UNVERIFIABLE = {
    "imagers/__init__.py:shutdown": "delegates to self._backend.shutdown()",
    "endpoint_acquire_and_find_max_flux": "delegates to self.flux_metering.start(), which raises the flag",
}

#: Declarations the walk resolved fully and found false. Empty is the point.
KNOWN_UNSTARTED: dict[str, str] = {}

_COMPLETION = re.compile(r"completion=([A-Za-z_][\w.]*)")


def _trees() -> dict[str, ast.Module]:
    return dict(astscan.modules(astscan.UNIT_SRC))


def _routed(trees: dict[str, ast.Module]) -> dict[str, str]:
    routed: dict[str, str] = {}
    for module in ROUTER_MODULES:
        if module in GENERATED_INTERFACE_MODULES:
            for verb in GENERATED_INTERFACE_VERBS:
                routed[verb] = module
        for call in astscan.calls(trees[module]):
            if astscan.called_name(call) != "add_api_route":
                continue
            target = next((kw.value for kw in call.keywords if kw.arg == "endpoint"), None)
            if target is None:
                continue
            if isinstance(target, ast.Call):
                target = target.func
            routed[ast.unparse(target).rsplit(".", 1)[-1]] = module
    return routed


def _methods(tree: ast.Module) -> dict[str, ast.FunctionDef]:
    found: dict[str, ast.FunctionDef] = {}
    for class_node in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
        for statement in class_node.body:
            if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef):
                found.setdefault(statement.name, statement)
    return found


def _reachable_flags(function: ast.FunctionDef, methods: dict[str, ast.FunctionDef]) -> tuple[set[str], bool]:
    """Flags started within the class, and whether the chain also left the module."""
    flags: set[str] = set()
    left_module = False
    seen: set[str] = set()

    def visit(node: ast.FunctionDef) -> None:
        nonlocal left_module
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call) and astscan.called_name(sub) == "start_activity" and sub.args:
                flags.add(ast.unparse(sub.args[0]).split(",")[0].strip())
            # `self.foo` -- called, or handed to a Thread as its target
            if isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name) and sub.value.id == "self":
                if sub.attr in methods:
                    if sub.attr not in seen:
                        seen.add(sub.attr)
                        visit(methods[sub.attr])
                elif isinstance(sub.ctx, ast.Load):
                    continue
            # `self.<component>.<method>()` -- the chain leaves this module
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute):
                target = ast.unparse(sub.func)
                if target.startswith("self.") and target.count(".") >= 2:
                    left_module = True

    visit(function)
    return flags, left_module


def _declared_activity_completions(trees: dict[str, ast.Module]) -> dict[str, tuple[str, str]]:
    """key -> (module, declared flag) for every routed handler declaring an activity flag."""
    routed = _routed(trees)
    found: dict[str, tuple[str, str]] = {}
    for module in ROUTER_MODULES + tuple(CROSS_MODULE_OWNERS.values()):
        for _, function in astscan.methods(trees[module]):
            decorators = [d for d in astscan.decorators(function) if d.startswith("endpoint(")]
            if not decorators or function.name not in routed:
                continue
            match = _COMPLETION.search(decorators[0])
            if not match or match.group(1).startswith("Completion."):
                continue
            key = f"{module}:{function.name}" if function.name in GENERATED_INTERFACE_VERBS else function.name
            found[key] = (module, match.group(1))
    return found


def _classify(trees: dict[str, ast.Module]) -> tuple[set[str], set[str]]:
    """(unverifiable, unstarted) keys."""
    unverifiable, unstarted = set(), set()
    for key, (module, flag) in _declared_activity_completions(trees).items():
        name = key.rsplit(":", 1)[-1]
        methods = _methods(trees[module])
        function = methods.get(name)
        if function is None:
            unverifiable.add(key)
            continue
        flags, left_module = _reachable_flags(function, methods)
        if flag in flags:
            continue
        (unverifiable if left_module else unstarted).add(key)
    return unverifiable, unstarted


def test_no_declared_flag_is_one_the_handler_never_starts():
    """The lie case: a published signal that never fires leaves a client waiting forever."""
    _, unstarted = _classify(_trees())

    modules = {key: module for key, (module, _) in _declared_activity_completions(_trees()).items()}

    astscan.report(
        {astscan.Site(modules[key], 0, key.rsplit(":", 1)[-1]) for key in unstarted},
        {tuple(key.split("|")): issue for key, issue in KNOWN_UNSTARTED.items()},
        "unstarted-completion-flag",
    )


def test_the_unverifiable_declarations_are_reported():
    """The blind spot is reported rather than pinned -- see the module docstring (MAST_unit#178).

    A declaration this walk cannot confirm is an unknown, not a violation. Warning keeps the
    limit visible in the warnings summary without failing a refactor that legitimately moves a
    flag-start behind a delegate.
    """
    unverifiable, _ = _classify(_trees())
    drift = unverifiable.symmetric_difference(KNOWN_UNVERIFIABLE)

    if drift:
        warnings.warn(
            "the set of declarations this check cannot confirm has changed:\n"
            f"    now unverifiable: {sorted(unverifiable)}\n"
            f"    listed:           {sorted(KNOWN_UNVERIFIABLE)}",
            stacklevel=2,
        )


def test_the_scan_found_the_declarations():
    """Both assertions pass vacuously over an empty scan."""
    declared = _declared_activity_completions(_trees())

    assert len(declared) >= 20, f"expected the activity-flag declarations, found {len(declared)}"
