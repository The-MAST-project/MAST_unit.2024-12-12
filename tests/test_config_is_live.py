"""The unit reads its configuration live, and never writes into it.

Both properties are invisible at runtime until they are wrong: a component that snapshots
its configuration keeps working, it just quietly ignores every later change -- which is
the behaviour this repo had for years and which MAST_common's dynamic-configuration work
exists to end. A component that *mutates* the configuration is worse: accessors are
memoized per generation, so an edit is shared with every other reader in the process and
then silently reverted at the next generation, possibly mid-operation.

Two halves. The **source-level** guards below are AST, not imports: `focuser`, `mount`,
`covers` and `stage` bind `win32com` and pyximc at module level, so they are unimportable
anywhere except a unit, and the point is to fail on the day someone reintroduces the
pattern -- which a grep-shaped test does on every platform. The **runtime** guards at the
bottom import the components and read each live attribute across a configuration change,
because a property can still be a property and read the wrong field; they run on a unit
and in CI (windows-latest) and skip elsewhere.
"""

import ast
import importlib
import pathlib
from types import SimpleNamespace

import pytest

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"

#: The names a configuration read goes through. `config` is deliberately absent:
#: `science/sky_quality.py` carries a `config` of hard-coded pydantic defaults that never
#: comes from the database, and it is not what this guard is about.
CONF_NAMES = ("conf", "unit_conf")

#: Components whose configuration must be reached through a property, with the section of
#: `unit_conf` each one exposes. Every one of these was an `__init__` snapshot.
LIVE_CONF_COMPONENTS = {
    "focuser.py": "focuser",
    "covers.py": "covers",
    "mount.py": "mount",
    "stage.py": "stage",
    "imagers/__init__.py": "imager",
    "imagers/ascom.py": "imager",
    "phd2/phd2.py": "phd2",
}

#: Individual configuration-derived attributes that must be properties over `conf`, not
#: values copied out once. Converting each component's `conf` was only half the job:
#: `phd2.settle` was copied field by field into an instance attribute and used for the
#: life of the process (#214), so one component had two configuration sections with
#: opposite reload semantics and nothing at either call site saying which was which.
#:
#: The value is the `unit_conf` path the property has to track. It is a *derivation*, not
#: necessarily a field read: `profile_binning` parses the profile name.
LIVE_ATTRIBUTES = {
    ("focuser.py", "known_as_good_position"): "focuser.known_as_good_position",
    ("imagers/ascom.py", "temp_check_interval"): "imager.temp_check_interval",
    ("phd2/phd2.py", "settling_settings"): "phd2.settle",
    ("phd2/phd2.py", "profile_binning"): "phd2.profile",
    ("phd2/phd2.py", "profile_bpp"): "phd2.profile",
    ("stage.py", "presets"): "stage.presets",
}

#: `self.<attribute> = <something read from the configuration>` sites that are correct as
#: they stand, because what they bind is chosen once when the device is opened and cannot
#: change without reconstructing the component. An operator editing these gets the new
#: value at the next service restart however the code is written, so a property here
#: would advertise a liveness it cannot deliver.
#:
#: Keyed on the attribute rather than on a line, so it survives edits above it. Every
#: entry must be exercised -- see `test_no_unused_construction_time_entries`.
CONSTRUCTION_TIME = {
    ("focuser.py", "_ascom"): "the COM object for the configured ASCOM driver",
    ("mount.py", "_ascom"): "the COM object for the configured ASCOM driver",
    ("imagers/ascom.py", "prog_id"): "the ASCOM driver the camera is bound to",
    ("imagers/__init__.py", "_prog_id"): "the ASCOM driver the backend was constructed with",
    ("phd2/phd2.py", "guiding_verification_timer"): "a RepeatTimer's period, fixed at construction",
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


def assignments(node: ast.AST) -> list[ast.Assign | ast.AnnAssign]:
    """Every assignment under `node`, in source order.

    `ast.walk` is breadth-first, and taint tracking needs the order the reader sees.
    """
    found = [n for n in ast.walk(node) if isinstance(n, (ast.Assign, ast.AnnAssign)) and n.value is not None]
    return sorted(found, key=lambda n: (n.lineno, n.col_offset))


def targets_of(node: ast.Assign | ast.AnnAssign) -> list[ast.AST]:
    if isinstance(node, ast.AnnAssign):
        return [node.target]
    return [element for target in node.targets for element in (target.elts if isinstance(target, ast.Tuple) else [target])]


def reads_configuration(node: ast.AST, tainted: set[str]) -> bool:
    """Does this expression reach the configuration, directly or through a local?

    `self.conf.settle`, `unit_conf.phd2`, `Config().get_unit()`, and anything mentioning a
    local already bound to one of those -- which is the form #214 took: the section went
    into a local first, so a check looking only at `self.conf` walked past it.
    """
    for n in ast.walk(node):
        if isinstance(n, ast.Attribute) and n.attr in CONF_NAMES:
            return True
        if isinstance(n, ast.Name) and (n.id in CONF_NAMES or n.id in tainted):
            return True
        if (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and isinstance(n.func.value, ast.Call)
            and isinstance(n.func.value.func, ast.Name)
            and n.func.value.func.id == "Config"
        ):
            return True
    return False


def configuration_derived_attributes(path: pathlib.Path) -> list[tuple[str, int]]:
    """`self.x = <configuration>` sites in one file, as (attribute, lineno).

    Per function, because a local's taint does not outlive the call that bound it. Only
    `self.<name>` targets are reported: a property is free to build the value in locals,
    which is what `Stage.presets` does, but caching one back onto the instance is the
    same snapshot wherever it is written.
    """
    found: list[tuple[str, int]] = []
    for function in [n for n in ast.walk(parse(path)) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        tainted: set[str] = set()
        for node in assignments(function):
            if not reads_configuration(node.value, tainted):
                continue
            for target in targets_of(node):
                chain = attribute_chain(target)
                if len(chain) == 2 and chain[0] == "self":
                    found.append((chain[1], node.lineno))
                elif len(chain) == 1:
                    tainted.add(chain[0])
    return found


def test_nothing_snapshots_the_configuration():
    """No `self.<anything> = <read from the configuration>` anywhere under src/.

    An assignment is what makes a component stale: it captures one generation's value and
    keeps it for the life of the process. This covers the whole shape, not just
    `self.conf` -- annotated assignments included, which is how `ASCOMImager`'s
    `self.conf: ImagerConfig = ...` survived the first version of this guard.
    """
    offenders = []
    for path in python_files():
        relpath = path.relative_to(SRC).as_posix()
        for attribute, lineno in configuration_derived_attributes(path):
            if (relpath, attribute) in CONSTRUCTION_TIME:
                continue
            offenders.append(f"{relpath}:{lineno} -> self.{attribute} = <configuration>")

    assert not offenders, (
        "configuration must be read live, not snapshotted (add a CONSTRUCTION_TIME entry "
        "with its reason if the binding really cannot change without reconstruction):\n  " + "\n  ".join(offenders)
    )


def test_no_unused_construction_time_entries():
    """An allowlist nothing checks becomes a list of things that used to be true."""
    exercised = {
        (path.relative_to(SRC).as_posix(), attribute)
        for path in python_files()
        for attribute, _ in configuration_derived_attributes(path)
    }

    unused = sorted(set(CONSTRUCTION_TIME) - exercised)

    assert not unused, f"CONSTRUCTION_TIME entries that no longer match any site: {unused}"


def reaches_conf(function: ast.FunctionDef, tree: ast.Module) -> bool:
    """`self.conf` in the body, or in a helper of the same class that the body calls.

    `profile_binning` and `profile_bpp` both parse the profile name, so the read sits in
    the shared parser rather than in either property.
    """
    source = ast.unparse(function)
    if "self.conf" in source:
        return True
    called = {n.func.attr for n in ast.walk(function) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    helpers = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name in called]
    return any("self.conf" in ast.unparse(helper) for helper in helpers)


@pytest.mark.parametrize(("relpath", "attribute"), sorted(LIVE_ATTRIBUTES))
def test_configuration_derived_attribute_is_a_property(relpath, attribute):
    """Each one was an `__init__` snapshot of a single field or section."""
    tree = parse(SRC / relpath)

    properties = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == attribute
        and any(isinstance(d, ast.Name) and d.id == "property" for d in node.decorator_list)
    ]

    assert properties, f"{relpath}: '{attribute}' must be a @property over {LIVE_ATTRIBUTES[(relpath, attribute)]}"
    assert reaches_conf(properties[0], tree), f"{relpath}: '{attribute}' should derive from self.conf"


def test_nothing_writes_into_the_configuration():
    """No assignment into anything reached through `unit_conf`.

    `Config().update_unit()` exists for read-modify-write: it hands the mutator a private
    deep copy. Writing into what `unit_conf` returns instead would change the model every
    other component in the process is reading, and lose the change at the next generation.
    """
    offenders = []
    for path in python_files():
        for node in assignments(parse(path)):
            for target in targets_of(node):
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


# --------------------------------------------------------------------------------------
# The source-level guards above say the shape is right. These say the value actually
# moves: each property is read, the section it derives from is replaced (which is what a
# new configuration generation does), and it is read again.
#
# They import the components, so they run on a unit and in CI (windows-latest) and skip
# elsewhere -- `focuser`, `mount` and `stage` bind win32com and pyximc at module level.
# --------------------------------------------------------------------------------------


def component(module: str, name: str):
    try:
        return getattr(importlib.import_module(module), name)
    except Exception as ex:  # noqa: BLE001 -- any import failure here means "not a unit"
        pytest.skip(f"{module} unimportable here ({ex!r})")


def imager_config(**overrides):
    from common.config.imager import ImagerConfig

    return ImagerConfig(
        **{
            "imager_type": "ascom:ASCOM.ASICamera2.Camera",
            "valid_imager_types": ["ascom", "zwo", "phd2"],
            "pixel_scale_at_bin1": 0.26,
            "format": "raw16",
            "gain": 170,
            **overrides,
        }
    )


def phd2_config(**overrides):
    from common.config.phd2 import PHD2Config

    return PHD2Config(
        **{
            "profile": "PWI4+asi-native,binning=1,bpp=16",
            "settle": {"pixels": 2, "time": 5, "timeout": 40},
            "validation_interval": 0.0,
            **overrides,
        }
    )


def stage_config(**overrides):
    from common.config.stage import StageConfig

    defaults = {"presets": {"sky": 190000, "spec": 321297}, "close_enough": 30, "model": "8MT173-20DCE2"}

    return StageConfig(**{**defaults, **overrides})


def with_unit_conf(instance, attribute: str, section_name: str, section):
    """Give a bare component just enough of a unit to reach one configuration section."""
    unit = SimpleNamespace(unit_conf=SimpleNamespace(**{section_name: section}))
    setattr(instance, attribute, unit)
    return unit


def focuser_case():
    from common.config.focuser import FocuserConfig

    focuser = object.__new__(component("focuser", "Focuser"))
    unit = with_unit_conf(focuser, "unit", "focuser", FocuserConfig(known_as_good_position=12000))

    def regenerate():
        unit.unit_conf.focuser = FocuserConfig(known_as_good_position=4321)

    return focuser, regenerate, 12000, 4321


def ascom_imager_case():
    imager = object.__new__(component("imagers.ascom", "ASCOMImager"))
    imager.parent_imager = SimpleNamespace(unit=None)
    unit = with_unit_conf(imager.parent_imager, "unit", "imager", imager_config(temp_check_interval=15))

    def regenerate():
        unit.unit_conf.imager = imager_config(temp_check_interval=90)

    return imager, regenerate, 15, 90


def phd2_connector_on(**next_generation):
    """A bare connector on the default PHD2 configuration, and the swap to the next."""
    connector = object.__new__(component("phd2.phd2", "PHD2Connector"))
    connector.parent = SimpleNamespace(unit=None)
    unit = with_unit_conf(connector.parent, "unit", "phd2", phd2_config())

    def regenerate():
        unit.unit_conf.phd2 = phd2_config(**next_generation)

    return connector, regenerate


def settle_case():
    """#214: raised 2 -> 4 mid-session on mast01, and never reached PHD2."""
    from common.config.phd2 import PHD2SettleConfig

    settle = PHD2SettleConfig(pixels=2, time=5, timeout=40)
    loosened = settle.model_copy(update={"pixels": 4})
    connector, regenerate = phd2_connector_on(settle=loosened.model_dump())

    return connector, regenerate, settle, loosened


def profile_case(expected_before: int, expected_after: int):
    def case():
        connector, regenerate = phd2_connector_on(profile="PWI4+asi-native,binning=2,bpp=8")
        return connector, regenerate, expected_before, expected_after

    return case


def stage_presets_case():
    from stage import StagePresetPosition

    stage = object.__new__(component("stage", "Stage"))
    stage.min_travel, stage.max_travel = None, None
    unit = with_unit_conf(stage, "unit", "stage", stage_config())
    before = {StagePresetPosition.Sky: 190000, StagePresetPosition.Spec: 321297}

    def regenerate():
        unit.unit_conf.stage = stage_config(presets={"sky": 191000, "spec": 321297})
        stage.min_travel, stage.max_travel = 0, 343544

    after = {
        StagePresetPosition.Sky: 191000,
        StagePresetPosition.Spec: 321297,
        StagePresetPosition.Min: 0,
        StagePresetPosition.Max: 343544,
        StagePresetPosition.Middle: 171772,
    }
    return stage, regenerate, before, after


#: How each `LIVE_ATTRIBUTES` row is driven. Parametrized off the same table, so a row
#: added there without a case here fails rather than going quietly undriven.
RUNTIME_CASES = {
    ("focuser.py", "known_as_good_position"): focuser_case,
    ("imagers/ascom.py", "temp_check_interval"): ascom_imager_case,
    ("phd2/phd2.py", "settling_settings"): settle_case,
    ("phd2/phd2.py", "profile_binning"): profile_case(1, 2),
    ("phd2/phd2.py", "profile_bpp"): profile_case(16, 8),
    ("stage.py", "presets"): stage_presets_case,
}


@pytest.mark.parametrize(("relpath", "attribute"), sorted(LIVE_ATTRIBUTES))
def test_configuration_derived_attribute_tracks_the_configuration(relpath, attribute):
    """A property can still read the wrong field; only reading it across a change says."""
    assert (relpath, attribute) in RUNTIME_CASES, f"no runtime case drives {relpath}:{attribute}"
    instance, regenerate, before, after = RUNTIME_CASES[(relpath, attribute)]()

    assert getattr(instance, attribute) == before

    regenerate()

    assert getattr(instance, attribute) == after


def test_the_service_starts_watching():
    """Nothing refreshes without this call -- the watcher is opt-in by design."""
    assert "Config().start_watching()" in (SRC / "app.py").read_text(encoding="utf-8"), (
        "app.py must call Config().start_watching() or the unit keeps a startup snapshot"
    )
