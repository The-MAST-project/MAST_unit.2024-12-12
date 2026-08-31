"""A unit's failure reasons are reported in full, not truncated at the first kind (#83).

`Unit.why_not_operational` returned `self._init_errors` and stopped there, so a unit that
failed to load its configuration *and* had a disconnected mount *and* had a stage off its
preset reported the configuration error alone. The operator fixed it, restarted, and met the
next reason -- one round trip per reason, on a machine whose whole surface for this is a
single `/unit/status` read.

`operational`'s own early return is a different question and is asserted here too: an init
error genuinely is decisive there, because `all()` over a component set that is missing its
failed members answers True for a unit that failed to build half of itself.

Order is part of the contract, and was not: the components came out of a `set`, so the reason
list reordered between processes -- caught by this file's first CI run, which passed on macOS
and failed on Windows on exactly that. An operator reads the list top to bottom and anything
diffing two `/unit/status` payloads compares it element-wise, so `_operational_components`
answers a list and the order is asserted below.

Needs no hardware: `why_not_operational` reads four attributes, and the components it walks
only have to answer `why_not_operational` themselves.
"""

from __future__ import annotations

from unit import Unit


class _Component:
    """Stands in for a built component that has its own reasons to offer."""

    def __init__(self, reasons):
        self._reasons = list(reasons)

    @property
    def why_not_operational(self):
        return list(self._reasons)

    @property
    def operational(self):
        return not self._reasons


def _unit(init_errors=(), components=(), unit_name=None):
    u = object.__new__(Unit)
    u._init_errors = list(init_errors)
    u.components = list(components)
    u.covers = None
    u.unit_conf = None if unit_name is None else _Conf(unit_name)
    return u


class _Conf:
    def __init__(self, name):
        self.name = name


def test_init_errors_do_not_hide_the_components_reasons():
    u = _unit(
        init_errors=["component 'covers' failed to initialize: Invalid class string"],
        components=[_Component(["mount: not connected"]), _Component(["stage: not at a preset"])],
    )

    assert u.why_not_operational == [
        "component 'covers' failed to initialize: Invalid class string",
        "mount: not connected",
        "stage: not at a preset",
    ]


def test_init_errors_come_first():
    u = _unit(init_errors=["a", "b"], components=[_Component(["c"])])

    assert u.why_not_operational[:2] == ["a", "b"]


def test_the_component_order_is_the_declaration_order():
    """Not a `set`: the same unit must produce the same list in every process."""
    first, second, third = _Component(["1"]), _Component(["2"]), _Component(["3"])
    u = _unit(components=[first, second, third])

    assert u.why_not_operational == ["1", "2", "3"]
    assert u._operational_components == [first, second, third]


def test_no_init_errors_reports_only_the_components():
    u = _unit(components=[_Component(["mount: not connected"])])

    assert u.why_not_operational == ["mount: not connected"]


def test_a_healthy_unit_has_no_reasons():
    assert _unit(components=[_Component([])]).why_not_operational == []


def test_an_init_error_alone_is_still_reported():
    u = _unit(init_errors=["unit configuration could not be loaded"], components=[_Component([])])

    assert u.why_not_operational == ["unit configuration could not be loaded"]


def test_operational_still_short_circuits_on_an_init_error():
    """The asymmetry is deliberate -- see the comment on `why_not_operational`."""
    u = _unit(init_errors=["component 'mount' failed to initialize: boom"], components=[_Component([])])

    assert u.operational is False
    assert u.why_not_operational == ["component 'mount' failed to initialize: boom"]


def test_the_mastw_covers_carve_out_survives():
    covers = _Component(["covers: not open"])
    u = _unit(components=[covers, _Component(["mount: not connected"])], unit_name="MASTW")
    u.covers = covers

    assert u.why_not_operational == ["mount: not connected"]
