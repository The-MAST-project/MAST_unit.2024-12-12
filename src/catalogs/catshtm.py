"""Cone search over a catsHTM catalog (HTM-indexed HDF5, as on ``\\\\last0\\catalogs``).

catsHTM (Soumagnac & Ofek 2018) splits the sky into Hierarchical Triangular Mesh
trixels and stores each leaf trixel's sources as one HDF5 dataset.  A catalog
``<name>`` is three kinds of file in one folder:

``<name>_htm.hdf5``
    The index: dataset ``<name>_HTM``, stored (13, n_trixels) -- MATLAB wrote it
    column-major, so it is transposed on load.  Columns: Level, Father, Son1-4,
    Poles1Lon, Poles1Lat, Poles2Lon, Poles2Lat, Poles3Lon, Poles3Lat (radians),
    Nsrc.  Trixel IDs are **1-based**: ID = row index + 1, and the Father/Son
    columns hold IDs.  The roots (the 8 level-0 trixels) have no father (NaN).
``<name>_htmColCell.mat``
    The column names (``ColCell``) and units (``ColUnits``) of the data
    datasets.  MATLAB v5 for GAIA DR3, read with ``scipy.io.loadmat``; a v7.3
    file is HDF5 and is read with h5py instead.
``<name>_htm_NNNNNN.hdf5``
    The data: trixel ``ID`` lives in file ``floor(ID / 100) * 100`` as dataset
    ``htm_<ID:06d>``, stored (n_columns, n_sources), again transposed.  Each also
    has an ``htm_<ID:06d>_Ind`` companion, which this reader does not need.

The search descends from the roots, keeping a trixel when the point lies within
``pi/2 + radius`` of all three of its edge poles -- catsHTM's own conservative
test, which can over-select but never misses a trixel the cone touches -- and
then filters the sources by exact angular distance.  Verified complete against
the GAIA DR3 copy on ``\\\\last0`` (2026-10-11): a 0.5 deg cone equals a 2 deg
cone cut to 0.5 deg, at the zenith, near the pole and across RA = 0.

Positions in the catalog are in radians; :meth:`CatsHtm.cone_search` returns
every column in the catalog's own units (see :meth:`CatsHtm.units`).

``h5py`` is imported on first use, not at module import, so a unit without it
fails only the feature that reads a catalog, not at startup.
"""

from __future__ import annotations

import functools
import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np

logger = logging.getLogger("mast.unit." + __name__)

#: Index columns, after the transpose.
_LEVEL, _FATHER, _SONS, _POLES, _NSRC = 0, 1, slice(2, 6), slice(6, 12), 12


class CatalogError(Exception):
    """The catalog cannot be read: share unreachable, files missing, h5py absent."""


def _h5py():
    try:
        import h5py  # noqa: PLC0415 -- deliberately lazy, see the module docstring
    except ImportError as ex:
        raise CatalogError("h5py is not installed in this environment; it is needed to read catsHTM catalogs") from ex
    return h5py


def angular_distance(lon1, lat1, lon2, lat2):
    """Great-circle distance in radians (haversine: accurate at small separations)."""
    s = np.sin((lat2 - lat1) / 2.0) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2.0) ** 2
    return 2.0 * np.arcsin(np.sqrt(np.clip(s, 0.0, 1.0)))


@dataclass
class _Index:
    sons: np.ndarray  # (n, 4) int, 0 = no son; IDs are 1-based
    poles_lon: np.ndarray  # (n, 3) radians
    poles_lat: np.ndarray  # (n, 3) radians
    nsrc: np.ndarray  # (n,) float; NaN above the leaf level
    roots: np.ndarray  # 0-based rows of the trixels with no father


@functools.lru_cache(maxsize=4)
def _load_index(root: str, name: str) -> _Index:
    """Read and cache the trixel index (36 MB for GAIA DR3; ~0.4 s over SMB)."""
    h5py = _h5py()
    path = os.path.join(root, f"{name}_htm.hdf5")
    try:
        with h5py.File(path, "r") as f:
            htm = np.asarray(f[f"{name}_HTM"][()], dtype=float).T
    except (OSError, KeyError) as ex:
        raise CatalogError(f"cannot read catsHTM index '{path}': {ex}") from ex
    sons = np.nan_to_num(htm[:, _SONS], nan=0.0).astype(np.int64)
    poles = htm[:, _POLES]
    roots = np.flatnonzero(~np.isfinite(htm[:, _FATHER]) | (htm[:, _FATHER] <= 0))
    logger.info(f"catsHTM index '{name}' loaded: {len(htm)} trixels, {len(roots)} roots")
    return _Index(sons=sons, poles_lon=poles[:, 0::2], poles_lat=poles[:, 1::2], nsrc=htm[:, _NSRC], roots=roots)


def _cell_strings(cell) -> list[str]:
    return [str(np.asarray(c).squeeze()) for c in np.ravel(cell)]


@functools.lru_cache(maxsize=4)
def _load_columns(root: str, name: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``(names, units)`` from ``<name>_htmColCell.mat``, v5 or v7.3."""
    path = os.path.join(root, f"{name}_htmColCell.mat")
    import scipy.io  # noqa: PLC0415 -- only needed here

    try:
        mat = scipy.io.loadmat(path)
        names = _cell_strings(mat["ColCell"])
        units = _cell_strings(mat["ColUnits"]) if "ColUnits" in mat else [""] * len(names)
    except NotImplementedError:  # v7.3: an HDF5 file holding object references to uint16 strings
        h5py = _h5py()
        with h5py.File(path, "r") as f:

            def read(key):
                return ["".join(chr(int(c)) for c in np.ravel(f[ref][()])) for ref in np.ravel(f[key][()])]

            names = read("ColCell")
            units = read("ColUnits") if "ColUnits" in f else [""] * len(names)
    except (OSError, KeyError) as ex:
        raise CatalogError(f"cannot read catsHTM column names '{path}': {ex}") from ex
    return tuple(names), tuple(units)


@dataclass
class ConeStats:
    """What one cone search touched -- for the run record and for diagnosing a thin result."""

    trixels: int = 0
    files: int = 0
    sources: int = 0
    missing_files: list[str] = field(default_factory=list)
    missing_datasets: int = 0


class CatsHtm:
    """One catsHTM catalog, e.g. ``CatsHtm(r"\\\\last0\\catalogs\\GAIA\\DR3", "GAIADR3")``."""

    def __init__(self, root: str, name: str = "GAIADR3", files_per_hdf: int = 100):
        self.root = root
        self.name = name
        self.files_per_hdf = files_per_hdf
        self.last_stats = ConeStats()

    def columns(self) -> list[str]:
        return list(_load_columns(self.root, self.name)[0])

    def units(self) -> dict[str, str]:
        names, units = _load_columns(self.root, self.name)
        return dict(zip(names, units, strict=False))

    def trixels(self, ra_deg: float, dec_deg: float, radius_deg: float) -> list[int]:
        """IDs (1-based) of the leaf trixels a cone may touch."""
        idx = _load_index(self.root, self.name)
        lon, lat, r = np.radians(ra_deg), np.radians(dec_deg), np.radians(radius_deg)
        limit = np.pi / 2.0 + r
        found: list[int] = []
        stack = list(idx.roots)
        while stack:
            row = stack.pop()
            if not np.all(angular_distance(lon, lat, idx.poles_lon[row], idx.poles_lat[row]) < limit):
                continue
            sons = idx.sons[row]
            sons = sons[sons > 0]
            if len(sons) == 0:
                found.append(int(row) + 1)
            else:
                stack.extend(int(s) - 1 for s in sons)
        return sorted(found)

    def _data_file(self, trixel: int) -> str:
        first = (trixel // self.files_per_hdf) * self.files_per_hdf
        return os.path.join(self.root, f"{self.name}_htm_{first:06d}.hdf5")

    def cone_search(self, ra_deg: float, dec_deg: float, radius_deg: float, columns: Sequence[str]) -> dict[str, np.ndarray]:
        """Every source within ``radius_deg`` of (``ra_deg``, ``dec_deg``), J2000 / catalog epoch.

        Returns ``{column: array}`` for the requested ``columns``, in the catalog's
        units -- for GAIA DR3 ``RA``/``Dec`` are radians.  A data file or dataset
        that is missing is logged and counted in :attr:`last_stats`, not raised:
        the share has been seen to carry partial copies, and a thin result is
        better reported than fatal.  An unreadable index or column list raises
        :class:`CatalogError`.
        """
        h5py = _h5py()
        names = self.columns()
        try:
            want = [names.index(c) for c in columns]
            i_ra, i_dec = names.index("RA"), names.index("Dec")
        except ValueError as ex:
            raise CatalogError(f"catalog '{self.name}' has no column {ex}; it has {names}") from ex
        rows = sorted({*want, i_ra, i_dec})  # h5py fancy indexing needs increasing indices
        pos = {r: k for k, r in enumerate(rows)}

        idx = _load_index(self.root, self.name)
        trixels = [t for t in self.trixels(ra_deg, dec_deg, radius_deg) if idx.nsrc[t - 1] != 0]
        stats = ConeStats(trixels=len(trixels))
        by_file: dict[str, list[int]] = {}
        for t in trixels:
            by_file.setdefault(self._data_file(t), []).append(t)

        parts: list[np.ndarray] = []
        for path, ids in by_file.items():
            try:
                f = h5py.File(path, "r")
            except OSError as ex:
                logger.warning(f"catsHTM: cannot open '{path}': {ex}")
                stats.missing_files.append(os.path.basename(path))
                continue
            stats.files += 1
            with f:
                for t in ids:
                    key = f"htm_{t:06d}"
                    if key not in f:
                        stats.missing_datasets += 1
                        continue
                    parts.append(np.asarray(f[key][rows, :], dtype=float))
        if stats.missing_files or stats.missing_datasets:
            logger.warning(
                f"catsHTM '{self.name}': {len(stats.missing_files)} file(s) and "
                f"{stats.missing_datasets} dataset(s) missing for the cone at ({ra_deg}, {dec_deg}) r={radius_deg}"
            )

        data = np.concatenate(parts, axis=1) if parts else np.empty((len(rows), 0))
        d = angular_distance(np.radians(ra_deg), np.radians(dec_deg), data[pos[i_ra]], data[pos[i_dec]])
        inside = d <= np.radians(radius_deg)
        stats.sources = int(inside.sum())
        self.last_stats = stats
        return {c: data[pos[i], inside] for c, i in zip(columns, want, strict=True)}
