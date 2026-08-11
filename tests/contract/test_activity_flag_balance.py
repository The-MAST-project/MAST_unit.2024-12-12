"""Invariant 3, mechanical half: every activity flag that starts, ends (#44).

A component signals "still working" by setting a bit in its `*Activities` IntFlag, and a
consumer detects completion by watching that bit clear. So the contract holds only if the
bits are balanced:

- **started but never ended** -- any waiter hangs forever;
- **ended but never started** -- the clear is dead code, and `abort`'s claim to end the
  component's live activities is false;
- **neither** -- a flag that is declared and does nothing, which reads as a completion
  signal a consumer could rely on.

Static by nature: it is a question about the set of start/end sites, which no single run of
the code answers.

## Two scoping rules, both load-bearing

**Only unit-owned enums.** `common/activities.py` declares the activity enums for the whole
fleet -- `GreatEyesActivities`, `NewtonActivities`, `DeepspecActivities` and
`HighspecActivities` belong to MAST_spec, `ControllerActivities`, `ControlledUnitActivities`,
`PlanActivities` and `BatchActivities` to MAST_control. Scanning the unit tree against all of
them reports every one of those as entirely dead, which is true of this repo and false of the
fleet. A first pass produced exactly that: 60-odd findings, none of them real.

**Zero-valued sentinels are not flags.** Most of these enums open with `Idle = 0`, which
names the absence of activity and is never passed to `start_activity`. Counting it as
"declared and never used" is a false positive in every enum at once.
"""

from __future__ import annotations

import ast

from . import astscan

ACTIVITY_MODULE = "activities.py"

# The activity enums this repo owns. Anything else in `common/activities.py` belongs to
# MAST_spec or MAST_control and must be judged against those trees, not this one.
UNIT_ENUMS = {
    "UnitActivities",
    "MountActivities",
    "CoverActivities",
    "FocuserActivities",
    "StageActivities",
    "ImagerActivities",
}

# Unbalanced flags, keyed to the issue that owns fixing them. #44 recorded four; this check
# finds seven. The three it adds -- StageActivities.Aborting, ImagerActivities.StartingUp and
# UnitActivities.PreGuiding -- were not in any inventory, which is the argument for the check
# over the inventory.
KNOWN_UNBALANCED = {
    # Declared, never set: start_tracking/stop_tracking block instead of flagging.
    ("MountActivities", "Tracking: declared, never started or ended"): "MAST_unit#44",
    # Ended in focuser.abort, started nowhere.
    ("FocuserActivities", "StartingUp: ended, never started"): "MAST_unit#44",
    # Never set; `is_shutting_down` hard-returns False.
    ("StageActivities", "ShuttingDown: declared, never started or ended"): "MAST_unit#44",
    ("StageActivities", "Aborting: declared, never started or ended"): "MAST_unit#44",
    # Set only in an uncalled cooldown(); its ontimer end is commented out. The worst of the
    # seven -- a waiter on this bit never returns.
    ("ImagerActivities", "CoolingDown: started, never ended"): "MAST_unit#44",
    ("ImagerActivities", "StartingUp: declared, never started or ended"): "MAST_unit#44",
    ("UnitActivities", "PreGuiding: declared, never started or ended"): "MAST_unit#44",
    # Became dead upstream on 2026-08-11: `6750f70` dropped the sequence-of-exposures
    # endpoints and the `do_start_sequence_of_exposures` that set this flag, but left the
    # enum member in MAST_common's activities.py. The member should follow the feature out.
    ("UnitActivities", "SequenceOfExposures: declared, never started or ended"): "MAST_unit#44",
}


def enum_members(modules) -> dict[str, set[str]]:
    """Members of each unit-owned activity enum, excluding zero-valued sentinels."""
    members: dict[str, set[str]] = {}
    for module, tree in modules:
        if module != ACTIVITY_MODULE:
            continue
        for class_node in [node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]:
            if class_node.name not in UNIT_ENUMS:
                continue
            names = set()
            for statement in class_node.body:
                if not isinstance(statement, ast.Assign):
                    continue
                # `Idle = 0` names the absence of activity, not an activity.
                if isinstance(statement.value, ast.Constant) and statement.value.value == 0:
                    continue
                names.update(target.id for target in statement.targets if isinstance(target, ast.Name))
            members[class_node.name] = names
    return members


def start_end_sites(members: dict[str, set[str]], modules):
    """Flags passed to start_activity / end_activity anywhere in `modules`."""
    started: dict[str, set[str]] = {enum: set() for enum in members}
    ended: dict[str, set[str]] = {enum: set() for enum in members}
    for _, tree in modules:
        for call in astscan.calls(tree):
            name = astscan.called_name(call)
            if name not in ("start_activity", "end_activity") or not call.args:
                continue
            argument = ast.unparse(call.args[0])
            if "." not in argument:
                continue
            qualifier, _, member = argument.rpartition(".")
            enum = qualifier.rsplit(".", 1)[-1]
            if enum in members:
                (started if name == "start_activity" else ended)[enum].add(member)
    return started, ended


def detect(member_modules, site_modules) -> set[astscan.Site]:
    """Unbalanced flags, given the trees that declare the enums and the trees that use them.

    Parameterized so the detector can be exercised over synthetic sources below: the real
    assertion is "no unexpected findings", which a broken detector satisfies perfectly.
    """
    members = enum_members(member_modules)
    started, ended = start_end_sites(members, site_modules)

    found = set()
    for enum, declared in members.items():
        for member in sorted(declared):
            is_started, is_ended = member in started[enum], member in ended[enum]
            if is_started and not is_ended:
                verdict = "started, never ended"
            elif is_ended and not is_started:
                verdict = "ended, never started"
            elif not is_started and not is_ended:
                verdict = "declared, never started or ended"
            else:
                continue
            # The enum stands in for a module here: an activity enum is shared by a
            # component and its backends (ImagerActivities spans the wrapper, ASCOM, ZWO and
            # PHD2), so the enum is the unit of balance, not any one file.
            found.add(astscan.Site(enum, 0, f"{member}: {verdict}"))
    return found


def test_activity_flags_are_balanced():
    found = detect(astscan.modules(astscan.COMMON_ROOT), astscan.modules(astscan.UNIT_SRC))
    astscan.report(found, KNOWN_UNBALANCED, "activity-flag")


def test_every_unit_enum_was_found():
    """A typo in UNIT_ENUMS would silently drop a component from the check."""
    found = set(enum_members(astscan.modules(astscan.COMMON_ROOT)))
    assert found == UNIT_ENUMS, f"missing from common/activities.py: {sorted(UNIT_ENUMS - found)}"


def test_the_scan_finds_start_and_end_sites():
    """Balance is asserted from call sites; zero sites would make every flag look dead."""
    members = enum_members(astscan.modules(astscan.COMMON_ROOT))
    started, ended = start_end_sites(members, astscan.modules(astscan.UNIT_SRC))
    assert sum(len(flags) for flags in started.values()) > 20
    assert sum(len(flags) for flags in ended.values()) > 20


SYNTHETIC_ENUMS = """
from enum import IntFlag, auto

class MountActivities(IntFlag):
    Idle = 0
    Balanced = auto()
    StartedOnly = auto()
    EndedOnly = auto()
    Untouched = auto()
"""

SYNTHETIC_SITES = """
def work(self):
    self.start_activity(MountActivities.Balanced)
    self.end_activity(MountActivities.Balanced)
    self.start_activity(MountActivities.StartedOnly)
    self.end_activity(MountActivities.EndedOnly)
"""


def test_the_detector_names_each_kind_of_imbalance():
    """All three arms, plus the two cases that must not be flagged (Balanced, Idle)."""
    found = detect(
        [(ACTIVITY_MODULE, ast.parse(SYNTHETIC_ENUMS))],
        [("synthetic.py", ast.parse(SYNTHETIC_SITES))],
    )

    assert {site.detail for site in found} == {
        "StartedOnly: started, never ended",
        "EndedOnly: ended, never started",
        "Untouched: declared, never started or ended",
    }
