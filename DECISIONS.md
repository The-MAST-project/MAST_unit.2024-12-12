# MAST Unit -- Architecture Decisions

---

## [2026-08-11] Contract checks assert set equality against a known-violations list

**Why:** The endpoint contract's static invariants had no enforcement, and three of them
regressed or were found inert *because nothing checked them*: invariant 9 broke within hours
of being written down (#102's `run_acquisition`), MAST_common#45's twelve undeclared abstract
signatures went uncounted until somebody thought to ask, and MAST_common#46's exposure-series
guard had never executed once since it was written. All three are questions about the shape
of the source, answerable statically, needing no hardware and no app fixture — so they could
have been checked at any point in the last two years.

**What:** `tests/contract/` — four AST checks (invariants 3, 4, 9, and the interface half of
10), each with a `KNOWN_*` dict of today's violations keyed to the issue that owns fixing
them. Three decisions inside that are worth recording:

- **Set equality, not `xfail` per item.** #52's design says xfail-keyed, and for the
  app-dependent rows that is right. For these it is not: an `xfail` catches a new violation
  but not a *stale* marker — an item fixed elsewhere leaves an `xpass` that is easy to
  configure into silence, and the list then encodes whatever was true the day it was written.
  That is precisely the failure mode MAST_common#45 warns about for hand-kept lists. Checking
  both directions means an issue landing must remove its entry, and a list that drifts from
  the tree is red.
- **Every detector is exercised over a synthetic source with a known answer.** Each real
  assertion is "no unexpected findings", which a *broken* detector satisfies as perfectly as
  a clean tree does. Without the synthetic case these checks would be unfalsifiable, which is
  the same defect as having no check. This was not hypothetical: the first run of the common
  scans passed over zero files, because the exclusion of `common` (correct for the unit tree,
  where an untracked sibling clone sits inside `src/`) was matched against absolute path parts
  and so also excluded everything under `COMMON_ROOT`.
- **Known lists key on (module, subject), never on a line number.** #81's inventory recorded
  `zwo.py:148` and `imagers/ascom.py:806`; both had drifted to 151 and 812 by the time the
  check was written, with nothing about the dispatch sites having changed.

**Implications:** the checks find more than the tickets recorded. The flag-balance check finds
**seven** unbalanced activity flags where #44 listed four — `StageActivities.Aborting`,
`ImagerActivities.StartingUp` and `UnitActivities.PreGuiding` were in no inventory. Two
scoping rules were needed to get there and both are load-bearing: only unit-owned activity
enums are judged (`common/activities.py` declares the whole fleet's, so scanning the unit tree
against MAST_spec's and MAST_control's reports 60-odd findings that are all false), and
zero-valued `Idle = 0` sentinels are not flags.

The checks live in this repo rather than in MAST_common even though two of them scan `common`:
#52 hosts the contract checks, the unit's CI already checks out `common` as a sibling, and the
dead-precondition check *must* see both trees — `require_open_exposure_series` is defined in
`common` and its callers would be the unit's backends, so a MAST_common-only check would start
reporting a false positive the moment the unit wired it up.

---

## [2026-08-10] `app.py` is a factory, not a module with side effects

**Why:** The HTTP surface was unreachable from anything but `python app.py`, which made
the endpoint contract (#42) unenforceable — you cannot assert a route table you cannot
construct. Three separate couplings did it:

- All six `app.include_router(...)` calls sat inside `if __name__ == "__main__"`, so an
  imported `app` had one route (`/favicon.ico`) while the tree registered 74 operations.
- Module scope started PWI4, PWShutter and ps3cli and blocked up to 30 s connecting to
  PWI4. On a unit machine that drives the real telescope, so `tests/conftest.py` blocks
  process launches wholesale — a tripwire whose own docstring names this file as the
  reason and asks for this fix.
- `lifespan` called `Unit()` while `Unit` was imported only inside the `__main__` block.
  Under `python app.py` the global happened to be bound in time; under any other entry
  point the call raised `NameError` into a bare `except Exception`, and the unit's
  lifespan hooks were skipped silently. (`Unit` is a singleton with an `_initialized`
  guard, so the second construction was a no-op rather than a duplicate instance — what
  was broken was the dependency on a global another code path happened to bind.)

**What:** `create_app(unit=None)` builds the app and mounts the routers;
`start_supporting_processes()` holds the spawns; `main()` sequences configuration
validation, `Unit()`, both of those, and uvicorn. `lifespan` closes over the unit the
factory was given. `python app.py` performs the same steps in the same order as before —
the spawns still run ahead of the configuration check, because that is the order module
import used to impose.

`create_app`'s `unit` parameter is deliberately unannotated: annotating it `Unit` would
require a module-scope `from unit import Unit`, which drags in the Windows / driver /
Mongo chain the factory exists to keep out of app construction.

Router mounting also became a loop over `COMPONENT_ATTRIBUTES` rather than six
`if unit.<component>:` branches. Same semantics, same order, and it gives #52's manifest
a single place to read the component list from.

**Implications:** `TestClient(create_app(unit))` reaches the real routes, which unblocks
#52's Phases 1-4. There is no module-level `app` object any more, so `uvicorn app:app`
is no longer a usable entry point — nothing used it, and it was one of the paths where
the silent `lifespan` failure above would have bitten.

One discovery worth carrying into #52: on FastAPI 0.139 `include_router()` does **not**
flatten into `app.routes`. It appends a single `_IncludedRouter` wrapper whose entire
interface is private, so mounted paths are not enumerable there. The public reading is
`app.openapi()["paths"]`, which is what `tests/test_app_factory.py` uses and what the
anchor test will have to use — with the caveat that a route registered
`include_in_schema=False` is invisible in it (nothing in the unit sets it today).

---

## [2026-08-06] Retire the `src/common` submodule; MAST_common is a sibling clone

**Why:** Two mechanisms were resolving the same package, and only one of them was
real. At runtime the service starts with `AppDirectory = <ProjectDir>\src` and no
`PYTHONPATH`; `import common` was satisfied by the venv's `mast.pth`, which puts the
flat top folder (`<top>/`) on `sys.path`, so it resolved to the **sibling clone**
`<top>/common/` — never to `src/common`. The submodule was carrying a gitlink that
nothing consumed, pinned at `07cee90` and by 2026-08-06 several merges stale.

Worse than redundant: an *uninitialised* `src/common` is an empty directory, which
Python can treat as a namespace-package portion named `common`. A real package with
`__init__.py` still wins, so nothing broke — but the same emptiness did make ruff's
first-party classification machine-dependent (see `ruff.toml`), and it is a
shadowing hazard that only ever resolves correctly by luck of precedence.

**What:**
- `git rm` of the `src/common` gitlink and of `.gitmodules` (it held nothing else).
- `CLAUDE.md` imports `@../common/CLAUDE.md` instead of `@src/common/CLAUDE.md`.
- `ruff.toml` drops `extend-exclude = ["src/common"]` — there is nothing inside this
  repo to exclude. `known-first-party = ["common"]` stays, and matters *more* now:
  the package is outside the repo, so ruff's path-based resolver cannot classify it
  at all.
- `tests/conftest.py`, `README.md`, `.vscode/launch.json` updated to the sibling path.

**Consequence:** a clone of this repo alone is not runnable. It needs the flat layout
(`<top>/common/` beside `<top>/unit/`) and the `mast.pth` that MAST_provisioning
writes into the venv. `git submodule update --remote` is no longer how this repo gets
its `common`; a `git pull` in the sibling clone is. The fleet-wide `MAST_common/CLAUDE.md`
still documents the submodule layout for the other consumers and now disagrees with
this repo — it should be reconciled when they follow.

---

## [2026-08-02] The endpoint-contract change does not reach `main` without hardware verification


**Why:** The contract remediation (#42) is verified by ruff, by pytest suites, and by a smoke
harness on labcomp2 -- and all three share one blind spot. Every component under test is built
with `object.__new__` and handed a recording stand-in for the PWI4 client or the camera, because
the real ones need a telescope. That setup answers *did the handler refuse*, *did it delegate*,
and *what shape went on the wire*. It cannot answer whether the mount still slews, whether the
focuser still reaches its target, or whether `/unit/status` still parses downstream. The two
defects most likely to escape are precisely the ones a stand-in cannot see: a move that silently
no-ops, and a status field consumers read positionally.

The units make the gap easy to underestimate. `mast-unit` has been **stopped on all four since
2026-07-08**, and they track `main` by `git pull`, so a merge looks inert -- right up until the
next pull takes the entire set at once, on the first service start in weeks.

**What:** Merging `eli/endpoint-contract` into `main` requires a hardware pass on one unit
first, item by item, with evidence recorded on the integration PR (#77). Held as a **draft** so
the requirement is mechanical rather than a note someone has to remember; leaving draft is the
signal that the checklist is complete. The gate sits at that boundary only -- the tranche PRs
merge *into* the integration branch on code review alone, which is what the branch is for.

The checklist lives on #77 rather than here, since it is per-tranche and changes as tranches
land. Two limits are worth recording as durable facts rather than checklist items:

- **The imager verbs cannot be hardware-tested while `imager_type` is `phd2`.**
  `units.common.imager.imager_type` is `phd2` fleet-wide with no unit overriding it, so the
  ASCOM and ZWO code paths are never imported. Pointing a unit at `ascom:...` first requires the
  stale-import fix (#71/#72) -- on `main` that backend does not import at all.
- **The ZWO paths are out of reach entirely** until some unit is configured for that backend.
  They stay stand-in-verified, and that should be stated rather than glossed.

**Implications:** "Tests pass" is not a merge argument for this change, and neither is "it is
only an API shape". Anything that cannot be exercised on hardware ships explicitly labelled as
unverified, or does not ship. The same reasoning applies to the sibling epics when the contract
extends to `MAST_control` and `MAST_spec`.

---

## [2026-08-02] /mount/goto delegates to the maintained slew; alt/az deferred to its own endpoint

**Why:** `mount.goto()` was a second implementation of the slew. It called
`pw.mount_goto_ra_dec_j2000` directly and skipped `start_activity(MountActivities.Slewing)` and
`self.target`, which the maintained `goto_ra_dec_j2000` sets. So a slew commanded through the
API was invisible to `wait_until_settled(SettleMode.SLEW)` and to mount status -- the mount
looked idle while it moved. This is invariant 2 of the endpoint contract (#42), and this fork is
the example that motivated the whole review.

**What:** `endpoint_goto` is a thin handler over the maintained method: check `connected`,
delegate to `goto_ra_dec_j2000`. `mount.goto()` is gone, and so is `goto_ra_dec_apparent` --
unrouted, zero callers, and it carried the same missing-envelope defect.

**Alt/az is deliberately *not* part of this endpoint** (decision revised 2026-08-02, reversing
the 2026-07-20 contract note's "`goto` gains alt/az"). Horizontal pointing is not the same
operation with different numbers:

- **Tracking has to be stopped for it to mean anything.** With tracking on, the mount
  immediately starts drifting off the commanded alt/az -- the pointing the caller asked for
  does not hold. So the operation is "stop tracking, then slew", which is a different contract
  and a different abort/settle story, not an extra pair of query parameters.
- **The input validation differs.** Alt/az is decimal degrees; RA/Dec is conventionally
  sexagesimal (and `unit.expose` accepts both forms for RA/Dec). Folding them into one handler
  means one endpoint with two validation regimes selected by which parameters arrived.
- Its `target` bookkeeping also differs, since `status()` renders a tuple as RA/Dec and would
  mislabel a horizontal target.

Removing it also removes the argument plumbing it forced: with one coordinate system,
`ra_j2000_hours` / `dec_j2000_degs` are simply **required**, and FastAPI rejects a missing or
half pair with a 422 before the handler runs. The hand-rolled "exactly one complete pair"
validation (mixed pairs, half a pair, no coordinates) is gone with it.

Two decisions that stand:

- **Decimal only, for now.** `/mount/goto` previously typed its coordinates `float | str`,
  which was a lie: PWI4's client does `float(value)`, so a sexagesimal string raised inside the
  client and surfaced as a 500. Typing them `float` makes that a clean 422 instead. Sexagesimal
  RA/Dec is a reasonable future addition here (`common.parsers` already has the two parsers);
  it was the *azimuth* that had no usable parser, and that concern leaves with alt/az.
- **`GET` -> `PUT` applied here, not deferred to #48.** Invariant 5, and safe to take now: the
  only live cross-repo calls into the unit are `status`, `execute_assignment` and `abort`, and
  `/mount/goto` has no automated caller. #48's risk lives in `abort` (called as `GET` from
  `common`), not here. A slew is also the least defensible route to leave on `GET`, where a
  caching proxy or a Swagger "try it out" can fire it.

**Implications:** `/mount/goto` is behavior-preserving from a caller's perspective except the
verb and the parameter names, both safe because nothing automated calls it; what changes is
that a slew commanded through it is now tracked like any other. The alt/az endpoint is future
work (its own issue), and the contract note's goto row needs the same correction.

Two adjacent fixes came out of this and are in the same change, since both are in the status
field an operator reads immediately after issuing a goto:

- **`target_verbal` was rendering declination as arcseconds.** `Angle(self.target[1],
  unit='arcsec')` on a value that is degrees: a target at Dec +30.5 displayed as `0:00:30.500`
  instead of `30:30:00.000` -- off by 3600. Display-only, but the display an operator uses to
  confirm where the telescope is going.
- **The rendering moved out of `status()` into `Mount.target_verbal()`**, which is what makes it
  testable without a live mount (`status()` needs `pw.status()`, the ASCOM dispatcher and the
  power switch), and thins the status handler in the direction invariant 6 wants.

---

## [2026-08-02] Response-envelope remediation, part 3: the envelope belongs to the endpoint, not to status()


**Why:** `/unit/status` returned a `CanonicalResponse` while every component `/status` returned a
bare typed model (`MountStatus`, `FocuserStatus`, `CoverStatus`, `StageStatus`, `ImagerStatus`),
so a consumer could not write one status parser. The real cost is in `MAST_common`'s client:
`BaseApi._common_get_put` unwraps a `1.0` envelope and hoists remote `errors` into the caller's
error list, but a bare model takes the "NON canonical response" branch -- a warning on every
poll, and **no error channel at all**, since a typed model has nowhere to put `errors`. Control's
own idiom (`if not canonical_response.succeeded: ... abort()`, `common/models/plans.py`) is
therefore dead code when pointed at a component `/status`: a powered-off or faulted component
answers with a status model and the client scores it a success. That is a poor foundation for
invariant 3 (#43), which makes `/status` the completion-detection surface.

**What:** The wrapper moved to the endpoint boundary. All six routed `endpoint_status` handlers
return `CanonicalResponse(value=self.status())`; every `status()` keeps returning its bare typed
model. `Unit.status()` -- which wrapped *internally* -- now returns `FullUnitStatus`, and
`Unit.endpoint_status()` does the wrapping (keeping the `serialize_ip_addresses` call at the
boundary, where it always ran).

**Why not the other two options:** #47 framed this as "components wrap up to match the unit, or
the unit unwraps down to match the components." Both are wrong.

- *Unwrapping down* standardizes on the broken half: it makes the warning branch the official
  path for status, permanently forecloses an error channel on the completion surface, and needs
  an explicit carve-out in invariant 4.
- *Wrapping `status()` itself* cannot work. `FullUnitStatus`'s fields are **typed as** the
  component status models and `Unit.status()` populates them by calling each component's
  `status()` directly, so a `CanonicalResponse` cannot go in a field typed `MountStatus`. Forcing
  it through would nest envelopes inside the payload, turning `mount.tracking` into
  `mount.value.tracking` -- a field-path change in disguise, and every consumer of it fails
  *silently*: `MAST_control`'s `status_from_dict` catches the validation failure and falls back to
  `BaseStatus(detected=False, operational=False)` (so a healthy fleet reads as dead in the
  dashboard, with only a log line), `MAST_gui` does `FullUnitStatus(**value)`, and the GUI's SSE
  handler walks cache paths by field-name string and warns-and-continues on a miss. There are
  also live internal typed consumers -- `acquisition.py` and `acquirer.py` read
  `mount.status().ra_j2000_hours` as attributes.

`status()` has two callers with different needs -- the route wants an envelope, internal
composition and logic want the typed model. Serving both is not a compromise; it is invariant 6,
and every component already had the `endpoint_status` / `status` split to hang it on.

**Implications:** `FullUnitStatus` is untouched -- no field renamed, nothing nested -- so the
GUI, control's cache and the SSE walk are unaffected. `/unit/status`, the only live cross-repo
status consumer, keeps its wire shape byte-for-byte; only *where* the wrapping happens moved. The
five component `/status` routes do change on the wire (they gain the envelope, which is the
point) and have no automated consumers -- they are operator and Swagger surface. Consequently
this tranche needs **no `MAST_common` or `MAST_gui` change and no coordinated deployment**: the
bump-common-then-deploy-everything lockstep that #47 was expected to require does not apply. It
still applies to #48 and to any future change to the envelope itself.

---

## [2026-08-02] Response-envelope remediation, part 2: imager backends agree, annotations enforce it

**Why:** The imager verbs were the least consistent surface on the unit. `ASCOMImager` returned
proper envelopes with `errors=["not connected"]` / `["not exposing"]`; `ZWOImager` returned
`None` from the same verbs and did not import `CanonicalResponse` at all. Because
`Imager` is a pure delegating wrapper, the backend a unit happens to run decided what a caller
saw -- the same `PUT /unit/imager/stop_exposure` was an envelope on an ASCOM unit and `null` on
a ZWO one. `Imager` also advertised the defect in its own signatures as
`-> CanonicalResponse | None`; the `| None` *was* invariant 4's breach, written down.

**What:** ZWO's `abort` / `stop_exposure` / `abort_exposure` / `start_exposure` return envelopes,
using ASCOM's existing error strings verbatim so the two backends are indistinguishable to a
caller. `Imager`'s annotations for `abort`, `endpoint_abort`, `start_exposure`, `stop_exposure`
and `abort_exposure` drop `| None`, which makes a type checker reject a future backend that
returns nothing.

Two offenders outside the #47 audit list came out of the alignment and are fixed here, since
"the two backends agree" is false without them:

- **`ASCOMImager.abort_exposure` had its own `None` path** -- it appended `"not connected"` to
  `self.errors` and then bare-`return`ed, so the reference implementation was not actually clean.
- **`ZWOImager.start_exposure` returned `None`** while swallowing every exception into
  `self.errors`, so a failed exposure start reported success. It now ends the way ASCOM's does.
- **`PHD2Connector.abort` was `pass`** and `endpoint_abort` returned `stop_capture()`'s `None`.
  Both return `Ok`. Behavior is unchanged deliberately -- whether a PHD2 `abort` should do more
  than nothing is a question for #43, not an envelope fix.

**Implications:** Three decisions worth recording as *not* taken.

1. **The annotations were tightened only where the claim is now true.** `startup` / `shutdown`
   keep `| None`: `PHD2Connector.startup` is `pass`, `ASCOMImager.shutdown` falls off the end,
   and `ZWOImager` delegates to `super().startup()`. Dropping `| None` there would assert
   something false. That chain is the remaining #47 work on the imager, tracked in the issue
   rather than folded in silently. `connect` / `disconnect` keep `| None` for the part-1 reason:
   #42 deletes them.
2. **The backend contract already exists, in `MAST_common`.** The #47 plan assumed no backend
   ABC and proposed adding a Protocol under `src/imagers/`; that was wrong.
   `common/interfaces/imager.py` defines `ImagerInterface(Component, ABC)` with
   `@abstractmethod stop_exposure` / `abort_exposure`, and all four implementers
   (`ZWOImager`, `ASCOMImager`, `Imager`, `PHD2Connector`) derive from it. No new abstraction
   was introduced.
3. **The ABC's own return annotations are deliberately left for a separate change.** Declaring
   `-> CanonicalResponse` on `ImagerInterface` is the single-source-of-truth fix and is what
   would bind *future* backends, but it edits `MAST_common`, which means a submodule PR, a
   gitlink bump here, and re-syncing the other three `common/` checkouts. `ImagerInterface` is
   implemented only in MAST_unit (verified across control, spec and gui), so that change is
   safe whenever it happens and nothing waits on it. It is a cross-repo step, not an oversight.

---

## [2026-08-02] Response-envelope remediation, part 1: refusals are returned, not dropped

**Why:** Invariant 4 of the endpoint contract (#42) says every routed handler returns a
`CanonicalResponse`, with failures as `errors=[...]` -- never an implicit `None`, never an
exception across the HTTP boundary. #47 audited the violations. The consumer-visible harm is
not the missing envelope itself but what it hides: a handler that falls off the end sends a
`null` body, and `covers.open` / `close` used a bare `return` on the not-connected path, which
over HTTP is a success-shaped empty response. A caller cannot distinguish "I refused" from
"it worked."

**What:** The offenders in this tranche now return an envelope --
`mount.goto_ra_dec_j2000`, `unit.abort`, `focuser.shutdown`, `covers.open` / `close` /
`shutdown`, `focuser.endpoint_move_in` / `_out` (were discarding `move()`'s response), and the
`/focuser/position` getter (was a raw `int`).

Two of these needed more than a `return` statement:

- **`focuser.endpoint_set_position` returned `Ok` even when nothing moved.** The refusal
  decision (not-powered / not-connected) lives in the `position` property setter, and a Python
  setter cannot return anything -- assignment discards it. Extracted `Focuser.goto_position()`,
  which owns the decision and returns a `CanonicalResponse`; the setter delegates to it and
  drops the result, so the existing `self.position = x` call sites (autofocusing, `move`,
  `goto_known_as_good_position`) keep working unchanged. The endpoint calls `goto_position`
  directly. Re-checking the preconditions inside the handler was the alternative and was
  rejected: it duplicates the decision and puts logic in a handler, violating invariant 6.
- **`covers.shutdown` on the not-connected path returns `Ok`, not an error.** Powering off
  *is* the shutdown for a disconnected cover -- it succeeded. Only `open` / `close` genuinely
  could not act, so only those two report `errors=["not connected"]`.

`Imager.connect` / `disconnect` also return `None` and were deliberately left alone: #42 slates
them for deletion (not ABC-enforced, unused, Arie OK'd), so fixing them is work that gets
deleted.

**Implications:** Six component endpoints change what they put on the wire, all in the
refusal direction -- a caller that previously got `null` or an empty body now gets
`{"api_version": "1.0", "errors": [...]}`. Nothing live consumes them: the only cross-repo
calls into the unit are `GET /unit/status`, `PUT /unit/execute_assignment` and
`GET /unit/abort`, and of those only `unit.abort` is touched here (it gained an envelope where
it sent `null`). No `MAST_common` or `MAST_gui` change, so no submodule-bump lockstep.

Behavioral verification cannot happen on a development Mac: `focuser.py`, `mount.py` and
`covers.py` all import `win32com` at module scope, so the three files changed here are
un-importable off Windows and the pytest suite skips (as README already documents). This
tranche was verified by ruff (no new findings against `origin/main`) and byte-compilation; the
refusal paths need an HTTP smoke pass on a unit, and permanent coverage belongs to the #52
contract suite rather than to hand-written per-site tests that could only ever run on the
units.

---

## [2026-07-23] Fixed limit frame sent verbatim; per-call endpoint override dropped

**Why (verbatim):** The `fixed` arm routed the DB rectangle through `ImagerRoi`,
whose `model_post_init` conditioning shifts and shrinks it (−1 center bias, mod-16 /
mod-4 trim for the max supported binning — see MAST_common#17). That conditioning is
unnecessary for the limit frame: PHD2 applies the camera alignment constraints
itself (upstream PRs #1374–#1376, present in the deployed MAST build), and
`set_limit_frame` takes unbinned full-sensor coordinates. A deliberately placed
frame must not move.

**What (verbatim):** The `fixed` arm now builds the ROI with
`ImagerRoi.verbatim(...)` (MAST_common, same day) — the configured rectangle IS the
wire value. The interim configured-vs-applied WARNING is removed (nothing mutates
anymore). Tests pin verbatim pass-through, including a rect conditioning would
demonstrably have mutated and one with camera-illegal odd dimensions.

**Why (dropped):** A per-call override — `limit_frame_mode` (+ rect) parameters on
`endpoint_start_acquisition_and_guiding`, threaded through `Acquisition` into
`start_guiding()` — was considered as an alternative to DB updates. Dropped because
of forgotten-on-restart: `validate_guiding()` stops and restarts guiding internally
via a bare `self.start_guiding()`, so a pass-through override would silently revert
to the DB mode mid-session; stashing the override on the connector would fix that
but reintroduces exactly the sticky hidden state (PHD2's registry-persisted limit
frame) this feature exists to escape.

**Implications:** The DB `phd2.limit_frame` document remains the single source of
truth for the guiding-phase limit frame; per-call experimentation means editing the
DB doc. If the endpoint override is ever revisited, the internal-restart path must
carry the override explicitly (e.g. on `Acquisition`) before it is trustworthy. The
existing `use_set_limit_frame` endpoint parameter is unaffected — it governs only
the acquisition-phase (sky/spec) exposures.

---

## [2026-07-23] Limit-frame config selects by `mode`; `start_guiding()` dispatches on it

**Why:** Companion to MAST_common's same-day rename of `phd2.limit_frame` from an
enabled-flag to a `mode` discriminator (`derived | full_frame | fixed`) — the flag
read backwards (`enabled: false` was the state operators actually want) and an
incomplete rectangle degraded silently to the derived ROI. Renamed before any
merge/deploy, so no migration.

**What:** `start_guiding()` replaces the boolean + `has_roi` conditionals with a
three-arm `match` on `LimitFrameMode`: `full_frame` → `use_set_limit_frame=False`
(reset to full frame), `derived` → the fiber/margin-derived guiding ROI as before,
`fixed` → the configured rectangle through `ImagerRoi` (the conditioning WARNING
from the 2026-07-22 entry below stays on the `fixed` arm). Tests updated to the
mode vocabulary; the no-DB-section safe-to-land invariant is unchanged.

**Implications:** Deploy-time DB docs use `{ mode: "full_frame" }` for hand-patch
parity, `{ mode: "fixed", x, y, width, height }` for an explicit band. A rectangle
under a non-`fixed` mode now fails config parse loudly instead of being ignored.

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
