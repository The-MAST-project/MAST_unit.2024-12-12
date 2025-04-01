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
