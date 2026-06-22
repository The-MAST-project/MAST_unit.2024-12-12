"""Shared pytest setup for the solver drift tests.

``pixel_grid`` is deliberately self-contained (no intra-package imports), so we
put its directory on ``sys.path`` and import it directly. That keeps the
pure-math tests importable on any machine, with no MAST runtime, no astrometry.net,
and no heavy ``mastrometry`` dependencies.
"""

import os
import shutil
import sys
from pathlib import Path

import pytest

SOLVERS_DIR = Path(__file__).resolve().parents[1]  # .../src/solvers
if str(SOLVERS_DIR) not in sys.path:
    sys.path.insert(0, str(SOLVERS_DIR))


# --- integration-test environment (astrometry.net) ---------------------------
#
# The integration test re-runs the equivalence study and needs a real solve-field,
# index files, and a sample full-frame FITS. Point it at your machine via env vars;
# otherwise the test is skipped. Defaults match the dev unit.

SOLVE_FIELD = os.environ.get(
    "MAST_SOLVE_FIELD", r"C:/cygwin64/usr/local/astrometry/bin/solve-field"
)
INDEX_DIR = os.environ.get("MAST_INDEX_DIR", r"D:\mast-indexes")
TEST_FITS = os.environ.get("MAST_TEST_FITS", r"C:\MAST\full-frame.fits")


def astrometry_available() -> tuple[bool, str]:
    """Return (ok, reason) describing whether the integration test can run."""
    if not Path(SOLVE_FIELD).exists() and shutil.which(SOLVE_FIELD) is None:
        return False, f"solve-field not found at {SOLVE_FIELD} (set MAST_SOLVE_FIELD)"
    if not Path(INDEX_DIR).exists():
        return False, f"index dir not found at {INDEX_DIR} (set MAST_INDEX_DIR)"
    if not Path(TEST_FITS).exists():
        return False, f"sample FITS not found at {TEST_FITS} (set MAST_TEST_FITS)"
    return True, ""


@pytest.fixture(scope="session")
def astrometry_env():
    ok, reason = astrometry_available()
    if not ok:
        pytest.skip(reason)
    return {"solve_field": SOLVE_FIELD, "index_dir": INDEX_DIR, "test_fits": TEST_FITS}
