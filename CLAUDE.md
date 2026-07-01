@src/common/CLAUDE.md

# MAST_unit — Claude Guidance

Per-unit telescope-hardware backend. Runs on each unit machine (`mast01`…`mast20`). Submodules `MAST_common` as `./src/common/`.

## Running

```bash
cd src
MAST_PROJECT=unit python app.py
```

## Gotchas

### `solve-field` needs lapack on PATH
Any Windows-side wrapper around the cygwin `solve-field.exe` must prepend **both** `C:\cygwin64\bin` **and** the POSIX path `/usr/lib/lapack` to `PATH` before spawning the solver:

```python
env["PATH"] = r"C:\cygwin64\bin" + os.pathsep + "/usr/lib/lapack" + os.pathsep + env.get("PATH", "")
```

Without `/usr/lib/lapack`, the `removelines` step imports numpy, fails to load `cyglapack-0.dll`, and the solve dies with a numpy `ImportError` and no `.solved` marker. Reference: `src/solvers/mastrometry.py`.

### ps3cli `--server` requires a star catalog at boot
`ps3cli.exe --server` validates a catalog on startup and exits (code 2) if absent. The minimal catalog that satisfies boot is `<cat>\UC4\Index.UC4` (may be empty) plus non-empty `<cat>\Orca\Orca0025.orc`, `StarOrca0025.orc`, `DistOrca0025.orc`. Because the unit service runs as **LocalSystem** (NSSM with no `ObjectName`), `Path.home()` resolves to `systemprofile`, not `C:\Users\mast` — so set machine-scope `PS3CLI_DIR` and `PS3CLI_CATALOG` env vars so the app finds `ps3cli.exe` and its catalog.
