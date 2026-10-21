"""`Acquisition._folder_on_the_share`: the folder `post_process` plots, on the shared area.

It was `self.folder.replace(ram.root, shared.root)`. The roots are forward-slashed
("D:/MAST/") and PathMaker builds folders with the native separator, so on Windows the
replace never matched and the plot was always pointed at the ram-disk folder
(plans/filing-areas-design.md 1.4). Built here the way each side really is -- the folder with
`os.path.join`, the roots posix-style -- so the Windows runner reproduces the mismatch.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

import acquisition as acquisition_module
from acquisition import Acquisition
from common.filer import Location


@pytest.fixture
def roots(tmp_path, monkeypatch):
    ram, shared = tmp_path / "ram", tmp_path / "shared"
    fake = SimpleNamespace(
        ram=Location(None, f"{ram.as_posix()}/"),
        shared=Location(None, f"{shared.as_posix()}/"),
    )
    monkeypatch.setattr(acquisition_module, "filer", fake)
    return ram, shared


def _on_the_share(folder: str) -> str:
    return Acquisition._folder_on_the_share(SimpleNamespace(folder=folder))  # type: ignore[arg-type]


def test_a_native_spelling_folder_maps_onto_the_share(roots):
    ram, shared = roots
    folder = os.path.join(str(ram), "2026-10-21", "Acquisitions", "seq=0007,target=12.5,20.2")

    result = _on_the_share(folder)

    expected = os.path.join(str(shared), "2026-10-21", "Acquisitions", "seq=0007,target=12.5,20.2")
    assert os.path.normcase(os.path.normpath(result)) == os.path.normcase(os.path.normpath(expected))


def test_a_posix_spelling_folder_maps_onto_the_share_too(roots):
    ram, shared = roots
    folder = f"{ram.as_posix()}/2026-10-21/Acquisitions/seq=0007"

    result = _on_the_share(folder)

    assert os.path.normpath(result) == os.path.normpath(os.path.join(str(shared), "2026-10-21", "Acquisitions", "seq=0007"))


def test_a_folder_off_the_ram_area_is_returned_unchanged(roots, tmp_path):
    folder = os.path.join(str(tmp_path), "elsewhere", "seq=0001")
    assert _on_the_share(folder) == folder
