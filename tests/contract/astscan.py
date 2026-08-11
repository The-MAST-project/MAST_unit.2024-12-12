"""Shared AST plumbing for the static contract checks.

These checks are static on purpose: they answer questions about the *shape* of the source
(is this abstract method annotated, does this flag ever clear, is this thread target named
for what dispatches it) which no amount of running the code reveals, and they need neither
Windows, hardware, Mongo nor an app fixture. That is what makes them the first tranche of
#52 rather than the last.

The one thing they cannot do is see through a call: `Filer(logger)` is textually
indistinguishable from any other constructor, so a static pass cannot tell that
constructing one probes the filesystem. Checks that need *that* belong to the dynamic
harness proposed in #118, not here.
"""

from __future__ import annotations

import ast
import pathlib
from dataclasses import dataclass

import common

# `common` is a sibling clone, not a subdirectory of this repo (#94): on a unit it is
# reached through the venv's `mast.pth`, and in CI through two side-by-side checkouts with
# the workspace root on PYTHONPATH. Asking the imported package where it lives is the only
# locator that holds in every one of those layouts.
COMMON_ROOT = pathlib.Path(common.__file__).resolve().parent
UNIT_SRC = pathlib.Path(__file__).resolve().parents[2] / "src"

# Vendored third-party trees. Their contents are nobody's contract to keep -- the ximc SDK
# alone is 903 of this repo's 968 tracked Python files (see #119) -- and a rule about our
# conventions applied to a vendor drop produces noise, not findings.
VENDORED = {"Standa", "PlaneWave", "PlateSolveSimulator"}

# `src/common` is an *untracked* sibling clone that happens to sit inside `src/` in a
# developer checkout; in CI the sibling is outside the repo entirely. Excluding the path
# component keeps a unit-tree scan from silently scanning `common` a second time and
# reporting its findings as the unit's -- the same trap that put a wrong claim about
# MAST_spec and MAST_control into #118 before it was caught.
#
# Matched against the path **relative to the scan root**, not the absolute path. Absolute
# matching looked equivalent and was not: on a developer machine `COMMON_ROOT` is itself
# `<repo>/src/common`, so an absolute `common` exclusion emptied the common scan entirely
# and every "no unexpected findings" assertion passed over zero files.
EXCLUDED_PARTS = VENDORED | {"__pycache__", "common", "tests", ".venv", "venv"}


@dataclass(frozen=True)
class Site:
    """One located finding. Carries no line number in equality -- see `key`."""

    module: str
    lineno: int
    detail: str

    @property
    def key(self) -> tuple[str, str]:
        """The identity a known-violations list is keyed on: module and subject, no line.

        Line numbers drift with every unrelated edit above them. #81's inventory recorded
        `zwo.py:148` and `imagers/ascom.py:806`; by the time this check was written they
        were 151 and 812, with nothing about the dispatch sites having changed. A list
        keyed on lines would have to be re-verified on every merge and would rot silently
        between them.
        """
        return (self.module, self.detail)

    def __str__(self) -> str:
        return f"{self.module}:{self.lineno} {self.detail}"


def python_files(root: pathlib.Path) -> list[pathlib.Path]:
    return [path for path in sorted(root.rglob("*.py")) if not set(path.relative_to(root).parts) & EXCLUDED_PARTS]


def modules(root: pathlib.Path):
    """Yield (relative-path-as-str, parsed-tree) for every scannable module under `root`."""
    for path in python_files(root):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:  # a vendored or generated file we do not own
            continue
        yield str(path.relative_to(root)).replace("\\", "/"), tree


def decorators(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    return [ast.unparse(decorator) for decorator in node.decorator_list]


def methods(tree: ast.Module):
    """Yield (class_node, function_node) for every method directly on a class body."""
    for class_node in [node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]:
        for statement in class_node.body:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield class_node, statement


def functions(tree: ast.Module):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def calls(tree: ast.Module):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            yield node


def called_name(node: ast.Call) -> str:
    """The bare name being called: `self.foo.bar(...)` -> `bar`."""
    return ast.unparse(node.func).rsplit(".", 1)[-1]


def report(found: set[Site], known: dict[tuple[str, str], str], subject: str) -> None:
    """Assert `found` is exactly the known-violation population, and explain any drift.

    Set equality rather than one `xfail` per item, deliberately. An `xfail` catches a new
    violation but not a *stale* marker: an item fixed elsewhere leaves an `xpass` that is
    easy to configure into silence, and the list then quietly encodes whatever was true on
    the day it was written -- which is the failure mode MAST_common#45 calls out about
    hand-maintained lists. Checking both directions means the list cannot drift from the
    tree without a red test, and an issue landing removes its entry in the same PR.
    """
    found_keys = {site.key for site in found}
    by_key = {site.key: site for site in found}

    new = sorted(found_keys - set(known))
    fixed = sorted(set(known) - found_keys)

    messages = []
    if new:
        messages.append(
            f"{len(new)} new {subject} violation(s) -- fix them, or add to the known list with the issue that owns them:\n"
            + "\n".join(f"    {by_key[key]}" for key in new)
        )
    if fixed:
        messages.append(
            f"{len(fixed)} known {subject} violation(s) no longer present -- remove from the known list:\n"
            + "\n".join(f"    {key[0]}: {key[1]}  (was {known[key]})" for key in fixed)
        )
    assert not messages, "\n\n".join(messages)
