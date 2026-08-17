"""Every field a status model declares must actually be passed when it is built.

`MountStatus` declared `tracking`, `slewing` and `dec_j2000_degs`, and `Mount.status()`
never passed any of them -- so they silently reported their defaults. `/mount/status`
answered `tracking: false` while PWI4 said the mount was tracking, and `dec_j2000_degs`
was `null` on every reading. Nothing outside the unit could tell whether the mount was
tracking, and the payload contradicted itself: `activities_verbal: ["Moving"]` with the
Tracking bit set, beside `tracking: false`.

The values were already being fetched -- `status()` reads `st.mount.is_tracking` to
integrate the activity flags -- and then dropped. That is what makes this class of bug
invisible: nothing raises, nothing logs, the field is simply the default forever.

A behavioural test would not have caught it without a live mount. This one is static: it
compares each model's declared fields against the keyword arguments actually supplied at
its construction site. It needs no hardware and no PWI4.
"""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

import pytest

from common.models.statuses import CoverStatus, MountStatus, StageStatus

SRC = Path(__file__).resolve().parent.parent / "src"

#: model -> (module under src/, the method that builds it)
CONSTRUCTION_SITES = [
    (MountStatus, "mount.py", "status"),
    (CoverStatus, "covers.py", "status"),
    (StageStatus, "stage.py", "status"),
]

#: Fields supplied by `**something.model_dump()` splats rather than by name. Those come
#: from the base models (PowerStatus, ComponentStatus, AscomStatus) and are covered by
#: their own construction, not by the site under test.
SPLAT_SOURCED = {
    "type",
    "detected",
    "operational",
    "why_not_operational",
    "connected",
    "activities",
    "activities_verbal",
    "was_shut_down",
    "powered",
    "ascom",
}


def _keywords_passed(module: str, method: str, model_name: str) -> set[str]:
    """The keyword arguments given to `model_name(...)` inside `method`, parsed statically."""
    tree = ast.parse((SRC / module).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == method:
            for call in ast.walk(node):
                if isinstance(call, ast.Call) and getattr(call.func, "id", None) == model_name:
                    return {kw.arg for kw in call.keywords if kw.arg is not None}
    raise AssertionError(f"no {model_name}(...) call found in {module}:{method}()")


@pytest.mark.parametrize(("model", "module", "method"), CONSTRUCTION_SITES)
def test_every_declared_field_is_passed(model, module, method):
    declared = set(model.model_fields) - SPLAT_SOURCED
    passed = _keywords_passed(module, method, model.__name__)

    missing = sorted(declared - passed)

    assert not missing, (
        f"{module}:{method}() builds {model.__name__} without passing {missing}. "
        f"Those fields will silently report their model defaults -- which is how "
        f"/mount/status reported tracking=false while the mount was tracking."
    )


def test_the_regression_this_was_written_for():
    """Named explicitly so a revert says what broke, not just 'a field is missing'."""
    passed = _keywords_passed("mount.py", "status", "MountStatus")

    for field in ("tracking", "slewing", "dec_j2000_degs"):
        assert field in passed, f"MountStatus built without {field!r}"


def test_the_fetched_value_is_the_one_passed():
    """`status()` must pass PWI4's reading, not a constant.

    The bug's shape was that `st.mount.is_tracking` was read for the activity bits and
    then not used for the field, so a fix that passed `tracking=False` would satisfy the
    test above while changing nothing.
    """
    source = inspect.getsource(__import__("mount").Mount.status)
    body = source.split("return MountStatus(")[1]

    for field, expected in (
        ("tracking", "is_tracking"),
        ("slewing", "is_slewing"),
        ("dec_j2000_degs", "dec_j2000_degs"),
    ):
        match = re.search(rf"^\s*{field}=(.+?),\s*(?:#.*)?$", body, re.MULTILINE)
        assert match, f"{field} not passed"
        assert expected in match.group(1), (
            f"{field} is passed as {match.group(1).strip()!r}, which does not read PWI4's {expected}"
        )
