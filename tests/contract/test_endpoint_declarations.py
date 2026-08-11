"""Invariant 10, route half: the HTTP surface is declared, not discovered (#42, #34 stage 2).

`common.endpoints.add_api_route` refuses an undeclared handler at import, so the *runtime*
guarantee needs no test. What a static pass adds is the two things that refusal cannot see:

1. **A route registered directly on the router bypasses the refusal entirely.** One
   `router.add_api_route(...)` call is all it takes to put an untiered endpoint back on the
   surface, and nothing at runtime would object. That is the same shape of hole the
   `endpoint_` prefix decayed through -- 26 of 73 routes registered on bare methods while the
   convention looked authoritative.
2. **A declaration with no route is dead weight** -- it inflates `declared_endpoints()`,
   which #39, #40 and #52 consume as the definition of the surface.

Both are questions about the source, answerable without importing a component module, so this
runs on any platform.
"""

from __future__ import annotations

import ast
import re

from . import astscan

# The modules that build routers. Kept explicit rather than discovered: a new one should be a
# deliberate addition here, not something that quietly starts being scanned.
ROUTER_MODULES = ("unit.py", "mount.py", "covers.py", "focuser.py", "stage.py", "imagers/__init__.py")

# `self.<attr>.<method>` routes reach methods that live elsewhere; this is where.
CROSS_MODULE_OWNERS = {"acquirer": "acquirer.py", "autofocuser": "autofocusing.py", "guider": "guiding.py"}

DECLARED_MODULES = ROUTER_MODULES + tuple(CROSS_MODULE_OWNERS.values())


def _unit_modules() -> dict[str, ast.Module]:
    return dict(astscan.modules(astscan.UNIT_SRC))


def _routed_method_names(trees: dict[str, ast.Module]) -> set[str]:
    """Method names reached by a route, from any of the router modules."""
    routed = set()
    for module in ROUTER_MODULES:
        for call in astscan.calls(trees[module]):
            if astscan.called_name(call) != "add_api_route":
                continue
            target = next((ast.unparse(kw.value) for kw in call.keywords if kw.arg == "endpoint"), None)
            if target:
                routed.add(target.rsplit(".", 1)[-1])
    return routed


def _declared_method_names(trees: dict[str, ast.Module]) -> set[str]:
    declared = set()
    for module in DECLARED_MODULES:
        for _, function in astscan.methods(trees[module]):
            if any(decorator.startswith("endpoint(") for decorator in astscan.decorators(function)):
                declared.add(function.name)
    return declared


def test_no_route_bypasses_the_declaring_helper():
    """Every registration goes through the helper that refuses an undeclared handler.

    Matches the *method-call* form `<something>.add_api_route(`, which is the bypass. The
    helper is called as a plain function, `add_api_route(router, ...)`, so it does not match.
    """
    offenders = []
    for module in ROUTER_MODULES:
        source = (astscan.UNIT_SRC / module).read_text(encoding="utf-8")
        for lineno, line in enumerate(source.splitlines(), start=1):
            if re.search(r"\w\.add_api_route\s*\(", line):
                offenders.append(f"{module}:{lineno} {line.strip()[:88]}")

    assert not offenders, (
        "these register a route directly on the router, bypassing the declaration check "
        "(use `add_api_route(router, ...)` from common.endpoints):\n    " + "\n    ".join(offenders)
    )


def test_every_routed_method_is_declared():
    trees = _unit_modules()

    undeclared = sorted(_routed_method_names(trees) - _declared_method_names(trees))

    assert not undeclared, f"routed but carrying no @endpoint declaration: {undeclared}"


def test_every_declaration_is_routed():
    """A declaration with no route inflates the surface #39, #40 and #52 read."""
    trees = _unit_modules()

    orphaned = sorted(_declared_method_names(trees) - _routed_method_names(trees))

    assert not orphaned, f"declared @endpoint but not routed: {orphaned}"


def test_the_scan_found_the_surface():
    """Both assertions above pass vacuously over an empty scan; pin the real population."""
    trees = _unit_modules()

    routed = _routed_method_names(trees)
    assert len(routed) >= 40, f"expected the unit's routed methods, found {len(routed)}"
    assert _declared_method_names(trees) == routed
