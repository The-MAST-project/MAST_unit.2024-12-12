"""Which routes start the unit up first (`@endpoint(..., autostartup=True)`).

Only the unit's high-level work: the unit itself, the acquirer, the guider and the autofocuser.
A component route never does -- a direct mount or focuser command on a unit that is not up
fails as it always has -- and neither do the unit's lifecycle verbs, its stops or its reads.
`common.endpoints` already refuses the flag on INTERFACE and IMMEDIATE routes, and
`add_api_route` refuses a declared route registered without the unit's autostarter; what is
left to check is where the flag is allowed, and the set it is on today.
"""

from __future__ import annotations

import ast

from . import astscan
from .astscan import DECLARED_MODULES

#: The only modules whose routes may autostart.
ALLOWED_MODULES = {"unit.py", "acquirer.py", "guiding.py", "autofocusing.py"}

#: The routes that autostart today. Pinned, so adding one is a deliberate edit here too.
EXPECTED = {
    "unit.py": {
        "expose",
        "endpoint_test_stage_repeatability",
        "_spiral_new_path_endpoint",
        "endpoint_spiral_next_step",
        "endpoint_spiral_previous_step",
        "endpoint_acquire_and_find_max_flux",
        "endpoint_focus_on_star",
    },
    "acquirer.py": {"endpoint_start_acquisition_and_guiding"},
    "guiding.py": {"endpoint_start_guiding"},
    "autofocusing.py": {"start_autofocus"},
}

#: The unit's lifecycle verbs: these are what an autostart calls, never what triggers one.
LIFECYCLE = {"endpoint_startup", "endpoint_shutdown", "endpoint_powerdown", "endpoint_abort", "endpoint_status"}


def _autostarting() -> dict[str, set[str]]:
    trees = dict(astscan.modules(astscan.UNIT_SRC))
    found: dict[str, set[str]] = {}
    for module in DECLARED_MODULES:
        for _, function in astscan.methods(trees[module]):
            if any(d.startswith("endpoint(") and "autostartup=True" in d for d in astscan.decorators(function)):
                found.setdefault(module, set()).add(function.name)
    return found


def test_only_the_high_level_modules_autostart():
    assert set(_autostarting()) <= ALLOWED_MODULES


def test_no_lifecycle_verb_autostarts():
    assert not (_autostarting().get("unit.py", set()) & LIFECYCLE)


def test_the_autostarting_routes_are_the_expected_ones():
    assert _autostarting() == EXPECTED


def test_unit_router_passes_the_autostarter_exactly_to_the_autostarting_routes():
    """`add_api_route` refuses a mismatch at import, but nothing in the suite builds the unit's
    router; this catches one before a unit fails to start."""

    tree = dict(astscan.modules(astscan.UNIT_SRC))["unit.py"]
    autostarting = set().union(*EXPECTED.values())
    with_starter, without = set(), set()
    for call in astscan.calls(tree):
        if astscan.called_name(call) != "add_api_route":
            continue
        target = next((kw.value for kw in call.keywords if kw.arg == "endpoint"), None)
        if target is None:
            continue
        if isinstance(target, ast.Call):
            target = target.func
        name = ast.unparse(target).rsplit(".", 1)[-1]
        (with_starter if any(kw.arg == "autostarter" for kw in call.keywords) else without).add(name)

    assert with_starter == autostarting
    assert not (without & autostarting)
