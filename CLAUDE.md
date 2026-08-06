@../common/CLAUDE.md

# MAST_unit — Claude Guidance

Per-unit telescope-hardware backend. Runs on each unit machine (`mast01`…`mast20`). Imports `MAST_common` as `common`, which is cloned as a **sibling** of this repo in the flat layout (`<top>/common/`, `<top>/unit/`) and put on `sys.path` by the `mast.pth` the provisioning writes into the venv. It is no longer a submodule.

## Running

```bash
cd src
python app.py   # role + identity come from the bootstrap config file
                # (/etc/wis/config.toml, or C:\WIS\config.toml on Windows);
                # set MAST_CONFIG to point elsewhere for dev/tests
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
Plate-solve corrections are commanded in the **RA/DEC sky frame** (`mount_offset(ra_add_arcsec=…` / `ra_add_gradual_offset_arcsec=…`). The old wait loop tracked completion on the **axis0/axis1** offset accumulators (`while ra_progress < 1 or dec_progress < 1`, reading `offsets.axis0_arcsec/axis1_arcsec.gradual_offset_progress`) and then fell back to `while mount.is_moving`. That **worked, but not reliably** — two durable traps to preserve:

- **`gradual_offset_progress` is signed by offset direction** — it ramps `0 → ±1.0` (and may overshoot past `|1|`), so ramp-complete is `abs(progress) >= 1.0`. The old unsigned `progress < 1` test never releases a negative-direction ramp (`-1 < 1` stays true). See `_gradual_ramp_complete` in `src/mount.py`.
- **`Mount.is_moving` is a slew detector, not a settle gate** — it is `axis0/axis1 rms_error` following-error: True during slews/homing/parking, but **False during tracking and the small offsets the servo keeps up with**, and it is recomputed on a timer so a `while is_moving:` entered right after a move can read a stale pre-move `False`. It cannot gate a post-offset settle.

The fix settles via `Mount.wait_until_settled(SettleMode.SLEW | OFFSET_STEP | OFFSET_GRADUAL, channels=("ra","dec"))` — tracking the channels actually commanded with the signed-progress test (or `is_slewing` for slews), guarding the start-of-move race with a grace window, then doing a final following-distance settle.

## Project-wide LLM guidance

Cross-repo LLM guidance for MAST lives in the **`mast-claude-config`** repo (`github.com/The-MAST-project/mast-claude-config`) — the overarching home for project-wide instructions (shared coding standards, team working-style, global environment facts), deployed into `~/.claude/` by its `setup.sh`. Keep repo-specific guidance in this file; put genuinely cross-repo guidance there. See `mast-claude-config/CLAUDE.md` for what belongs where.
