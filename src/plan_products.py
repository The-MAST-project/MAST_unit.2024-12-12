"""Telling control where a plan's products are (plans/control_acquisition_design.md 5.10, 6.5).

A unit endpoint that a plan execution drives -- `start_acquisition_and_guiding`,
`focus_on_star` -- takes an optional `plan_ulid`. With it, as soon as the unit has created its
products folder, it writes the plan's ULID into a `plan_id` file there, and tells control which
stage the folder belongs to and where it is relative to this machine's folder on the share;
control links it into the execution's products folder. Without it (an operator's call)
nothing is written or sent.

It replaces the notifications `execute_assignment` sent, and that route with them.
"""

from __future__ import annotations

from common.filer import Filer
from common.mast_logging import get_logger
from common.models.assignments import AssignmentNotification
from common.models.plans import PlanStage, write_plan_id_file
from common.notifications import Notifier

logger = get_logger(__name__)

#: What `shared_subpath` said for each stage before `stage` existed. Still sent, so a
#: control that predates `stage` names its links as it did.
LEGACY_SUBPATH = {
    PlanStage.Autofocus: "focus_on_star",
    PlanStage.Acquisition: "acquisition",
}


def announce_plan_products(plan_ulid: str | None, stage: PlanStage, folder: str | None) -> bool:
    """Label ``folder`` with the plan and tell control where it is; True if notified.

    Never raises: neither may fail the operation it announces. Nothing without a ULID or a
    folder. The notification also needs a folder under one of the filing areas -- a path
    the share does not mirror means nothing to control -- but the label is written anyway.
    """
    if plan_ulid is None or folder is None:
        return False
    write_plan_id_file(folder, plan_ulid)
    try:
        _area, relative = Filer().area_of(folder)
    except ValueError:
        logger.warning(f"plan {plan_ulid}: {stage} products in '{folder}' are under no filing area; not reported")
        return False
    Notifier().assignment_notification(
        AssignmentNotification(
            assignment_id=plan_ulid,
            state="in-progress",
            # Relative to this machine's folder on the share: control joins it with the
            # producer's hostname, in its own spelling of the share (MAST_spec#39).
            shared_top=relative,
            stage=stage,
            shared_subpath=LEGACY_SUBPATH.get(stage),
        )
    )
    return True
