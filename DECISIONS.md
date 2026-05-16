# MAST Unit -- Architecture Decisions

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
