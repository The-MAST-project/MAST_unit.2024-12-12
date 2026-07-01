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

### Mount offsetting: settle on the channel you commanded, never on `is_moving`
Plate-solve corrections are computed and commanded in the **RA/DEC sky frame** (`mount_offset(ra_add_arcsec=…)` / `ra_add_gradual_offset_arcsec=…`), so completion must be tracked on the **same** PWI4 offset channel. PWI4 exposes distinct offset accumulators (`_OFFSET_CHANNELS = ra, dec, axis0, axis1, path, transverse`): commanding `ra`/`dec` never advances `axis0`/`axis1`. An earlier bug polled `offsets.axis0_arcsec`/`axis1_arcsec` progress while commanding `ra`/`dec` — so the offset looked like it never completed and the code fell back to `while mount.is_moving`, which never gated correctly. Two durable traps this leaves:

- **`gradual_offset_progress` is signed by offset direction** — it ramps `0 → ±1.0` (and may overshoot past `|1|`), so ramp-complete is `abs(progress) >= 1.0`, **never** `progress >= 1.0`; the naive test hangs every negative-direction offset until timeout. See `_gradual_ramp_complete` in `src/mount.py`.
- **`Mount.is_moving` is a slew detector, not a settle gate** — it is `axis0/axis1 rms_error` following-error (True during slews/homing/parking, but **False during tracking and the small offsets the servo keeps up with**), and is recomputed on a timer so a `while is_moving:` right after a move can read a stale pre-move `False`.

Always settle via `Mount.wait_until_settled(SettleMode.SLEW | OFFSET_STEP | OFFSET_GRADUAL, channels=…)`, which waits on the commanded channels' signed progress (or `is_slewing` for slews), guards the start-of-move race with a grace window, then does a final following-distance settle.
