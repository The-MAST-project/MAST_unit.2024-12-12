"""Pytest setup for the solver integration test.

``pixel_grid`` is deliberately self-contained (no intra-package imports), so we
put its directory on ``sys.path`` and import it directly, with no MAST runtime
and no heavy ``mastrometry`` dependencies. The pure-math tests live in
``tests/test_pixel_grid.py`` and do not use this file.
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

SOLVE_FIELD = os.environ.get("MAST_SOLVE_FIELD", r"C:/cygwin64/usr/local/astrometry/bin/solve-field")
INDEX_DIR = os.environ.get("MAST_INDEX_DIR", r"D:\mast-indexes")

# The ~90 MB sample frame is intentionally NOT in the repo (it bloats clones and
# git-lfs quota). It lives as a GitHub Release asset on the public MAST_unit repo
# and is downloaded over plain HTTPS into a local cache the first time the
# integration test actually runs. The cache path is git-ignored (root *.fits
# rule), and so is the partial file the download writes first. Override the
# location entirely with MAST_TEST_FITS to point at a frame already on disk (and
# skip the download).
_FITS_CACHE = Path(__file__).resolve().parent / "fixtures" / "full-frame.fits"
_FITS_REPO = "The-MAST-project/MAST_unit.2024-12-12"
_FITS_RELEASE_TAG = "fixtures-v1"
_FITS_ASSET = "full-frame.fits"
_FITS_URL = f"https://github.com/{_FITS_REPO}/releases/download/{_FITS_RELEASE_TAG}/{_FITS_ASSET}"
_FITS_SHA256 = "fd8618de757bb29080ce5ea352b0332257d176caf3b84a57c35af4d26570e526"
_READ_CHUNK = 1 << 20
_DOWNLOAD_TIMEOUT_S = 60


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
    partial = _FITS_CACHE.with_name(f"{_FITS_CACHE.stem}.partial{_FITS_CACHE.suffix}")
    try:
        with urllib.request.urlopen(_FITS_URL, timeout=_DOWNLOAD_TIMEOUT_S) as response, partial.open("wb") as fh:
            shutil.copyfileobj(response, fh, _READ_CHUNK)
    except OSError as exc:
        partial.unlink(missing_ok=True)
        return None, f"downloading {_FITS_URL} failed: {exc} (or set MAST_TEST_FITS to a local frame)"
    if _sha256(partial) != _FITS_SHA256:
        partial.unlink()
        return None, f"{_FITS_URL} failed its sha256 check"
    partial.replace(_FITS_CACHE)
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
