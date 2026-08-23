"""Invariant 3: a component answers one completion contract, not two (#43 stage 3).

**Async-vs-blocking is uniform per component** -- which is what the invariant actually
requires. A component answering two contracts means two clients calling two of its verbs get
incompatible behaviour from the same subsystem.

The companion check, that a *declared* signal is one the handler actually raises, is in
`test_completion_flags.py`.

**What used to be here and is not any more:** a gate asserting every routed operation declares
a completion at all. Withdrawn as MAST_unit#178 R4. It carried a 16-entry exemption list keyed
to the issues that own classifying each route (#154, #156, #80), which meant the same inventory
was maintained in two places -- and the list could not shrink honestly until MAST_unit#180 gave
the routes that report on the notification stream a form to declare. `completion=` and its
`x-completion` publication are unchanged; nothing forces a new route to carry one.
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


def _form(completion: str) -> str:
    """The contract a declaration expresses: immediate, blocking, or out-of-band.

    An activity flag and a notification channel are **one form here**, deliberately. Both mean
    "returns at once; completion arrives elsewhere", which is the choice this clause exists to
    keep uniform -- a client is either made to wait for the response or it is not. *Where* the
    out-of-band signal lands is per route, and the schema says so in `x-completion`, so folding
    the two together loses nothing a consumer needs (MAST_unit#180).
    """
    if completion.startswith("Completion."):
        return completion.removeprefix("Completion.").lower()
    return "out-of-band"


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
