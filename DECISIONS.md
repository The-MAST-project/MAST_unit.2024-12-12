# MAST Unit -- Architecture Decisions

---

## [2026-07-22] Establish a pytest `tests/` harness; first suite guards the limit-frame RPC contract

**Why:** The `phd2.limit_frame` work (#29, issue #51) was validated by one-off
bench scripts on labcomp2 (2026-07-07); those runs proved the behavior once but
protect nothing against regressions. The repo had no test harness at all.

**What:** `tests/` with a pytest suite that drives the **real**
`PHD2Connector` methods with mocked collaborators — no PHD2 process, no
hardware, no Mongo — asserting on the exact RPC stream
(`set_limit_frame` / `guide` / `capture_single_frame`):

- The four `phd2.limit_frame` states: disabled -> `roi: None` (full frame);
  enabled + rectangle -> applied after `ImagerRoi` conditioning (mod-8 width /
  mod-2 height at all binnings, optical-axis centering — caught by the first
  Windows run: (3031, 2692, 2000, 400) reaches the wire as (3038, 2693, 1984,
  396)); enabled without rectangle -> the derived guiding ROI; **no DB section
  -> identical to deployed behavior** (the safe-to-land invariant).
- Ordering: the limit frame is set before the `guide` RPC.
- Scope pin: acquisition-time `start_exposure()` keys off
  `ImagerSettings.use_set_limit_frame` alone — the config section must play
  no role there.

Connectors are built via `object.__new__` (bypassing the heavy `__init__`)
with a real `PHD2Config` — the pattern proven on the 2026-07-07 bench.
`tests/conftest.py` bootstraps `sys.path` to `src/` and shims `Filer` on
Darwin (unsupported there). The import chain is Windows-only today
(`stage.py` uses pyximc names at module level), so the suite runs in the unit
venv and skips cleanly elsewhere. `requirements-dev.txt` declares pytest.

**Implications:** Guiding/config changes should extend this suite rather than
add bench one-offs; the labcomp2 bench remains for what needs a live PHD2 or a
real camera. This executes the unit-side half of the 2026-07-07 bench's
TEST-MIGRATION plan; the common-side half lives in `src/common/tests/`.

---

## [2026-07-02] Guiding limit frame comes from `phd2.limit_frame` config, not code toggles

**Why:** Two things about the PHD2 limit frame (the sub-frame PHD2 confines
guide-star selection to) were hard-wired: whether it is applied at all was a code
path Oren hand-toggled on the production machine (the `# oren` branches in
`src/phd2/phd2.py`), and the rectangle itself was derived at guiding time by
`Guider.make_guiding_settings()` from the fiber position/margins in
`guiding.rois`. Both need to be operator-tunable without editing deployed code.

**What:** `PHD2Connector.start_guiding()` still calls `make_guiding_settings()`
for exposure/gain/binning (and as the ROI fallback), but the limit frame is now
governed by the DB-persisted `phd2.limit_frame` section (see `LimitFrameConfig`
in MAST_common, bumped here):

- `enabled: false` → `guide()` resets the limit frame (`set_limit_frame(None)`,
  full-frame star selection) — the behavior Oren previously got by editing code.
- `enabled: true` with a configured rectangle → that rectangle (unbinned camera
  pixels, conditioned through `ImagerRoi`) is the limit frame.
- `enabled: true` without a rectangle (section absent, or width/height 0) → the
  fiber/margin-derived guiding ROI is used, exactly as before.

The `# oren` hand-toggle markers were removed: the "reset the limit frame"
branch is now a legitimate, configuration-selected path. Acquisition-time limit
frame control (`use_set_limit_frame` on the acquisition API and the fcu_v2
override in `acquirer.py`) is untouched — this change only re-sources the
guiding-time decision.

To set the frame for all units (Mongo on `mast-ns-control`, db `mast`):

```js
db.units.updateOne(
  { name: "common" },
  { $set: { "phd2.limit_frame": { enabled: true, x: 3031, y: 2692, width: 2000, height: 400 } } }
)
```

(or per-unit by matching its name; values above are an example). The unit
service reads its configuration snapshot at startup, so restart the unit
service after changing it.

**Implications:** With no DB change, behavior is identical to the pre-change
default (`use_set_limit_frame=True`, derived ROI). Operators flip/tune guiding
limit-frame behavior in the configuration DB (GUI-exposable via the fields' UI
metadata) instead of patching `phd2.py` on the machine.

---

## [2026-06-23] Host the solver test fixture as a GitHub Release asset, not in-repo git-lfs

**Why:** The previous day's commit bundled the ~90 MB `full-frame.fits` integration
fixture in-repo via git-lfs. Even via LFS this bloats the unit: every clone pays the
LFS download for a file only the astrometry.net integration test (skipped on most
machines) ever needs, and it consumes the repo's LFS storage/bandwidth quota. The unit
must stay lean.

**What:** Removed the file from history (the LFS pointer was excised from the four
commits that carried it and `main` was force-pushed) and republished the frame as a
GitHub Release asset on `The-MAST-project/MAST_unit.2024-12-12`, tag `fixtures-v1`.
`src/solvers/tests/conftest.py` now resolves the fixture lazily: it uses `MAST_TEST_FITS`
if set, else a git-ignored local cache at `tests/fixtures/full-frame.fits`, else it
fetches the release asset on first use and verifies its sha256 (`fd8618de…e526`). Because
the repo is private the asset is not anonymously downloadable, so the fetch shells out to
the authenticated `gh` CLI (`gh release download fixtures-v1`); absent `gh`, the test
skips with a message pointing at `MAST_TEST_FITS`. The pure-math tests never trigger a
download; only a machine that already has solve-field + indexes (i.e. actually running the
integration test) fetches it.
The scoped `fixtures/.gitattributes` LFS rule and the `fixtures/.gitignore` re-include
were removed; the repo-wide `*.fits` ignore now keeps the cached frame untracked.

**Implications:** Fresh clones no longer carry the fixture. Collaborators who pulled
`main` on/after 2026-06-22 must re-sync the rewritten history (`git fetch` then reset their
`main`). The orphaned 90 MB LFS object remains in GitHub's LFS store until a repo admin
prunes it (history rewriting alone does not reclaim server-side LFS storage). Supersedes
the "bundled via git-lfs" mechanism noted in the 2026-06-22 entry below.

---

## [2026-06-22] Keep MASTrometry's numpy pre-downsample/ROI-crop surface; fix its bugs rather than switch to bare astrometry.net

**Why:** MASTrometry pre-downsamples (2x2) and optionally ROI-crops the image in numpy
before calling astrometry.net, so the returned WCS lives in the binned/cropped grid, not
the original full frame. Relating it back requires a coordinate transform, and that
transform had produced silent sub-arcsecond bugs. A bare `solve-field --downsample` on the
full frame would avoid the surface entirely (its WCS is already in full-frame pixels) and
would satisfy the science requirement -- full-frame solving in full-frame pixel
coordinates -- for free. We chose to KEEP the surface anyway, so ROI cropping stays
implementable in-house without reopening the solver decision. The surface is the price of
that flexibility; it is fenced and tested, not removed. Background study:
`C:\MAST\mastrometry-equivalence` (`compare_solves.py`, `FINDINGS.md`).

**What:**

`src/solvers/pixel_grid.py` (new)
- Single source of truth for the binned<->full-frame transform, with the validated
  pixel-center convention `g = (o + (f-1)/2) / f`. Pure module, no heavy imports, so its
  unit tests run with no astrometry.net / MAST runtime.

`src/solvers/mastrometry.py`
- ROI `refpix` now uses `pixel_grid.roi_center_to_crpix` (fractional) instead of integer
  `(center - start) // factor`. The old code biased `--crpix-x/--crpix-y` by ~0.4", a
  constant pointing offset on the spec/fiber path.
- Dropped `--no-tweak` so SIP is fit, matching the reference solver (AstrometryDotNet) and
  the equivalence study, which found `--no-tweak` over-constrains the WCS and causes up to
  ~7" disagreement. This is the one runtime/perf-affecting change; reversible (documented
  inline).

`src/solvers/CLAUDE.md`, `src/solvers/COORDINATE_SURFACE.md` (new): warning signs for the
fragile surface.

`src/solvers/tests/` (new): `test_pixel_grid.py` (pure math, runs anywhere -- the primary
drift canary) and `test_equivalence_integration.py` (skipped unless astrometry.net +
indexes + fixture present). Sample frame fetched on demand from a GitHub Release asset
(see the 2026-06-23 entry above; originally bundled via git-lfs).

**Implications:** All original<->binned and ROI-refpix coordinate math must go through
`pixel_grid.py` -- do not reintroduce `orig/factor` or `(center - start) // factor` inline;
those are the exact bugs. `--crpix-x/--crpix-y` are FITS 1-based. After any change to that
module, the mastrometry downsample/crop/refpix logic, or the solve-field flags, run
`src/solvers/tests/` (the pure-math tests catch convention drift with no astrometry.net; the
integration test needs `git lfs pull` plus a solver + indexes). Keep tweak on unless solve
latency becomes binding. If the ROI path is ever permanently abandoned, the surface and
`pixel_grid.py` can go with it.

---

## [2026-06-21] Unit fails startup loudly on invalid configuration

**Why:** With the bootstrap configuration moving to a per-machine TOML file (see the
MAST_common decision of the same date), an absent, malformed, or drifted config must
not let the unit limp along with bad values. It should refuse to start and say
exactly why.

**What:**

`src/app.py`
- The `__main__` entrypoint constructs `Config()` inside `try/except ConfigError`
  *before* anything else (and before `uvicorn.run()`). On failure it logs the detailed
  reason, calls `app_quit()` (which also tears down the PWI4 child it spawned earlier),
  and `sys.exit(1)`. The server never starts.
- `ConfigError` covers: missing `MAST_PROJECT`, missing/malformed `C:\WIS\unit.toml`
  (or `$MAST_CONFIG`), schema/validation errors, and the local config disagreeing with
  the DB `sites` document (`site`/`project`/`controller_host`/`location`).
- Requires the unit's `src/common` submodule to point at the MAST_common commit that
  introduces `config/local.py` (`ConfigError`, `load_local_config`).

---

## [2026-06-10] ps3cli runs as a persistent --server; locate by largest exe (supersedes 2026-05-14)

**Why:** The 2026-05-14 entry ("ps3cli is a one-shot tool, not a persistent process")
was based on the older on-demand `ps3cli.exe`, which exited immediately and could not
serve repeated requests. We have since obtained a specially built `ps3cli.exe`
(2024-09-10) that supports `--server` mode: it loads its catalogs once and stays
resident, making repeat plate-solves fast. That makes the persistent-process model
correct again -- the exact case the 2026-05-14 entry anticipated ("if a future change
makes ps3cli persistent again, restore the `ensure_process_is_running` call").

**What:**

`src/app.py`
- Launches `ps3cli.exe --server --port=8998` via `ensure_process_is_running(...)`;
  the one-shot `check_ps3cli()` probe was removed.
- `_locate_ps3cli_dir()` searches recursively under known roots (`$PS3CLI_DIR`,
  `~/Documents/PlaneWave/ps3cli`, the Program Files path) and returns the directory of
  the **largest** `ps3cli.exe`. The special build unpacks into a dated folder
  (`ps3cli-2024-09-10\`) and an older build may linger beside it; picking the largest
  selects the special `--server` build regardless of folder name.

**Implications:**
- Supersedes the 2026-05-14 "one-shot" decision; that entry stays for history but the
  design has reverted to persistent `--server`.
- Resolution logic is kept in sync with `verify-planewave.ps1` in MAST_provisioning,
  which selects the largest `ps3cli.exe` the same way. The install itself is provisioned
  by the `planewave` provider; see the matching 2026-06-10 entry in
  MAST_provisioning/DECISIONS.md.

---

## [2026-05-16] Unit service must not crash on missing hardware or config

**Why:** The unit service was crashing at startup (or failing to serve any response) whenever
a component could not be initialised -- most commonly because the MongoDB config was
unreachable or a hardware device hostname could not be resolved. This made provisioning
tests fragile: a single missing dependency would leave the service dead with no diagnostic
output, instead of running and reporting what was wrong.

The guiding principle is that the HTTP server must always start and the `/status` endpoint
must always respond -- even when every piece of hardware is absent and config is degraded.
Failures are surfaced as `CanonicalResponse.errors`, not as startup crashes or 500s.

**What:**

`src/app.py`
- Removed the module-level `Config().get_unit()` call that ran before FastAPI initialised;
  a failure there killed the import. `log_level` is now hardcoded to `WARNING` at module
  level (was the only thing the call produced).
- `lifespan()`: wrapped `Unit()` construction in try/except so a Unit init failure lets the
  app continue rather than propagating out of the context manager.
- `__main__` block: same try/except around `Unit()`; `unit = None` on failure so the
  `if unit:` guard prevents router registration on a broken object.

`src/unit.py`
- `__init__`: moved `self._init_errors: list[str] = []` to the top of the method (before
  any config or component work) so errors can be accumulated from the first line.
- Config loading: replaced bare `Config().get_unit()` (which could raise) with a
  try/except that appends to `_init_errors` on failure; added a second check for the case
  where config returns None without raising. If config is unavailable, numeric fields that
  would have come from it (`min_ra_correction_arcsec`, `min_dec_correction_arcsec`,
  `autofocus_max_tolerance`) are defaulted to `0.0` so the rest of `__init__` can proceed.
- `connected` setter: added `if self.<component>:` guards on all five component
  assignments (mount, imager, covers, stage, focuser) -- any of these may be None if
  `_try_init` caught a failure.
- `do_shutdown`, `abort`: added `if self.guider:` guards before calling
  `self.guider.abort()`.
- `ontimer` StartingUp / ShuttingDown completion checks: each component activity test is
  now wrapped in `(self.<component> and self.<component>.is_active(...))` so a None
  component is treated as not-active rather than raising AttributeError.
- `operational` / `why_not_operational`: removed `assert self.unit_conf is not None`;
  both properties short-circuit to the `_init_errors` list when init errors exist, and
  handle `self.unit_conf is None` gracefully when building the component set.

**Implications:** Any `Unit.__init__` failure path must append a human-readable message to
`self._init_errors` rather than raising. The existing pipeline (`_init_errors` ->
`self.errors` -> `status()` -> `CanonicalResponse.errors`) ensures those messages reach
callers. Code that adds new component initialisations must follow the same `_try_init`
pattern and must not assume the result is non-None.

---

## [2026-05-16] Submodule bumps for hostname canonicalization

**Why:** Windows returns `socket.gethostname()` in uppercase (`MAST-WIS-01`), but MongoDB
`unit_ids` and the configuration lookup keys are stored in lowercase. Also, the canonical
unit-hostname format was being extended from the old `mast-<site>-<role>` scheme to
`mast-<site>-NN` for numbered units, which required regex updates in `common`. Both
changes live in `MAST_common`; the unit repo just needs its submodule pointer advanced.

**What:**

`src/common` submodule
- Bumped to `c05fc82` (mast-<site>-NN format + lowercase hostname in `Config.__init__` and
  `get_unit()`; new `canonic_unit_name()` branch).
- Bumped to `3e648b8` (lowercase normalisation in `site_name_from_unit_name` and
  `_verify_unit_site_membership`).

**Implications:** Unit code can assume `Config` works correctly regardless of host OS
casing. No changes required in the unit itself beyond carrying the new submodule SHA.

---

## [2026-05-14] FastAPI Query parameter: `regex=` -> `pattern=`; ImagerStatus import path

**Why:** FastAPI removed the deprecated `regex=` keyword on `Query()` (and `Path()`,
`Body()`) in favour of `pattern=`. Endpoints using `regex=` were failing schema validation
on the newer FastAPI in the provisioning VM environment. Separately, `ImagerStatus` had
been moved from `common.interfaces.imager` to `common.models.statuses` in MAST_common
(part of the earlier statuses refactor), but `src/imagers/__init__.py` was still importing
the old path and crashing at import time.

**What:**

- `src/acquirer.py`, `src/autofocusing.py`, `src/unit.py`: replaced `Query(regex=...)`
  with `Query(pattern=...)` on all `ra_j2000_hours` / `dec_j2000_degs` parameters.
- `src/imagers/__init__.py`: split the `common.interfaces.imager` import to keep
  `ImagerExposureSeries`, `ImagerInterface`, `ImagerSettings`, `ImagerTypes` there but
  pull `ImagerStatus` from `common.models.statuses`.

**Implications:** Any new endpoint adding a regex-constrained query parameter must use
`pattern=`. Any new code touching imager statuses must import `ImagerStatus` from
`common.models.statuses`, not `common.interfaces.imager`.

---

## [2026-05-14] ps3cli is a one-shot tool, not a persistent process

**Why:** `src/app.py` was using `ensure_process_is_running(...)` to keep `ps3cli.exe`
alive in `--server` mode, but ps3cli does not actually run as a persistent server in our
deployment -- each solve is a fresh short-lived invocation. The persistent-process wrapper
would spin forever waiting for a process that exited milliseconds after starting, blocking
the rest of unit startup behind the wait loop in `ensure_process_is_running`.

**What:**

`src/app.py`
- Removed the `ensure_process_is_running("ps3cli.exe", ...)` call.
- Added `check_ps3cli()`: verifies the exe exists at the expected path, runs it with no
  arguments under `CREATE_NO_WINDOW`, and treats exit codes 0 or 1 (1 = "no args supplied",
  which proves the binary loaded) as success. Failures are logged but do not abort startup.
- Also corrected the ps3cli path (was pointing at the wrong directory).

**Implications:** ps3cli health is a startup-time probe, not a supervised process. If
ps3cli becomes unreachable mid-run, solving calls will fail at solve time -- we do not
detect or restart it. If a future change makes ps3cli persistent again, restore the
`ensure_process_is_running` call rather than extending `check_ps3cli`.

---

## [2026-05-14] Submodule pinned to fork on `eli/vm-provisioning`; ximc path anchored to file

**Why:** Provisioning work in `MAST_common` lives on `eli/vm-provisioning` in a personal
fork (`elibrody-weizmann/MAST_common`) so it can be iterated without touching the upstream
`The-MAST-project` repo. The unit submodule must point at the fork on that branch, not at
upstream `main`. Separately, `src/stage.py` was resolving the Standa ximc DLL path from
`Path().cwd()`, which only worked when the unit was launched from `src/`. Running the
service from any other directory (a Windows service wrapper, a different shell) silently
broke stage initialisation.

**What:**

- `.gitmodules`: `url` switched to `https://github.com/elibrody-weizmann/MAST_common.git`;
  `branch = eli/vm-provisioning` added so `git submodule update --remote` tracks it.
- `src/stage.py`: `ximc_top_dir` derived from `Path(__file__).parent / "Standa" / ...`
  instead of `Path().cwd() / ...`.

**Implications:** Anyone cloning the unit repo on this branch must use
`git submodule update --init` (not a bare clone) to pick up the fork. The ximc DLL search
path is now CWD-independent; do not reintroduce `Path().cwd()` for resource lookup
anywhere else in the codebase for the same reason.

---

## [2026-05-14] requirements.txt: add `rich` + `astroplan`, normalise to UTF-8

**Why:** The file was UTF-16 LE with BOM (PowerShell `Out-File` default), which `pip
install -r` on some platforms parses incorrectly or rejects outright. Two missing
dependencies (`rich` for console output, `astroplan` for observability calculations) were
being installed manually on each VM, which is exactly the kind of drift provisioning is
meant to eliminate.

**What:** `requirements.txt` rewritten in plain UTF-8 (no BOM) with `rich` and `astroplan`
added.

**Implications:** When editing `requirements.txt` from PowerShell, pass `-Encoding utf8`
to `Out-File` / `Set-Content` -- the default will silently revert to UTF-16 and break
`pip` again.
