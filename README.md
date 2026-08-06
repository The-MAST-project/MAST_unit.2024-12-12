This repository contains the 'unit' part of the **MAST** project.  It needs the `MAST_common` repo as submodule

The software controls a **MAST** unit which includes:
* An EDGE class computer
* A managed DLI power switch
* A PlaneWave L550 mount
* A PlaneWave Hedrik focuser
* A PlaneWave covers unit
* A Standa translating stage
* A ZWO ASI294MM camera

Provides (via FastAPI) `autofocus` and `acquisition` interfaces

## Tests

`tests/` holds a pytest suite that drives the real connector code with mocked
collaborators — no PHD2 process, no hardware, no Mongo. The import chain is
Windows-only today (`stage.py` needs pyximc), so the suite runs in the unit
venv and skips cleanly elsewhere. Install `requirements-dev.txt` into the
venv, then from the repo root:

```
python -m pytest tests/ -v
```

MAST_common carries its own platform-independent suite, run from its own clone
(`<top>/common/tests/`). It is a sibling of this repo, not a submodule.
