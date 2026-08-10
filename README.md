This repository contains the 'unit' part of the **MAST** project.  It needs the `MAST_common` repo as a sibling clone (`<top>/common/`, reached through the venv's `mast.pth` — not a submodule; see `DECISIONS.md` 2026-08-06)

The software controls a **MAST** unit which includes:
* An EDGE class computer
* A managed DLI power switch
* A PlaneWave L550 mount
* A PlaneWave Hedrik focuser
* A PlaneWave covers unit
* A Standa translating stage
* A ZWO ASI294MM camera

Provides (via FastAPI) `autofocus` and `acquisition` interfaces

## Running the service

```
python app.py [--log-level DEBUG]
```

`app.py` splits into three pieces, and the split matters if you import it:

- `start_supporting_processes()` starts PWI4, PWShutter and ps3cli and waits up to 30 s
  for PWI4 to answer. Called from `main()` only. **Importing `app` starts nothing.**
- `create_app(unit)` builds the FastAPI app — exception handlers, CORS, the favicon
  route — and mounts the unit router plus every component router the unit managed to
  build. Called with no argument it returns the bare app, which is what a schema-only
  or test caller wants.
- `main()` validates the configuration, builds the `Unit`, calls the other two, and
  hands the app to uvicorn.

There is no module-level `app` object: an app needs a `Unit`, and building one needs
Windows, the device drivers and Mongo. `uvicorn app:app` therefore does not work —
use `python app.py`, or `create_app()` if you are constructing one yourself.

## Tests

`tests/` holds a pytest suite that drives the real connector code with mocked
collaborators — no PHD2 process, no hardware, no Mongo. The import chain is
Windows-only today (`stage.py` needs pyximc, and the component modules need
`win32com`), so the suite runs in the unit venv and skips cleanly elsewhere.
Install `requirements-dev.txt` into the venv, then from the repo root:

```
python -m pytest tests/ -v
```

`test_app_factory.py` is the exception to "Windows-only": it covers `create_app()`
without importing a component module, so it runs anywhere. It pins the three defects
that made the HTTP surface untestable — routers mounted only under `__main__`,
processes spawned at import, and a `lifespan` that reached for a global the `__main__`
block happened to bind.

`test_response_envelope.py` covers invariant 4 of the endpoint contract (#42):
every routed handler returns a `CanonicalResponse`, with refusals as `errors`.
It builds components with `object.__new__` and gives them only the state the
path under test reads — the method under test is always the real one — and
drives two cases through a real FastAPI app to pin the wire shape.

MAST_common carries its own platform-independent suite, run from its own clone
(`<top>/common/tests/`). It is a sibling of this repo, not a submodule.
