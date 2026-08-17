"""Invariant 4: the envelope is owned by registration, not by handlers (#34 stage 3).

`common.endpoints.add_api_route` wraps every handler, so a handler that returns a bare value,
returns nothing, or raises still answers a `CanonicalResponse`. Two things follow, and neither
is visible at runtime:

1. **No handler should wrap a payload itself.** `CanonicalResponse(value=X)` inside a handler
   is the boilerplate this stage removed, and #70 records why a second envelope is worse than
   redundant: `FullUnitStatus`'s fields are *typed as* the component status models, so a nested
   envelope corrupts the payload silently rather than loudly. The wrapper passes an existing
   envelope through, so a re-introduced wrap would not fail a test -- only this check catches it.
2. **No routed handler should be able to answer HTTP `null`.** Eight could before this stage.
   The wrapper turns a `None` return into `value=None`, which is a well-formed envelope but
   still cannot distinguish "refused" from "succeeded with nothing to say". Those two meanings
   have to be spelled: `CanonicalResponse(errors=[...])` or `CanonicalResponse_Ok`.

`CanonicalResponse_Ok` and `CanonicalResponse(errors=...)` are deliberately allowed. "ok" is a
value a consumer may check and a refusal is domain information; neither is plumbing.
"""

from __future__ import annotations

import ast

from . import astscan

DECLARING_MODULES = (
    "unit.py",
    "mount.py",
    "covers.py",
    "focuser.py",
    "stage.py",
    "imagers/__init__.py",
    "guiding.py",
    "acquirer.py",
    "autofocusing.py",
)


#: The lifecycle verbs `register_component_endpoints` emits (#40). They are routed, so
#: invariant 4 binds them -- but their declaration lives on the `Component` ABC in
#: MAST_common, not on the override, so a decorator scan of this tree cannot see them.
GENERATED_INTERFACE_VERBS = ("startup", "shutdown", "abort", "status")

#: The components whose routers call the generator. `unit.py` is not one -- its lifecycle verbs
#: are CONTRACT-tier and hand-registered -- and neither are the helper classes in the remaining
#: declaring modules, whose same-named methods are internal.
GENERATED_INTERFACE_MODULES = ("mount.py", "covers.py", "focuser.py", "stage.py", "imagers/__init__.py")


def _declared_handlers(trees):
    """Every routed handler in the unit tree, however it got routed.

    Two provenances: a method carrying `@endpoint(`, and the interface verbs the generator
    emits for every component. Scanning only the first would silently stop checking twenty
    handlers the moment #40 moved their declaration to the ABC.
    """
    for module in DECLARING_MODULES:
        for class_node, function in astscan.methods(trees[module]):
            declared = any(d.startswith("endpoint(") for d in astscan.decorators(function))
            generated = module in GENERATED_INTERFACE_MODULES and function.name in GENERATED_INTERFACE_VERBS
            if declared or generated:
                yield module, class_node.name, function


def _trees():
    return dict(astscan.modules(astscan.UNIT_SRC))


def test_no_handler_wraps_its_own_payload():
    trees = _trees()
    offenders = [
        astscan.Site(module, node.lineno, f"{cls}.{fn.name}")
        for module, cls, fn in _declared_handlers(trees)
        for node in ast.walk(fn)
        if isinstance(node, ast.Return)
        and node.value is not None
        and ast.unparse(node.value).startswith("CanonicalResponse(value")
    ]

    assert not offenders, (
        "these build the envelope themselves; return the bare value and let registration wrap it:\n    "
        + "\n    ".join(str(o) for o in offenders)
    )


def test_no_handler_can_answer_http_null():
    trees = _trees()
    offenders = []
    for module, cls, fn in _declared_handlers(trees):
        returns = [n for n in ast.walk(fn) if isinstance(n, ast.Return)]
        if not returns:
            offenders.append(astscan.Site(module, fn.lineno, f"{cls}.{fn.name}: no return at all"))
        elif any(r.value is None for r in returns):
            bare = next(r for r in returns if r.value is None)
            offenders.append(astscan.Site(module, bare.lineno, f"{cls}.{fn.name}: bare return"))

    assert not offenders, (
        "these can answer HTTP null, which cannot distinguish a refusal from a success -- "
        "return CanonicalResponse(errors=[...]) or CanonicalResponse_Ok:\n    " + "\n    ".join(str(o) for o in offenders)
    )


def test_the_scan_found_the_handlers():
    """Both assertions pass vacuously over an empty scan."""
    assert len(list(_declared_handlers(_trees()))) >= 60
