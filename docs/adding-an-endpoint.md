# Adding an endpoint

The HTTP surface is **declared, not discovered**: a handler states its tier where it is
defined, and registration refuses one that does not. Get the declaration right and the
transport, the response envelope and the Swagger grouping follow.

If this file disagrees with the code, the code wins — see *Keeping this current*.

Where the text below says *a check* catches something, it means one of the static passes in
*The static checks*, near the end. Those live on a branch and are run deliberately — they are
not in this tree and no build runs them, so a check catching something is an audit finding, not
a red build. What does fail on its own is the import-time refusal of an undeclared handler.

## 0. Check whether you need to write one

`startup`, `shutdown`, `abort` and `status` on a component already exist: they are the
INTERFACE tier, generated from declarations on the `Component` ABC in `MAST_common`. Implement
the concrete method — no decorator — and `register_component_endpoints` emits the route. One
declaration is why `startup` is `PUT` and `status` is `GET` on every component.

## 1. Declare it at the definition site

```python
@endpoint(tier=Tier.OPERATION, completion=MountActivities.Slewing)
def endpoint_park(self, ...) -> CanonicalResponse:
```

Keyword-only, and `tier` is mandatory: routing an undeclared handler raises
`UndeclaredEndpointError` **at import**.

### tier

| tier | use it for |
|---|---|
| `CONTRACT` | orchestration a programmatic client depends on. The name is wire contract with `MAST_control` and the shared plan client — **grep `common/models/plans.py` before renaming one** |
| `OPERATION` | operator and diagnostic verbs; the day-to-day manual surface |
| `INTERFACE` | generated only. Never declare it by hand |
| `DEMO` | parked demonstrations. Implies `deprecated=True`, so the route renders struck through |

The tier publishes per operation as `x-stability`, so a client's own test can assert it never
calls an operator verb. It is also the Swagger group, except on `OPERATION`: an operator route
is grouped by **area** instead — the path segment before its verb, so `/unit/mount/park` files
under *Mount (operator)* and `/unit/expose` under *Unit (operator)*. Nothing to declare; the
group is read from where the route is mounted (#207).

A **new component** needs one entry in `OPERATOR_AREAS` in `src/app.py` for its group to be
described and ordered; `test_every_component_has_an_operator_group` fails until it is there.

**Do not pass `tags=`** — it is ignored with a warning, and a check keeps this tree free of them.

### completion

How the caller learns the operation finished; publishes as `x-completion`. Declare one whenever
the operation outlives its response. Nothing forces you to, and no declaration is honest about
being unclassified.

| form | the caller |
|---|---|
| `Completion.IMMEDIATE` | is done when the response arrives |
| `Completion.BLOCKING` | waits: the response is withheld until the hardware is done |
| an activity flag, e.g. `MountActivities.Slewing` | is answered at once, then watches that flag clear in `status` |
| a notification channel, e.g. `NotificationChannel.ASSIGNMENT` | is answered at once, then reads that `Notifier` stream (`execute_assignment` is the live case) |

Exactly one flag member — "watch these two bits" is not a signal a caller can act on. **The
signal has to be one the handler really raises:** a check verifies that, because publishing a
signal you never send leaves a client waiting forever with nothing looking wrong.

### the optional arguments

| argument | when to pass it |
|---|---|
| `methods=` | generated verbs only. A hand-registered route carries its verb at the registration site, next to the path |
| `factory=True` | when a default depends on loaded configuration. A signature default is evaluated at import, before `Config()` exists, so the handler has to come back from a closure instead. `/unit/spiral_new_path` binds this unit's own fiber position that way |
| `stability=` | not yet: `DEMO` already implies deprecated, and nothing in this repo declares `DEPRECATED`. It is the retirement notice for the next route to be removed |

## 2. Register through the helper

```python
add_api_route(router, "/mount/park", endpoint=self.endpoint_park, methods=["PUT"])
```

- **Never `router.add_api_route`** — it bypasses the declaration refusal, the envelope and the
  tier tag at once. A check catches it, since the import-time refusal cannot.
- **`PUT` if it changes state, `GET` only for reads.** Nothing checks this; see *What is not
  enforced*.
- Two components serving one path leaf must use **the same parameter names** (order is free —
  query parameters are named). `/position` took `pos` on the stage and `position` on the
  focuser, so a client that drove one got a 422 from the other.

## 3. Preflight, then the expensive part

Refuse before you spend: parameter validation → component preconditions → configuration
completeness → dependency reachability → *only then* the work.

**Expensive** is anything committing time, hardware motion or state the caller cannot cheaply
undo: a slew, a stage or focuser move, an exposure, a plate solve, a file copy off the RAM disk
— **and a thread dispatch**. A route that dispatches a thread and answers `Ok` has already
spent the request.

Push what you can up to the parameter layer, where it is free and appears in the schema:

```python
az_degs: Annotated[float, Query(ge=0, lt=360)]
```

A `pattern=` accepts the *form* only, so range checks still belong to the parser — which is why
the equatorial verbs validate again after the pattern passes.

Then guard in the body, and refuse with information:

```python
if not self.connected:
    return CanonicalResponse(errors=[f"{op}: not connected"])
```

Name a reusable guard `require_*`. **Never `assert` for a runtime guard**: it is stripped under
`python -O`, the envelope renders `AssertionError` as a generic error indistinguishable from a
bug, and it carries no message for the caller.

## 4. Return the bare value

The envelope is applied once, at registration. So a handler:

- returns its typed model or a plain value — **never `CanonicalResponse(value=...)`**;
- refuses with `CanonicalResponse(errors=[...])`, and uses `CanonicalResponse_Ok` when "ok" is
  genuinely the answer;
- **never** bare-`return`s, returns `None`, or lets an exception escape.

A check rejects both a hand-built envelope and a handler able to answer `None`, because neither
is visible at run time: the wrapper passes an existing envelope through, and `value=None` cannot
tell a refusal from a success.

One load-bearing exception: **`status()` returns its bare typed model.** `FullUnitStatus`'s
fields are typed as the component status models, so an envelope nested in the payload would
break every consumer silently.

## 5. Name a thread target `do_<operation>`

```python
Thread(name="expose", target=self.do_expose).start()
```

Dispatcher `<operation>`, target `do_<operation>`, so a dispatch site says what is running
without the reader opening the target. A check enforces it.

## 6. Run the suite

```bash
ruff check . && ruff format --check .
python -m pytest tests -q              # the full suite
```

An undeclared route needs none of this to be caught: `common.endpoints.add_api_route` raises
`UndeclaredEndpointError` at import, so the app will not start.

## The static checks — where they are, and what each one refuses

The contract's static half is a set of pure AST passes — no hardware, no Mongo, no app fixture —
that run on any platform in about two seconds. **They are not in this tree.** They live on the
branch `eli/contract-enforcement` (`#184`), deliberately unlanded: the contract is audited by
running them against a checkout, not by gating CI or the runtime.

```bash
git checkout eli/contract-enforcement
python -m pytest tests/contract -q
```

A check failing on something deliberate wants an entry in its `KNOWN_*` dict, keyed to the issue
that owns the fix — not a silenced check. Only **new** findings fail; an entry that has stopped
being true is warned about instead, so read the warnings summary when you land a fix and drop
the entry you just made stale.

Open this table when one goes red.

| module | refuses |
|---|---|
| `test_endpoint_declarations.py` | a route served without a declaration, a declaration that is not routed, a route registered straight on the router, and a component that stops generating its interface verbs |
| `test_envelope_ownership.py` | a handler building its own envelope, or able to answer `None` |
| `test_completion_declarations.py` | a component answering two different completion conventions |
| `test_completion_flags.py` | a declared signal — activity flag or notification channel — the handler never raises |
| `test_activity_flag_balance.py` | an activity flag that starts and never ends, and so hangs a waiter |
| `test_dispatch_naming.py` | a thread target not named `do_<operation>` |
| `test_route_parameter_names.py` | two components serving one path leaf with differently *named* parameters |
| `test_abstract_declarations.py` | an `@abstractmethod` with no declared return type — the drift that let `/imager/status` 500 |

## What is not enforced

Three gaps, so nobody mistakes review for machinery. What the softening pass withdrew or
relaxed, and the audit that revisits it, is `#178`.

- **The verb.** No check reads `methods=`; the `GET`→`PUT` sweep was a scripted pass whose
  residue is enumerated on `#48`. The generated verbs are safe by provenance, the rest rest on
  the reviewer. `/unit/abort` answers both deliberately — the shared plan client aborts the
  fleet with `GET` (`MAST_common#51`, removal on `#113`).
- **The preflight ordering.** Section 3 is a discipline: nothing stops a handler slewing first
  and validating second. `#179` would make it structural, by running declared preconditions at
  registration.
- **A CONTRACT-tier route name.** Rename one on one side of the wire and nothing fails at
  import — it is a 404 in the field. Grep the client (`#178` W3, `#35`).

## Keeping this current

By hand: adding a check, a tier or a completion form means editing this file in the same change.
The test that asserted this file named every check and every `Tier` was withdrawn as `#178` W1,
whose revisit compares the check modules on `eli/contract-enforcement` against the table above.
