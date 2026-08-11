"""A declared precondition that nothing calls is a defect (MAST_common#46, point 2).

`ImagerInterface.start_exposure` carried its precondition in the *abstract* method's body:
raise unless an exposure series is open. An abstract body only runs when a subclass
delegates to it, and no backend calls `super().start_exposure()` -- so the guard had never
executed once since it was written. MAST_common#44 moved it to a concrete
`require_open_exposure_series()` so the abstract method could declare a return type, and
deliberately left it unwired, because wiring it changes runtime behavior and wanted its own
decision.

That decision is MAST_common#46 point 1 and is not this check. This is point 2: the general
shape, caught mechanically, so a guard cannot sit unreachable again for as long as this one
did. It sat that long because nothing could notice.

**This establishes a convention rather than policing an existing one.** `require_*` is the
name a precondition takes, and this check asserts each one has a caller. There is exactly
one such method today, so the check is narrow now and grows with the convention -- which is
the point: the next `require_*` someone writes is policed from the moment it is named, and a
precondition that would rather not be checked has to avoid the prefix visibly.

Scanned across both trees, deliberately. `require_open_exposure_series` is *defined* in
`common` and its callers would be the unit's backends, so a check confined to either repo
alone would answer the wrong question -- and a MAST_common-only check would start reporting
a false positive the moment the unit wired it up.
"""

from __future__ import annotations

import ast

from . import astscan

PRECONDITION_PREFIX = "require_"

# Preconditions with no caller anywhere, keyed to the issue that owns wiring them up (or
# deciding to retire them). Landing that issue removes the entry.
KNOWN_UNCALLED = {
    ("interfaces/imager.py", "require_open_exposure_series"): "MAST_common#46",
}


def detect(modules) -> set[astscan.Site]:
    """Preconditions in `modules` that nothing in `modules` calls.

    Takes a list rather than an iterator: definitions and call sites need two passes over the
    same trees. Parameterized so the detector can be run over a synthetic source below --
    otherwise "no unexpected findings" would pass just as well with the detector broken.
    """
    modules = list(modules)

    defined: dict[str, astscan.Site] = {}
    for module, tree in modules:
        for function in astscan.functions(tree):
            if function.name.startswith(PRECONDITION_PREFIX):
                defined[function.name] = astscan.Site(module, function.lineno, function.name)

    called: set[str] = set()
    for _, tree in modules:
        for call in astscan.calls(tree):
            name = astscan.called_name(call)
            if name in defined:
                called.add(name)

    return {site for name, site in defined.items() if name not in called}


def _both_trees():
    for root in (astscan.COMMON_ROOT, astscan.UNIT_SRC):
        yield from astscan.modules(root)


def test_every_declared_precondition_has_a_caller():
    astscan.report(detect(_both_trees()), KNOWN_UNCALLED, "unreachable precondition")


SYNTHETIC = """
class Widget:
    def require_called(self):
        pass

    def require_never_called(self):
        pass

    def work(self):
        self.require_called()
"""


def test_the_detector_flags_only_the_uncalled_precondition():
    found = detect([("synthetic.py", ast.parse(SYNTHETIC))])

    assert {site.detail for site in found} == {"require_never_called"}


def test_the_reference_case_is_still_the_reference_case():
    """`require_open_exposure_series` must remain visible to the scan.

    The check above is satisfied by finding nothing, so if the naming convention were
    renamed away or the locator broke, it would pass while checking nothing at all. This
    pins the one method the convention was extracted from.
    """
    names = {site.detail for site in detect(_both_trees())} | {
        function.name
        for root in (astscan.COMMON_ROOT, astscan.UNIT_SRC)
        for _, tree in astscan.modules(root)
        for function in astscan.functions(tree)
        if function.name.startswith(PRECONDITION_PREFIX)
    }
    assert "require_open_exposure_series" in names, (
        "the reference precondition is gone -- if it was renamed, rename the convention with it"
    )
