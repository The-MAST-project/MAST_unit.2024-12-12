"""Invariant 3: every long-running route publishes how it finishes (#43 stage 3).

Two checks here, both static:

1. **Every routed operation declares a completion**, so a new endpoint cannot be added
   without someone deciding whether it finishes on return, on a flag, or by blocking.
2. **Async-vs-blocking is uniform per component**, which is what the invariant actually
   requires -- a component answering two contracts means two clients calling two of its verbs
   get incompatible behaviour from the same subsystem.

A third check, that a declared flag is one the handler actually starts, is deliberately not
here: it needs a call graph across module boundaries and lands separately.

Both lists below are checked for set equality in both directions, so an entry that stops being
true fails the test rather than quietly encoding whatever was so on the day it was written.
"""

from __future__ import annotations

import ast
import re

from . import astscan
from .astscan import (
    CROSS_MODULE_OWNERS,
    GENERATED_INTERFACE_MODULES,
    GENERATED_INTERFACE_VERBS,
    ROUTER_MODULES,
)

#: Routed operations carrying no `completion=`, each keyed to the issue that owns it. Nothing is
#: here because it was forgotten -- an undeclared route publishes no `x-completion` at all,
#: which is what lets this check tell "not yet classified" from "classified as immediate".
KNOWN_UNDECLARED = {
    # Three threads that answer Ok and report nothing. They have no signal to declare.
    "expose": "MAST_unit#156",
    "endpoint_execute_assignment": "MAST_unit#156",
    "endpoint_test_stage_repeatability": "MAST_unit#156",
    # The same route means "started" on ZWO and "finished" on ASCOM.
    "imagers/__init__.py:abort": "MAST_unit#154",
    "start_exposure": "MAST_unit#154",
    "stop_exposure": "MAST_unit#154",
    # Completes when every component's Aborting clears; no unit-level flag says so.
    "endpoint_abort": "MAST_unit#80",
    # The guider, autofocus and spiral chains: #43's inventory could not classify these
    # without a run.
    "endpoint_start_guiding": "MAST_unit#43",
    "endpoint_stop_acquisition_and_guiding": "MAST_unit#43",
    "start_autofocus": "MAST_unit#43",
    "endpoint_stop_autofocus": "MAST_unit#43",
    "endpoint_start_acquisition_and_guiding": "MAST_unit#43",
    # The route is served by a factory-built handler since #117 (see the `factory=True`
    # declaration); the name follows the factory.
    "_spiral_new_path_endpoint": "MAST_unit#43",
    "endpoint_spiral_next_step": "MAST_unit#43",
    "endpoint_spiral_previous_step": "MAST_unit#43",
    "endpoint_spiral_end_path": "MAST_unit#43",
}

#: Components answering more than one completion contract. The mount is all four at once.
KNOWN_MIXED = {"mount.py": "MAST_unit#159"}

#: `Tier.DEMO` is parked and struck through in Swagger, so its form is not held to the
#: component's convention -- `dance` blocks, and making it uniform would be a behaviour change
#: to a route nobody should call.
EXEMPT_FROM_UNIFORMITY = {"dance"}

_COMPLETION = re.compile(r"completion=([A-Za-z_][\w.]*)")


def _trees() -> dict[str, ast.Module]:
    return dict(astscan.modules(astscan.UNIT_SRC))


def _routed_names(trees: dict[str, ast.Module]) -> dict[str, str]:
    """Routed handler name -> the module whose router registered it."""
    routed: dict[str, str] = {}
    for module in ROUTER_MODULES:
        if module in GENERATED_INTERFACE_MODULES:
            for verb in GENERATED_INTERFACE_VERBS:
                routed[f"{module}:{verb}"] = module
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


def _declarations(trees: dict[str, ast.Module]) -> dict[str, str | None]:
    """Declared handler name -> its completion expression, or None if it declares none."""
    found: dict[str, str | None] = {}
    for module in ROUTER_MODULES + tuple(CROSS_MODULE_OWNERS.values()):
        for _, function in astscan.methods(trees[module]):
            decorators = [d for d in astscan.decorators(function) if d.startswith("endpoint(")]
            if not decorators:
                continue
            match = _COMPLETION.search(decorators[0])
            key = f"{module}:{function.name}" if function.name in GENERATED_INTERFACE_VERBS else function.name
            found[key] = match.group(1) if match else None
    return found


def _split(key: str) -> tuple[str, int, str]:
    module, _, name = key.rpartition(":")
    return (module or "routed", 0, name)


def _form(completion: str) -> str:
    """The contract a declaration expresses: immediate, blocking, or watch-a-flag."""
    if completion.startswith("Completion."):
        return completion.removeprefix("Completion.").lower()
    return "activity"


def test_every_routed_operation_declares_how_it_finishes():
    trees = _trees()
    declarations = _declarations(trees)

    # `.get(name)` rather than a sentinel: a routed method carrying no `@endpoint` at all --
    # legitimate for a generated verb, which inherits its tier from the ABC -- still declares
    # no completion, and skipping it would hide exactly the case this check exists for.
    undeclared = {name for name in _routed_names(trees) if declarations.get(name) is None}

    # Keyed by module for a generated verb: `abort` alone names five different routes.
    astscan.report(
        {astscan.Site(*_split(name)) for name in undeclared},
        {(_split(name)[0], _split(name)[2]): issue for name, issue in KNOWN_UNDECLARED.items()},
        "undeclared-completion",
    )


def test_each_component_answers_one_completion_contract():
    """Invariant 3's uniformity clause: not "declare something", but "declare the same thing"."""
    trees = _trees()
    declarations = _declarations(trees)
    routed = _routed_names(trees)

    mixed = {}
    for name, module in routed.items():
        completion = declarations.get(name)
        bare = name.rsplit(":", 1)[-1]
        if completion is None or bare in EXEMPT_FROM_UNIFORMITY:
            continue
        form = _form(completion)
        if form == "immediate":
            continue  # a read is not a convention choice
        mixed.setdefault(module, set()).add(form)

    offenders = {module for module, forms in mixed.items() if len(forms) > 1}

    astscan.report(
        {astscan.Site(module, 0, "answers more than one completion contract") for module in offenders},
        {(module, "answers more than one completion contract"): issue for module, issue in KNOWN_MIXED.items()},
        "mixed-completion",
    )


def test_the_scan_found_the_surface():
    """Both checks pass vacuously over an empty scan; pin the real population."""
    trees = _trees()

    routed = _routed_names(trees)
    declared = {name for name, completion in _declarations(trees).items() if completion is not None}

    # Floors, not counts: the surface is meant to shrink. It has -- #124 removed 11 deprecated
    # routes and #41 collapsed four relative-motion routes into two -- so these are set well
    # below today's population to catch a scan that has collapsed, not to pin a number.
    assert len(routed) >= 45, f"expected the unit's routed operations, found {len(routed)}"
    assert len(declared) >= 35, f"expected the declared completions, found {len(declared)}"
