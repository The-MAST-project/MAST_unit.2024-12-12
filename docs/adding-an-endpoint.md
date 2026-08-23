# Adding an endpoint

The HTTP surface is **declared, not discovered**: a route states what it is at the place it
is defined, and registration refuses anything that does not. So adding an endpoint is mostly
a matter of making the right declaration — the transport, the response envelope and the
Swagger grouping all follow from it, and none of them is your code to write.

This describes the contract as it stands (`#42`). If you find it disagreeing with the code,
the code wins and this file is stale — see *Keeping this current* at the end.

## 0. Check whether you need to write one

If what you want is `startup`, `shutdown`, `abort` or `status` on a component: **it already
exists.** Those four are the INTERFACE tier and they are *generated* for every component from
declarations on the `Component` ABC in `MAST_common`. Implement the concrete method — no
decorator, the declaration lives on the ABC — and `register_component_endpoints` emits the
route.

That is also why components cannot drift on those four: there is one declaration, so
`startup` is `PUT` and `status` is `GET` everywhere, by provenance rather than by review.
A static check asserts every component's `api_router` actually calls the generator, because
dropping the call would remove four routes and break nothing else.

## 1. Declare it at the definition site

```python
@endpoint(tier=Tier.OPERATION, completion=MountActivities.Slewing)
def endpoint_park(self, ...) -> CanonicalResponse:
```

The decorator is keyword-only, and `tier` is mandatory: registering a handler that declares
nothing raises `UndeclaredEndpointError` **at import**, not at request time.

### tier

| tier | use it for |
|---|---|
| `CONTRACT` | unit orchestration a programmatic client depends on. The name is part of the wire contract with `MAST_control` and the shared plan client, so **renaming one is a cross-repo change** — grep `common/models/plans.py` for the literal before you touch it. Nothing enforces this; the shared-enum mechanism that did was withdrawn (MAST_unit#178 W3, #35) |
| `OPERATION` | bespoke operator / diagnostic verbs — the day-to-day manual surface. Paths stay literal by design: no programmatic client, so an enum entry would imply a promise the contract does not make |
| `INTERFACE` | generated only. Do not declare this by hand |
| `DEMO` | parked demonstrations. Implies `deprecated=True`, so the route renders struck through and cannot be mistaken for live |

The tier is also the Swagger group and is published per operation as `x-stability`, so a
client's own test can assert it never reaches for an operator verb. **Do not pass `tags`** —
the declaration is the group. Passing one is ignored with a warning rather than refused, so
that adopting the helper in another service is not a flag day; a static check keeps this tree
free of them.

### completion

How a caller learns the operation finished. Published as `x-completion`.

| form | meaning |
|---|---|
| `Completion.IMMEDIATE` | finished when the response arrives |
| `Completion.BLOCKING` | the response is withheld until the hardware is done |
| an activity flag, e.g. `MountActivities.Slewing` | returns at once; the caller watches that flag clear in `status` |
| a notification channel, e.g. `NotificationChannel.ASSIGNMENT` | returns at once; completion is announced on that `Notifier` stream. `execute_assignment` is the live case |

Exactly one flag member — "watch these two bits" is not a signal a caller can act on.

**Declare one whenever the operation outlives its response.** Nothing forces you to (the gate
that did was withdrawn — MAST_unit#178 R4), and an absent declaration publishes no
`x-completion` at all, which is honest about being unclassified. Two checks still hold what you
do declare to account: a component answers one convention rather than two, and **the signal you
name is one the handler actually raises**. That second one matters most — publishing a signal
you never send is worse than declaring nothing, because a client waits on it forever and
nothing looks wrong.

### the rest

- `methods` — only for generated verbs. A hand-registered route carries its verb at the
  registration site, next to the path.
- `factory=True` — only when the handler must be *built* at registration because a default
  depends on loaded configuration. A signature default is evaluated at import, long before
  `Config()` exists, so binding this unit's own configured values into the OpenAPI schema
  means returning the handler from a closure. `/unit/spiral_new_path` is the live example:
  its `center_x` / `center_y` defaults are the unit's own fiber position.
- `stability` — leave it. `DEMO` already implies deprecated, and `DEPRECATED` currently has
  no users in this repo; it is the retirement-notice mechanism for the next route to go.

## 2. Register through the helper

```python
add_api_route(router, "/mount/park", endpoint=self.endpoint_park, methods=["PUT"])
```

- **Never `router.add_api_route`.** It bypasses the declaration refusal, the envelope and the
  tier tag all at once. A static check catches the bypass, because that is the one thing the
  import-time refusal cannot see.
- **`PUT` if it changes state; `GET` only for reads.** See *What is not enforced* below —
  this one is on you and the reviewer.
- If two components serve the same path leaf, **their parameter names must match**. A check
  compares them across components. This exists because `/position` took `pos` on the stage and
  `position` on the focuser, so a client that could drive one got a 422 from the other. Order
  is not checked — query parameters are named, so it cannot break a client.

## 3. Preflight, then the expensive part

Everything that can refuse the request happens **before** anything expensive starts, in this
order:

1. parameter validation
2. component preconditions
3. configuration completeness
4. dependency reachability
5. *only then* the work

**Expensive** means anything committing time, hardware motion, or state the caller cannot
cheaply undo: a slew, a stage or focuser move, an exposure, a plate solve, a file copy off
the RAM disk — **and a thread dispatch.** A route that dispatches a thread and answers `Ok`
has already spent the request.

Push what you can up to the parameter layer, where it is free and self-documenting:

```python
az_degs: Annotated[float, Query(ge=0, lt=360)]
```

FastAPI refuses out of range with a 422 before your body runs, and the bound appears in the
schema. Note that a `pattern=` accepts the *form* only — range checks still belong to the
parser, which is why the equatorial verbs validate again after the pattern has passed.

Then guard in the body, and refuse with information:

```python
if not self.connected:
    return CanonicalResponse(errors=[f"{op}: not connected"])
```

Name a reusable guard `require_*`. **Do not use `assert` for a runtime guard** — it is
stripped under `python -O`, it raises `AssertionError` that the envelope renders as a generic
error indistinguishable from a bug, and it carries no message for the caller. The check that
asserted every `require_*` has a caller is withdrawn (MAST_unit#178 W2); declaring preconditions
on `@endpoint` so registration runs them is MAST_unit#179, and that is where the convention gets
machinery again.

## 4. Return the bare value

The response envelope is applied **once, at registration**. Your handler therefore:

- returns its typed model, or a plain value — **do not build `CanonicalResponse(value=...)`**;
- refuses with `CanonicalResponse(errors=[...])`, and uses `CanonicalResponse_Ok` where "ok"
  is genuinely the answer;
- **never** bare-`return`s, never returns `None`, and never lets an exception escape.

A check rejects a hand-built envelope and any handler able to answer `None`. Neither is
visible at run time — the wrapper passes an existing envelope straight through, and a `None`
return produces a well-formed envelope that simply cannot tell a refusal from a success.
That indistinguishability is the whole defect class.

One exception, and it is load-bearing: **`status()` itself returns its bare typed model.**
`FullUnitStatus`'s fields are typed as the component status models, so an envelope nested
inside the payload would break every consumer silently. The envelope goes on at the boundary.

## 5. Name a thread target `do_<operation>`

```python
Thread(name="expose", target=self.do_expose).start()
```

Dispatcher `<operation>`, target `do_<operation>`, so a dispatch site says what is now
running without the reader opening the target. A check enforces it — and it exists because
the convention regressed within hours of being written down, in a good-faith fix.

## 6. Run the checks

```bash
python -m pytest tests/contract -q     # ~2 s, any platform, no hardware
ruff check . && ruff format --check .
python -m pytest tests -q              # the full suite
```

If a check fails on something deliberate, **do not silence it** — add an entry to that
check's `KNOWN_*` dict, keyed to the issue that owns fixing it. Only **new** findings fail; an
entry that is no longer true is reported in pytest's warnings summary instead, so read that
summary when you land a fix and remove the entry you just made stale (MAST_unit#178 R1).

## The checks, and what each one refuses

When one of these goes red, this is the file to open. All are pure AST passes — no hardware,
no Mongo, no app fixture — so they run on any platform in about two seconds.

| module | refuses |
|---|---|
| `test_endpoint_declarations.py` | a route served without a declaration, a declaration that is not routed, and any route registered directly on the router, bypassing the helper |
| `test_envelope_ownership.py` | a handler building its own envelope, or one able to answer `None` |
| `test_completion_declarations.py` | a component answering two different completion conventions |
| `test_completion_flags.py` | a declared completion signal — activity flag or notification channel — the handler never actually raises |
| `test_activity_flag_balance.py` | an activity flag that starts and never ends, and so hangs a waiter |
| `test_dispatch_naming.py` | a thread target that is not named `do_<operation>` |
| `test_route_parameter_names.py` | two components serving one path leaf with differently *named* parameters |
| `test_abstract_declarations.py` | an `@abstractmethod` with no declared return type — the drift that let `/imager/status` 500 |

## What is not enforced

Three gaps, stated so nobody mistakes review for machinery. A fourth register — everything the
softening pass withdrew or relaxed, with the audit that revisits the call — is MAST_unit#178.

**The verb.** None of the static checks reads `methods=`. The `GET`→`PUT` sweep was a
one-time scripted pass and its residue is enumerated on `#48`, not eliminated. The four
generated interface verbs are safe by provenance; every hand-registered route rests on the
author and the reviewer. One deliberate exception exists — `/unit/abort` answers both
methods, because the shared plan client aborts the fleet with `GET`, and `PUT`-only would
405 the fleet's abort path (`MAST_common#51`, removal tracked by `#113`).

**The preflight ordering.** Section 3 is a discipline, and now entirely one: nothing stops a
handler slewing first and validating second. The mechanism that would close it — preconditions
declared on `@endpoint` and run by registration, so the handler *cannot* go first — is
`#179`, with the static half remaining the unbuilt part of `#52`.

**A CONTRACT-tier route name.** Renaming one reaches `MAST_control` and the shared plan client,
and nothing fails at import if only one side moves — it is a 404 in the field. The shared enum
that made this structural was withdrawn (`#178` W3, `#35`): grep the client for the literal.

## Keeping this current

By hand. `tests/contract/test_endpoint_guide.py` used to assert that this file names every
contract-check module and every `Tier` member; it was withdrawn as `#178` W1 — a test over prose
was the cheapest thing in the suite to give up, and it could never verify that a paragraph was
*true*, only that it was complete.

So: adding a check, a tier or a completion form means editing this file in the same change.
`#178`'s revisit asks whether that discipline held, by comparing `ls tests/contract/test_*.py`
against the table above.
