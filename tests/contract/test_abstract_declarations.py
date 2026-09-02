"""Invariant 10, interface half: every `@abstractmethod` declares its return type.

This is MAST_common#45's definition of done. The check is the deliverable, not the list of
offenders: it enumerates the population on every run, so nobody has to keep a count
accurate by hand.

Why it matters concretely. `Component.status()` was declared abstract with no return
annotation at all, and every component in the fleet inherits that. Nothing constrained what
an implementation returned, which is the mechanism behind MAST_unit#100 -- `ImagerInterface`
inherited the undeclared `status`, three backends each invented a meaning for it, and
`/imager/status` was a 500 on two of the three. That was found by a hardware pass on mast04,
because there was nothing for a type checker to check against.

It lives in this repo rather than in MAST_common because #52 hosts the contract checks and
because the unit's CI already checks out `common` as a sibling. It scans `common`, not this
tree.
"""

from __future__ import annotations

import ast

from . import astscan

# Every abstract method in `common` that does not declare a return type, keyed to the issue
# that owns declaring it. Twelve as of 2026-08-11, across four ABCs -- the population
# MAST_common#45 first counted, reproduced by this check rather than transcribed from it.
#
# Removing an entry is part of landing its owning issue. Adding one requires an issue: an
# undeclared abstract signature is how three backends came to disagree about `status`.
KNOWN_UNDECLARED = {
    ("interfaces/components.py", "Component.startup"): "MAST_unit#73, then MAST_common#45",
    ("interfaces/components.py", "Component.shutdown"): "MAST_unit#73, then MAST_common#45",
    ("interfaces/components.py", "Component.abort"): "MAST_unit#73, then MAST_common#45",
    ("interfaces/components.py", "Component.status"): "MAST_common#45",
    # Abstract and implemented by all eight components, but routed nowhere -- and correctly
    # so: the INTERFACE tier is startup/shutdown/abort/status. It wants `-> None` for
    # consistency; it must not be pulled into the tier on the strength of appearing here.
    ("interfaces/components.py", "Component.powerdown"): "MAST_common#45",
    ("interfaces/guiding.py", "GuiderInterface.start_guiding"): "MAST_common#45",
    ("interfaces/guiding.py", "GuiderInterface.stop_guiding"): "MAST_common#45",
    ("interfaces/guiding.py", "GuiderInterface.status"): "MAST_common#45",
    # An exposure verb that MAST_unit#74's sweep missed. Not a one-line addition: PHD2
    # returns a CanonicalResponse from it while the Imager wrapper raises ValueError on a
    # series mismatch, so declaring it is also an invariant-4 fix.
    ("interfaces/imager.py", "ImagerInterface.end_exposure_series"): "MAST_unit#74",
    ("interfaces/imager.py", "ImagerInterface.wait_for_image_ready"): "MAST_common#45",
    ("interfaces/imager.py", "ImagerInterface.wait_for_image_saved"): "MAST_common#45",
    # Its sibling `solve` is properly declared `-> SolvingResult`.
    ("interfaces/solving.py", "SolverInterface.solve_and_correct"): "MAST_common#45",
}


def detect(modules) -> set[astscan.Site]:
    """Undeclared abstract signatures in `modules`, an iterable of (name, parsed tree).

    Parameterized rather than scanning directly so the detector can be exercised over a
    synthetic source below. Every assertion in this file is "no unexpected findings", which a
    broken detector satisfies as perfectly as a clean tree does.
    """
    found = set()
    for module, tree in modules:
        for class_node, function in astscan.methods(tree):
            decorators = astscan.decorators(function)
            if "abstractmethod" not in decorators:
                continue
            # A property setter returning None is correct, and annotating it would be noise.
            if any(decorator.endswith(".setter") for decorator in decorators):
                continue
            if function.returns is None:
                found.add(astscan.Site(module, function.lineno, f"{class_node.name}.{function.name}"))
    return found


def test_no_new_undeclared_abstract_signatures():
    astscan.report(detect(astscan.modules(astscan.COMMON_ROOT)), KNOWN_UNDECLARED, "undeclared abstract signature")


SYNTHETIC = '''
from abc import ABC, abstractmethod

class Widget(ABC):
    @abstractmethod
    def declared(self) -> int:
        """Annotated -- must not be flagged."""

    @abstractmethod
    def undeclared(self):
        """No return annotation -- must be flagged."""

    @property
    @abstractmethod
    def prop(self) -> int:
        """An annotated abstract property."""

    @prop.setter
    @abstractmethod
    def prop(self, value: int):
        """A setter returning None is correct; must not be flagged."""

    def concrete(self):
        """Not abstract -- out of scope."""
'''


def test_the_detector_flags_an_undeclared_signature_and_nothing_else():
    """Proves the check bites, on a source where the right answer is known by construction."""
    found = detect([("synthetic.py", ast.parse(SYNTHETIC))])

    assert {site.detail for site in found} == {"Widget.undeclared"}


def test_the_check_sees_the_abstract_population_at_all():
    """Guard against the scan silently finding nothing -- a locator break would pass otherwise.

    Every assertion above is of the form "no unexpected findings", which a scan over zero
    files satisfies perfectly. `COMMON_ROOT` is derived from an imported package, so a
    layout change cannot make it wrong without making the import wrong too -- but a bad
    exclusion pattern could still empty the file list, and that must fail loudly.
    """
    abstract_methods = [
        (module, function.name)
        for module, tree in astscan.modules(astscan.COMMON_ROOT)
        for _, function in astscan.methods(tree)
        if "abstractmethod" in astscan.decorators(function)
    ]
    assert len(abstract_methods) > 20, f"expected the common ABCs, scanned {astscan.COMMON_ROOT}"
