"""The solvers' indexes and scratch, against the RAM filing area (filing-areas plan, item 9).

The indexes live on the RAM disk itself and never on its fallback; the scratch follows the
RAM area's current root. `MastrometryDotNet` is constructed here because nothing else in
the suite did, which is how it came to raise AttributeError on `Location.drive` after
MAST_common#183 removed it, with every check green.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import common.filer as filer_module
import solvers.ram_area as ram_area
from common.filer import Filer, FilingArea, Location
from common.testing.filer import DiskUsage, reset_filing_areas
from solvers.astrometry_dot_net import cygwin_to_win, win_to_cygwin, win_to_wsl
from solvers.ram_area import index_dir, missing_indexes, scratch_dir


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    monkeypatch.setattr(filer_module, "_probe_cache", {})
    monkeypatch.setattr(filer_module, "_probe_threads", {})
    monkeypatch.setattr(Filer, "MONITOR_INTERVAL_SECONDS", 3600.0)
    monkeypatch.setattr(Filer, "_request_ramdisk_restart", lambda self: None)
    reset_filing_areas()
    yield
    reset_filing_areas()


def _demote_ram():
    Filer().report_failure(FilingArea.RAM, "x", OSError(errno.ENOSPC, "No space left on device"))


class TestTheIndexes:
    def test_beside_the_ram_disks_root_on_its_drive(self, monkeypatch):
        monkeypatch.setattr(ram_area, "Filer", lambda: SimpleNamespace(ram=Location("D:/MAST/")))
        assert index_dir().as_posix() == "D:/mast-indexes"

    def test_never_on_the_fallback(self):
        expected = index_dir()
        _demote_ram()
        assert index_dir() == expected

    def test_missing_is_said_plainly(self, tmp_path):
        assert missing_indexes(tmp_path) is None
        why = missing_indexes(tmp_path / "mast-indexes")
        assert "astrometry indexes are missing" in why

    def test_missing_with_the_ram_disk_on_its_fallback_says_that_too(self, tmp_path):
        _demote_ram()
        assert "on its fallback" in missing_indexes(tmp_path / "mast-indexes")


class TestTheScratch:
    def test_on_the_ram_disk(self):
        folder = scratch_dir("tmp_test1")
        assert folder.as_posix().startswith(Filer().ram.root)
        assert folder.is_dir()

    def test_on_the_ram_fallback_while_the_disk_is_demoted(self):
        _demote_ram()
        folder = scratch_dir("tmp_test2")
        assert folder.as_posix().startswith(Filer().ram_fallback.root)

    def test_nowhere_raises_enospc(self, monkeypatch):
        monkeypatch.setattr(filer_module, "_disk_usage", lambda path: DiskUsage(total=100, used=95, free=5))
        _demote_ram()
        with pytest.raises(OSError) as raised:
            scratch_dir("tmp_test3")
        assert raised.value.errno == errno.ENOSPC


class TestPathConversions:
    @pytest.mark.parametrize(
        "windows, cygwin",
        [
            ("D:/MAST/tmp/x", "/cygdrive/d/MAST/tmp/x"),
            ("D:\\MAST\\tmp\\x", "/cygdrive/d/MAST/tmp/x"),
            ("C:\\MAST\\ram-fallback\\tmp\\x", "/cygdrive/c/MAST/ram-fallback/tmp/x"),
            ("e:/data", "/cygdrive/e/data"),
            ("/usr/local/astrometry/data", "/usr/local/astrometry/data"),
        ],
    )
    def test_win_to_cygwin(self, windows, cygwin):
        assert win_to_cygwin(windows) == cygwin

    @pytest.mark.parametrize("cygwin, windows", [("/cygdrive/d/MAST/x", "D:/MAST/x"), ("/cygdrive/c/MAST", "C:/MAST")])
    def test_cygwin_to_win(self, cygwin, windows):
        assert cygwin_to_win(cygwin) == windows

    def test_win_to_wsl_converts_d_too(self):
        assert win_to_wsl("D:\\MAST\\tmp") == "/mnt/d/MAST/tmp"


class TestMastrometryIsConstructed:
    def test_without_the_indexes_it_says_so(self):
        from solvers.mastrometry import MastrometryDotNet

        index = index_dir()
        if index.exists():
            pytest.skip("the suite's layout already has an index folder")
        with pytest.raises(Exception, match="astrometry indexes are missing"):
            MastrometryDotNet()

    def test_with_the_indexes_it_is_built(self):
        from solvers.mastrometry import MastrometryDotNet

        index = index_dir()
        index.mkdir(parents=True, exist_ok=True)
        try:
            assert Path(MastrometryDotNet().index_dir) == index
        finally:
            os.rmdir(index)
