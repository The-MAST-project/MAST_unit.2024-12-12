"""A route name means the same thing on every component that serves it (#41).

`/stage/position` took `pos` while `/focuser/position` took `position` -- the same route
name, the same concept, two spellings. A client written against one component gets a 422 from
the other, which is how it was found: by hand, on mast02, mid-hardware-pass.

Parameter **order** is held to the same rule, deliberately, even though it is not a wire
concern: query parameters are named, so order cannot break a client. It is enforced to stop
the reader having to reconcile two spellings of the same route while looking for a real
difference.

Route *names* were #41's subject. Parameter names belong with them for the same reason: both
are the wire contract, and both are invisible to a suite that never calls two components with
one client.
"""

from __future__ import annotations

import ast
import collections

from . import astscan
from .astscan import GENERATED_INTERFACE_MODULES, GENERATED_INTERFACE_VERBS, ROUTER_MODULES

#: Route leaves whose components legitimately take different parameters. Empty, and worth
#: keeping that way -- an entry here is a wire inconsistency a consumer has to know about.
KNOWN_DIVERGENT: dict[str, str] = {}


def _routes_by_leaf(trees: dict[str, ast.Module]) -> dict[str, list[tuple[str, str]]]:
    """Path leaf -> [(module, handler name)] for every hand-registered route."""
    found: dict[str, list[tuple[str, str]]] = collections.defaultdict(list)
    for module in ROUTER_MODULES:
        for call in astscan.calls(trees[module]):
            if astscan.called_name(call) != "add_api_route" or len(call.args) < 2:
                continue
            path = call.args[1]
            leaf = path.right.value if isinstance(path, ast.BinOp) and isinstance(path.right, ast.Constant) else None
            if leaf is None:
                # f-string form, e.g. f"{base_path}/{UnitEndpoint.STATUS}"
                text = ast.unparse(path)
                leaf = text.rsplit("/", 1)[-1].rstrip('"').rstrip("}") if "/" in text else None
            if not isinstance(leaf, str):
                continue
            handler = next((kw.value for kw in call.keywords if kw.arg == "endpoint"), None)
            if handler is None:
                continue
            target = handler.func if isinstance(handler, ast.Call) else handler
            found[leaf.lstrip("/")].append((module, ast.unparse(target).rsplit(".", 1)[-1]))
    return found


def _parameters(tree: ast.Module, name: str) -> list[str] | None:
    for _, function in astscan.methods(tree):
        if function.name == name:
            return [a.arg for a in function.args.args if a.arg != "self"]
    return None


def test_a_route_name_takes_the_same_parameters_on_every_component():
    trees = dict(astscan.modules(astscan.UNIT_SRC))
    offenders = []

    for leaf, sites in _routes_by_leaf(trees).items():
        if leaf in KNOWN_DIVERGENT or len(sites) < 2:
            continue
        signatures = {}
        for module, handler in sites:
            params = _parameters(trees[module], handler)
            if params is not None:
                # A sequence, not a set. Order is not a wire concern -- query parameters are
                # named -- so this is a consistency rule rather than a correctness one (Eli,
                # 2026-08-18): two components serving one route name should read identically,
                # so a reader comparing them has nothing to reconcile.
                signatures[module] = tuple(params)
        if len(set(signatures.values())) > 1:
            # Declaration order, not sorted: order is the thing under test, so sorting here
            # would print two identical-looking tuples and hide the difference.
            detail = ", ".join(f"{module}({', '.join(params)})" for module, params in signatures.items())
            offenders.append(f"/{leaf} -- {detail}")

    assert not offenders, (
        "these route names take different parameters depending on the component, so one client "
        "cannot drive both (#41):\n    " + "\n    ".join(offenders)
    )


def test_the_generated_interface_verbs_are_uniform_by_construction():
    """The four generated verbs cannot diverge: one declaration, one generator (#40).

    Pinned so that reverting to per-component registration would have to face this test.
    """
    trees = dict(astscan.modules(astscan.UNIT_SRC))

    for module in GENERATED_INTERFACE_MODULES:
        for verb in GENERATED_INTERFACE_VERBS:
            params = _parameters(trees[module], verb)
            assert params is not None, f"{module} does not implement {verb}"
            assert params == [], f"{module}:{verb} takes {params}; the generated verbs take none"
