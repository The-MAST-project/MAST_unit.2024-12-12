"""Shared pytest setup for the solver drift tests.

``pixel_grid`` is deliberately self-contained (no intra-package imports), so we
put its directory on ``sys.path`` and import it directly. That keeps the
pure-math tests importable on any machine, with no MAST runtime, no astrometry.net,
and no heavy ``mastrometry`` dependencies.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import urllib.request
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

# The ~90 MB sample frame is intentionally NOT in the repo (it bloats clones and
# git-lfs quota). It lives as a GitHub Release asset and is fetched on demand into
# a local cache the first time the integration test actually runs. The cache path
# is git-ignored (root *.fits rule). Override the location entirely with
# MAST_TEST_FITS to point at a frame already on disk.
_FITS_CACHE = Path(__file__).resolve().parent / "fixtures" / "full-frame.fits"
_FITS_URL = (
    "https://github.com/The-MAST-project/MAST_unit.2024-12-12/"
    "releases/download/fixtures-v1/full-frame.fits"
)
_FITS_SHA256 = "fd8618de757bb29080ce5ea352b0332257d176caf3b84a57c35af4d26570e526"
_READ_CHUNK = 1 << 20


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(_READ_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _resolve_test_fits() -> tuple[Path | None, str]:
    """Resolve the sample FITS, downloading the release asset on first use.

    Returns ``(path, "")`` when usable, or ``(None, reason)`` when it cannot be
    obtained (the integration test then skips with ``reason``).
    """
    override = os.environ.get("MAST_TEST_FITS")
    if override:
        path = Path(override)
        if path.exists():
            return path, ""
        return None, f"MAST_TEST_FITS={override} not found"

    if _FITS_CACHE.exists() and _sha256(_FITS_CACHE) == _FITS_SHA256:
        return _FITS_CACHE, ""

    _FITS_CACHE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _FITS_CACHE.with_name(_FITS_CACHE.name + ".part")
    try:
        urllib.request.urlretrieve(_FITS_URL, tmp)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        return None, f"could not download sample FITS from {_FITS_URL}: {exc} (or set MAST_TEST_FITS)"
    if _sha256(tmp) != _FITS_SHA256:
        tmp.unlink(missing_ok=True)
        return None, f"downloaded sample FITS from {_FITS_URL} failed its sha256 check"
    tmp.replace(_FITS_CACHE)
    return _FITS_CACHE, ""


@pytest.fixture(scope="session")
def astrometry_env():
    if not Path(SOLVE_FIELD).exists() and shutil.which(SOLVE_FIELD) is None:
        pytest.skip(f"solve-field not found at {SOLVE_FIELD} (set MAST_SOLVE_FIELD)")
    if not Path(INDEX_DIR).exists():
        pytest.skip(f"index dir not found at {INDEX_DIR} (set MAST_INDEX_DIR)")
    test_fits, reason = _resolve_test_fits()
    if test_fits is None:
        pytest.skip(reason)
    return {
        "solve_field": SOLVE_FIELD,
        "index_dir": INDEX_DIR,
        "test_fits": str(test_fits),
    }
