"""The catsHTM reader against a synthetic catalog with real HTM geometry.

The catalog is built the way catsHTM lays one out -- an index of trixels with 1-based
IDs, edge poles in radians and MATLAB's column-major storage, leaf datasets named
``htm_<ID>`` grouped 100 to a file, and the column names in a v5 ``.mat`` -- but only
two levels deep (8 + 32 + 128 trixels) so it is built in milliseconds.

The reference for every cone is brute force over all the stars, so the test asks the
one question that matters: does the trixel search ever drop a star the cone contains?
"""

import os
import shutil

import numpy as np
import pytest

h5py = pytest.importorskip("h5py")
scipy_io = pytest.importorskip("scipy.io")

from catalogs.catshtm import CatalogError, CatsHtm, angular_distance  # noqa: E402

NAME = "TESTCAT"
COLUMNS = ["RA", "Dec", "Epoch", "PMRA", "PMDec", "mag"]


def _unit(v):
    v = np.asarray(v, dtype=float)
    return v / np.linalg.norm(v)


def _lonlat(v):
    x, y, z = v
    return np.arctan2(y, x) % (2 * np.pi), np.arcsin(np.clip(z, -1, 1))


def _poles(tri):
    """The three edge poles, each on the trixel's own side of its edge."""
    centroid = _unit(sum(tri))
    poles = []
    for a, b in ((tri[0], tri[1]), (tri[1], tri[2]), (tri[2], tri[0])):
        p = _unit(np.cross(a, b))
        poles.append(p if np.dot(p, centroid) > 0 else -p)
    return poles


def _build_trixels(depth=2):
    """Breadth-first trixels: list of (level, father_id or None, vertices). IDs are index + 1."""
    v = [_unit(p) for p in ((0, 0, 1), (1, 0, 0), (0, 1, 0), (-1, 0, 0), (0, -1, 0), (0, 0, -1))]
    roots = [
        (v[1], v[5], v[2]),
        (v[2], v[5], v[3]),
        (v[3], v[5], v[4]),
        (v[4], v[5], v[1]),
        (v[1], v[0], v[4]),
        (v[4], v[0], v[3]),
        (v[3], v[0], v[2]),
        (v[2], v[0], v[1]),
    ]
    trixels = [(0, None, t) for t in roots]
    frontier = list(range(len(trixels)))
    for level in range(1, depth + 1):
        nxt = []
        for i in frontier:
            a, b, c = trixels[i][2]
            w0, w1, w2 = _unit(b + c), _unit(a + c), _unit(a + b)
            for child in ((a, w2, w1), (b, w0, w2), (c, w1, w0), (w0, w1, w2)):
                trixels.append((level, i + 1, child))
                nxt.append(len(trixels) - 1)
        frontier = nxt
    return trixels


def _contains(poles, p):
    return all(np.dot(p, q) >= -1e-12 for q in poles)


@pytest.fixture(scope="module")
def catalog(tmp_path_factory):
    """Write the catalog; return (root, all_stars) with all_stars as {column: array}."""
    root = str(tmp_path_factory.mktemp("catshtm"))
    trixels = _build_trixels()
    n = len(trixels)
    poles = [_poles(t[2]) for t in trixels]
    sons: dict[int, list[int]] = {}
    for i, (_, father, _) in enumerate(trixels):
        if father is not None:
            sons.setdefault(father, []).append(i + 1)
    leaves = [i for i in range(n) if (i + 1) not in sons]

    rng = np.random.default_rng(7)
    xyz = rng.normal(size=(6000, 3))
    xyz /= np.linalg.norm(xyz, axis=1)[:, None]
    # Extra stars where the tests look: around the RA seam and next to the pole.
    for lon, lat in ((0.0, 0.0), (0.0, np.radians(89.0))):
        c = np.array([np.cos(lat) * np.cos(lon), np.cos(lat) * np.sin(lon), np.sin(lat)])
        extra = c + 0.02 * rng.normal(size=(800, 3))
        xyz = np.vstack([xyz, extra / np.linalg.norm(extra, axis=1)[:, None]])
    lon, lat = _lonlat(xyz.T)
    mag = rng.uniform(5, 18, len(lon))
    stars = {
        "RA": lon,
        "Dec": lat,
        "Epoch": np.full(len(lon), 2016.0),
        "PMRA": rng.normal(0, 5, len(lon)),
        "PMDec": rng.normal(0, 5, len(lon)),
        "mag": mag,
    }

    # Each star goes to exactly one leaf.
    owner = np.full(len(lon), -1)
    for k, p in enumerate(xyz):
        for i in leaves:
            if _contains(poles[i], p):
                owner[k] = i
                break
    assert (owner >= 0).all()

    index = np.full((n, 13), np.nan)
    for i, (level, father, _) in enumerate(trixels):
        index[i, 0] = level
        index[i, 1] = np.nan if father is None else father
        for j, s in enumerate(sons.get(i + 1, [])):
            index[i, 2 + j] = s
        for j, q in enumerate(poles[i]):
            index[i, 6 + 2 * j], index[i, 7 + 2 * j] = _lonlat(q)
        if i in leaves:
            index[i, 12] = int((owner == i).sum())
    with h5py.File(os.path.join(root, f"{NAME}_htm.hdf5"), "w") as f:
        f.create_dataset(f"{NAME}_HTM", data=index.T.astype(np.float32))  # stored (13, n), as MATLAB writes it

    table = np.vstack([stars[c] for c in COLUMNS])  # (ncol, nstars)
    for i in leaves:
        tid = i + 1
        path = os.path.join(root, f"{NAME}_htm_{(tid // 100) * 100:06d}.hdf5")
        rows = table[:, owner == i]
        if rows.shape[1] == 0:
            continue
        with h5py.File(path, "a") as f:
            f.create_dataset(f"htm_{tid:06d}", data=rows)

    cells = np.empty((1, len(COLUMNS)), dtype=object)
    cells[0, :] = COLUMNS
    units = np.empty((1, len(COLUMNS)), dtype=object)
    units[0, :] = ["rad", "rad", "JYear", "mas/yr", "mas/yr", "mag"]
    scipy_io.savemat(os.path.join(root, f"{NAME}_htmColCell.mat"), {"ColCell": cells, "ColUnits": units})
    return root, stars


def _brute(stars, ra_deg, dec_deg, r_deg):
    d = angular_distance(np.radians(ra_deg), np.radians(dec_deg), stars["RA"], stars["Dec"])
    return np.sort(stars["mag"][d <= np.radians(r_deg)])


@pytest.mark.parametrize(
    ("ra", "dec", "r"),
    [
        (90.0, 30.0, 5.0),
        (359.9, 0.0, 1.0),  # across RA = 0
        (0.1, 0.0, 1.0),
        (10.0, 89.0, 2.0),  # around the pole
        (200.0, -45.0, 20.0),  # many trixels
        (0.0, 0.0, 180.0),  # the whole sky: every trixel, both data files
        (45.0, 35.26, 3.0),  # on a level-0 vertex region
    ],
)
def test_cone_returns_exactly_the_stars_inside(catalog, ra, dec, r):
    root, stars = catalog
    got = CatsHtm(root, NAME).cone_search(ra, dec, r, ["mag"])
    assert np.array_equal(np.sort(got["mag"]), _brute(stars, ra, dec, r))


def test_columns_and_units_come_from_the_mat_file(catalog):
    root, _ = catalog
    cat = CatsHtm(root, NAME)
    assert cat.columns() == COLUMNS
    assert cat.units()["RA"] == "rad"


def test_only_the_requested_columns_come_back(catalog):
    root, stars = catalog
    got = CatsHtm(root, NAME).cone_search(90.0, 30.0, 5.0, ["PMDec", "mag"])
    assert set(got) == {"PMDec", "mag"}
    assert len(got["PMDec"]) == len(got["mag"]) == len(_brute(stars, 90.0, 30.0, 5.0))


def test_a_missing_data_file_thins_the_result_without_failing(catalog, tmp_path):
    root, stars = catalog
    partial = tmp_path / "partial"
    partial.mkdir()
    for name in os.listdir(root):
        if not name.endswith("_000100.hdf5"):
            shutil.copy(os.path.join(root, name), partial / name)
    cat = CatsHtm(str(partial), NAME)
    got = cat.cone_search(0.0, 0.0, 180.0, ["mag"])  # the whole sky, so every trixel is asked for
    assert 0 < len(got["mag"]) < len(stars["mag"])
    assert cat.last_stats.missing_files == [f"{NAME}_htm_000100.hdf5"]


def test_an_unknown_column_is_a_catalog_error(catalog):
    root, _ = catalog
    with pytest.raises(CatalogError, match="no column"):
        CatsHtm(root, NAME).cone_search(90.0, 30.0, 1.0, ["phot_g_mean_mag"])


def test_an_unreachable_catalog_is_a_catalog_error(tmp_path):
    with pytest.raises(CatalogError):
        CatsHtm(str(tmp_path / "nowhere"), NAME).cone_search(0.0, 0.0, 1.0, ["RA"])
