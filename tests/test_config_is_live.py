"""The unit reads its configuration live, and never writes into it.

Both properties are invisible at runtime until they are wrong: a component that snapshots
its configuration keeps working, it just quietly ignores every later change -- which is
the behaviour this repo had for years and which MAST_common's dynamic-configuration work
exists to end. A component that *mutates* the configuration is worse: accessors are
memoized per generation, so an edit is shared with every other reader in the process and
then silently reverted at the next generation, possibly mid-operation.

Checked at the source level rather than by importing: `focuser`, `mount` and `covers`
import `win32com`, so they are unimportable anywhere except a unit. The point is to fail
on the day someone reintroduces the pattern, and a grep-shaped test does that on every
platform.
"""

import ast
import pathlib

import pytest

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"

#: Components whose configuration must be reached through a property, with the section of
#: `unit_conf` each one exposes. Every one of these was an `__init__` snapshot.
LIVE_CONF_COMPONENTS = {
    "focuser.py": "focuser",
    "covers.py": "covers",
    "mount.py": "mount",
    "stage.py": "stage",
    "imagers/__init__.py": "imager",
    "phd2/phd2.py": "phd2",
}


def python_files():
    return [p for p in SRC.rglob("*.py") if "Standa" not in p.parts and "PlaneWave" not in p.parts]


def parse(path: pathlib.Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def attribute_chain(node: ast.AST) -> list[str]:
    """['self', 'unit', 'unit_conf', 'focuser'] for `self.unit.unit_conf.focuser`."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return list(reversed(parts))


@pytest.mark.parametrize("relpath,section", sorted(LIVE_CONF_COMPONENTS.items()))
def test_component_conf_is_a_property(relpath, section):
    """`conf` must be a property delegating to `unit_conf`, not an attribute."""
    tree = parse(SRC / relpath)

    properties = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "conf"
        and any(isinstance(d, ast.Name) and d.id == "property" for d in node.decorator_list)
    ]
    assert properties, f"{relpath}: 'conf' must be a @property reading unit_conf.{section}"

    source = ast.unparse(properties[0])
    assert f"unit_conf.{section}" in source, f"{relpath}: conf property should return unit_conf.{section}"


def test_nothing_snapshots_the_configuration():
    """No `self.conf = ...` / `self.unit_conf = ...` anywhere under src/.

    An assignment is what makes a component stale: it captures one generation's model and
    keeps it for the life of the process.
    """
    offenders = []
    for path in python_files():
        for node in ast.walk(parse(path)):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                chain = attribute_chain(target)
                if len(chain) == 2 and chain[0] == "self" and chain[1] in ("conf", "unit_conf"):
                    offenders.append(f"{path.relative_to(SRC)}:{node.lineno} -> self.{chain[1]} = ...")

    assert not offenders, "configuration must be read live, not snapshotted:\n  " + "\n  ".join(offenders)


def test_nothing_writes_into_the_configuration():
    """No assignment into anything reached through `unit_conf`.

    `Config().update_unit()` exists for read-modify-write: it hands the mutator a private
    deep copy. Writing into what `unit_conf` returns instead would change the model every
    other component in the process is reading, and lose the change at the next generation.
    """
    offenders = []
    for path in python_files():
        for node in ast.walk(parse(path)):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                chain = attribute_chain(target)
                if "unit_conf" in chain and chain[-1] != "unit_conf":
                    offenders.append(f"{path.relative_to(SRC)}:{node.lineno} -> {'.'.join(chain)} = ...")

    assert not offenders, "use Config().update_unit() instead of writing into unit_conf:\n  " + "\n  ".join(offenders)


def test_unit_exposes_unit_conf_as_a_property():
    tree = parse(SRC / "unit.py")
    unit_class = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "Unit")

    for name in ("unit_conf", "autofocus_max_tolerance"):
        found = [
            n
            for n in unit_class.body
            if isinstance(n, ast.FunctionDef)
            and n.name == name
            and any(isinstance(d, ast.Name) and d.id == "property" for d in n.decorator_list)
        ]
        assert found, f"Unit.{name} must be a @property so it tracks configuration changes"


def test_the_service_starts_watching():
    """Nothing refreshes without this call -- the watcher is opt-in by design."""
    assert "Config().start_watching()" in (SRC / "app.py").read_text(encoding="utf-8"), (
        "app.py must call Config().start_watching() or the unit keeps a startup snapshot"
    )
