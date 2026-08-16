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
    """Method names reached by a route, from any of the router modules.

    Two forms reach a handler, and both name a declared method:

        endpoint=self.status                    the method is the handler
        endpoint=self._new_path_endpoint()      a factory builds the handler at registration

    The second exists because some defaults are only knowable after construction -- a unit's
    configured fibre position cannot be written into a signature evaluated at import. The
    declaration sits on the factory (`@endpoint(..., factory=True)`), so the trailing call is
    stripped and the factory's own name is what must be declared.
    """
    routed = set()
    for module in ROUTER_MODULES:
        for call in astscan.calls(trees[module]):
            if astscan.called_name(call) != "add_api_route":
                continue
            target = next((kw.value for kw in call.keywords if kw.arg == "endpoint"), None)
            if target is None:
                continue
            # `self._new_path_endpoint()` -> `self._new_path_endpoint`
            if isinstance(target, ast.Call):
                target = target.func
            routed.add(ast.unparse(target).rsplit(".", 1)[-1])
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


FACTORY_BUILT_ROUTE = '''
class Unit:
    """A component whose handler is built after construction, not defined at import."""

    @endpoint(tier=Tier.OPERATION, factory=True)
    def _new_path_endpoint(self):
        def new_path(steps: int, center: int = configured):
            return {}

        return new_path

    @endpoint(tier=Tier.INTERFACE)
    def status(self):
        return {}

    def api_router(self):
        add_api_route(router, "/unit/spiral_new_path", endpoint=self._new_path_endpoint(), methods=["PUT"])
        add_api_route(router, "/unit/status", endpoint=self.status)
'''


def test_a_factory_built_route_reconciles_with_its_declaration():
    """Both halves of the scan must agree on a route registered as `endpoint=self.<factory>()`.

    Exercised against a synthetic source rather than waiting for the first real one: the scan
    reading `_new_path_endpoint()` (with the parentheses) would silently report the route as
    undeclared, and the two names would never meet. The nested `new_path` is deliberately left
    undeclared -- the declaration belongs on the factory, which is the class attribute.
    """
    tree = ast.parse(FACTORY_BUILT_ROUTE)
    trees = dict.fromkeys(ROUTER_MODULES + tuple(CROSS_MODULE_OWNERS.values()), tree)

    routed = _routed_method_names(trees)
    declared = _declared_method_names(trees)

    assert routed == {"_new_path_endpoint", "status"}
    assert not routed - declared
    assert "new_path" not in declared


def test_the_scan_found_the_surface():
    """Both assertions above pass vacuously over an empty scan; pin the real population."""
    trees = _unit_modules()

    routed = _routed_method_names(trees)
    assert len(routed) >= 40, f"expected the unit's routed methods, found {len(routed)}"
    assert _declared_method_names(trees) == routed


def test_no_route_carries_a_tag_of_its_own():
    """#39: the tag is the tier, read from the declaration.

    The helper raises on a `tags=` argument, so this is belt-and-braces -- but the raise
    happens at import on Windows, and this runs everywhere and names the line.
    """
    trees = _unit_modules()
    offenders = []
    for module in ROUTER_MODULES:
        for call in astscan.calls(trees[module]):
            if astscan.called_name(call) != "add_api_route":
                continue
            if any(keyword.arg == "tags" for keyword in call.keywords):
                offenders.append(f"{module}:{call.lineno}")

    assert not offenders, "the tag is the tier; drop the `tags=` argument:\n    " + "\n    ".join(offenders)
