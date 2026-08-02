"""Phase-tagged logging for the calibration suite.

Every calibration log line is prefixed ``[calibration.<phase>]`` (or plain
``[calibration]`` outside a phase, e.g. the orchestrator's own messages), so a
run is greppable in a log the whole unit is writing to concurrently -- mount,
imager, guider and PHD2 all interleave there.

**Why a filter and not an edit to every call site.** There are hundreds of
``logger.debug(...)`` calls across the suite and the module docstring in
``calibrator`` makes the debug log *the* decision trace, so the prefix has to
hold for all of them, including ones added later. A filter applies to whatever
is logged, forever, and cannot be forgotten.

**Why it must attach per-logger.** ``common.mast_logging.init_log`` sets
``propagate = False`` and gives each logger its own handlers. Filters attached
to a *parent* logger only run for records logged through that parent, so a
filter on ``mast.unit.calibration`` would silently never fire for
``mast.unit.calibration.phases.focuser``. Hence :func:`init_calibration_log`,
which each module calls in place of ``init_log``.

**Why a ContextVar and not an argument.** The phase is ambient: it is set once
when a phase starts and every module logging underneath it -- phases, analysis,
the shared slew helper -- should inherit the tag without threading a parameter
through call signatures that have nothing else to do with logging. Each phase
runs on its own thread (``Calibrator._start``), and a thread starts with a fresh
context, so the value set inside a phase cannot leak into another.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar

from common.mast_logging import init_log

#: The phase currently executing, or None outside any phase.
_current_phase: ContextVar[str | None] = ContextVar("calibration_phase", default=None)


def current_phase() -> str | None:
    """The phase whose tag is being applied, or ``None``."""
    return _current_phase.get()


@contextmanager
def phase_logging(phase: str | None):
    """Tag everything logged in this block with ``[calibration.<phase>]``.

    Restores the previous value on exit -- via the token, not by assuming the
    previous value was ``None`` -- so a nested phase (a phase run under
    ``/calibrate``'s umbrella) leaves the outer tag intact.
    """
    token = _current_phase.set(phase)
    try:
        yield
    finally:
        _current_phase.reset(token)


class PhasePrefixFilter(logging.Filter):
    """Prepend the phase tag to every record passing through."""

    def filter(self, record: logging.LogRecord) -> bool:
        phase = _current_phase.get()
        prefix = f"[calibration.{phase}]" if phase else "[calibration]"
        # Prepend to `msg`, NOT to the formatted message: `record.getMessage()`
        # applies `msg % args` later, and the prefix contains no '%', so
        # %-style args keep working untouched.
        record.msg = f"{prefix} {record.msg}"
        return True  # a prefixer, never a gate


def init_calibration_log(logger: logging.Logger) -> logging.Logger:
    """``init_log`` plus the phase prefix -- what calibration modules call.

    Idempotent: a module re-imported (or a logger shared by two modules) must
    not stack the filter and emit ``[calibration] [calibration] ...``.
    """
    init_log(logger)
    if not any(isinstance(f, PhasePrefixFilter) for f in logger.filters):
        logger.addFilter(PhasePrefixFilter())
    return logger
