"""
Mount-stability campaign -- walk a fixed alt/az mesh, recording servo telemetry at each
cell first UNGUIDED and then GUIDED.

Owned by the Unit, not by the Mount. The dwell drives the mount, the imager and PHD2,
and Mount has no business reaching into the guider; `SpiralSearch` coordinates the same
way and for the same reason. What belongs on Mount is the *monitor* (a rolling stability
metric on MountStatus) -- a separate thing, not this.

Why the parts are shaped the way they are:

* **The cell is derived from a slot clock, not from a stored position.** Two units run
  the campaign so that a cell can be measured on two OTAs *under identical wind*; that is
  the whole reason for the second unit. Two units each walking from their own checkpoint
  drift apart within an hour, and then the pair is unit A at 20:00 against unit B at
  23:00, with the evening wind decaying in between -- the between-condition variance the
  campaign exists to eliminate. Deriving the cell from a shared epoch makes both units
  visit the same cell at the same moment with no coordination protocol, and a unit that
  loses a cell to a wrap or a failed star rejoins at the next slot instead of
  desynchronising for the rest of the night.

* **Resume is that same mechanism.** A restarted unit computes the current slot and
  carries on; there is no "last position" file to desync from what was actually measured.

* **The traversal rotates by a stride coprime with the mesh size.** Visiting cells in
  plain order would advance azimuth monotonically with the hour, and the evening wind
  decay would be attributed to azimuth. Note a plain resume-where-you-stopped walk does
  not fix this: at ~4 passes over 40 cells a night, the walk lands back on cell 0 each
  evening and every cell keeps its time-of-night slot forever.

* **Raw samples are written, not summary statistics.** PWI4 serves distinct drive samples
  at ~44 Hz (measured on mast02, 2026-08-28), so a dwell resolves the disturbance
  *spectrum* to ~22 Hz -- which is what separates a wind gust from a structural resonance
  the servo is exciting. A rolling sigma cannot answer that and cannot be un-computed
  later. ~200 MB/night/unit is the price.

Nothing here gates any other subsystem. Per the design's observer-first rule, the
campaign only measures; no threshold derived from it controls anything yet.
"""

import csv
import datetime
import json
import math
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import astropy.units as u
import httpx
from astropy.coordinates import AltAz, EarthLocation, SkyCoord
from astropy.time import Time

from common.activities import UnitActivities
from common.canonical import CanonicalResponse, CanonicalResponse_Ok
from common.config import Config
from common.filer import Filer, MoveGuardian
from common.mast_logging import get_logger, observing_night
from common.paths import PathMaker

if TYPE_CHECKING:
    from unit import Unit

logger = get_logger(__name__)
filer = Filer(logger)


class StabilityCampaignError(Exception):
    """The campaign could not be set up, or could not do something it needs to do."""


# --------------------------------------------------------------------------- mesh --

#: Bump on ANY change to the mesh below. It is stamped into every product, because
#: "same mesh for all runs" is a correctness requirement rather than a preference:
#: cells are pooled across nights and across the two units, and silently mixing two
#: mesh definitions is the same failure that MIN_CONFIDENCE and max_best_hfd_px
#: already produced twice in this repo -- a number calibrated on one population and
#: applied to another, with nothing in the tests to catch it.
MESH_VERSION = "v1"

#: alt 15 is the mount floor (Mount.MIN_ALTITUDE_DEGREES) and where the wind effect
#: should be largest; alt 65 is the control, and without the contrast there is no way
#: to show the effect is real.
MESH_ALTITUDES_DEGS: tuple[float, ...] = (15.0, 30.0, 45.0, 65.0)
MESH_AZIMUTH_STEP_DEGS: float = 36.0  # 10 azimuths

#: Half a step, so that NO cell points due north.
#:
#: A cell's declination has no time term -- sin(dec) = sin(alt)sin(lat) + cos(alt)cos(lat)
#: cos(az) -- so each cell guides at the same declination on every visit, forever. Along
#: the due-north column that reduces to dec = 90 - |alt - lat|, and at this site
#: (lat 30.053) the mesh's alt 30 row lands 0.05 degrees from the celestial pole. PHD2
#: scales the RA guide rate by 1/cos(dec), which is ~1081x there: the cell can never be
#: guided, by geometry rather than by calibration, and alt 15/45 at az 0 are marginal at
#: dec ~75. Unguided dwells are unaffected (the cell is mechanically ordinary), but the
#: guided-minus-unguided difference -- the headline result -- would be structurally
#: missing across the whole northern column, and missing non-randomly.
#:
#: The offset costs nothing: the azimuth origin is arbitrary when the covariate is
#: |az - wind bearing|, ten azimuths still span the circle, and the worst cell moves from
#: dec 89.9 to 74.4. It has to be decided before the first night, since the mesh is frozen
#: once cells start pooling across nights.
MESH_AZIMUTH_OFFSET_DEGS: float = 18.0

#: The pilot is a STRICT SUBSET of the full mesh, so its nights contribute to the final
#: dataset instead of being discarded. The design's original pilot (alt 20/40/65, az
#: every 45 degrees) shared exactly one altitude and two azimuths with the full mesh,
#: which would have thrown away all three pilot nights.
PILOT_ALTITUDES_DEGS: tuple[float, ...] = (15.0, 65.0)
PILOT_AZIMUTH_STEP_DEGS: float = 72.0  # 5 azimuths -> 10 cells

#: Coprime with both mesh sizes (40 and 10), so the rotation visits every cell before
#: repeating and each cell lands on a different hour on successive passes.
TRAVERSAL_STRIDE = 7

# ------------------------------------------------------------------------- timing --

DWELL_UNGUIDED_SECONDS = 60.0
DWELL_GUIDED_SECONDS = 60.0

#: Outer bound on waiting for PHD2 to settle onto a star. PHD2 enforces its own settle
#: timeout and reports failure as a non-zero SettleDone status, which arrives well inside
#: this; this only catches a SettleDone that never comes. The wait is additionally capped
#: by whatever is left in the slot, so a slow settle costs the guided dwell rather than
#: pushing the unit out of step with its partner.
SETTLE_TIMEOUT_SECONDS = 45.0

#: One cell visit, slew included. Both units step on this grid, so it must be generous
#: enough that a slow slew does not push a unit into the next slot.
SLOT_SECONDS = 210.0

#: Just above PWI4's measured ~44 Hz refresh. Polling faster returns the same drive
#: sample twice, which would deflate any sigma computed downstream.
SAMPLE_HZ = 45.0

#: A campaign nobody stops must not run into the day. Belt-and-braces against a missing
#: stop call; the operator or the safety system is the primary stop.
MAX_CAMPAIGN_HOURS = 14.0

#: Refuse a cell whose slew would take axis0 within this of its wrap limit. Approximate
#: on purpose -- the exact mech position of a cell is only known after the transform --
#: but it keeps the mesh off the limit we found mast02 parked against on 2026-08-28.
WRAP_MARGIN_DEGS = 10.0


@dataclass(frozen=True)
class Cell:
    index: int
    alt_deg: float
    az_deg: float

    @property
    def label(self) -> str:
        return f"cell={self.index:02d},alt={self.alt_deg:g},az={self.az_deg:g}"


@dataclass(frozen=True)
class Mesh:
    version: str
    cells: tuple[Cell, ...]
    stride: int = TRAVERSAL_STRIDE

    @staticmethod
    def build(altitudes: tuple[float, ...], azimuth_step: float, version: str = MESH_VERSION) -> "Mesh":
        # The same offset for the pilot as for the full mesh, which is what keeps the
        # pilot's azimuths a subset of the full mesh's rather than interleaved with them.
        azimuths = [MESH_AZIMUTH_OFFSET_DEGS + i * azimuth_step for i in range(round(360.0 / azimuth_step))]
        cells = tuple(
            Cell(index=i, alt_deg=alt, az_deg=az)
            for i, (alt, az) in enumerate((alt, az) for alt in altitudes for az in azimuths)
        )
        if math.gcd(TRAVERSAL_STRIDE, len(cells)) != 1:
            raise StabilityCampaignError(
                f"traversal stride {TRAVERSAL_STRIDE} is not coprime with {len(cells)} cells, so the "
                f"rotation would visit only {len(cells) // math.gcd(TRAVERSAL_STRIDE, len(cells))} of them"
            )
        return Mesh(version=version, cells=cells)

    def cell_for_visit(self, visit: int) -> Cell:
        """The cell a given slot belongs to.

        Deterministic in the visit number alone, which is what keeps two units in
        lockstep and makes resume a computation rather than a stored pointer.
        """
        return self.cells[(visit * self.stride) % len(self.cells)]


FULL_MESH = Mesh.build(MESH_ALTITUDES_DEGS, MESH_AZIMUTH_STEP_DEGS)
PILOT_MESH = Mesh.build(PILOT_ALTITUDES_DEGS, PILOT_AZIMUTH_STEP_DEGS, version=f"{MESH_VERSION}-pilot")


# -------------------------------------------------------------------- descriptor --


@dataclass
class CampaignDescriptor:
    """What the two units must agree on to stay in lockstep.

    Lives ABOVE the per-machine product roots, at
    `<share>/MAST/Stability/<observing-night>/campaign.json`, because it is shared by
    every unit in the campaign -- unlike the products, which are per-machine. The first
    unit to start writes it; the second adopts it rather than minting its own epoch.
    """

    epoch_utc: str
    mesh_version: str
    slot_seconds: float
    stride: int
    created_by: str
    #: False when the share was unreachable at start and this unit had to invent its own
    #: epoch. Recorded rather than hidden: a run with this set cannot be paired with the
    #: other unit's, and the analysis has to know that instead of guessing.
    standalone: bool = False

    @property
    def epoch(self) -> datetime.datetime:
        return datetime.datetime.fromisoformat(self.epoch_utc)

    def visit_now(self, when: datetime.datetime | None = None) -> int:
        when = when or datetime.datetime.now(datetime.UTC)
        return int((when - self.epoch).total_seconds() // self.slot_seconds)

    def slot_start(self, visit: int) -> datetime.datetime:
        return self.epoch + datetime.timedelta(seconds=visit * self.slot_seconds)


def _descriptor_path() -> Path | None:
    """`<share>/MAST/Stability/<night>/campaign.json`, or None if the share is down."""
    share = Filer().share_root
    if share is None:
        return None
    night = observing_night(datetime.datetime.now(datetime.UTC))
    return Path(share.root) / "Stability" / night / "campaign.json"


def load_or_create_descriptor(mesh: Mesh, hostname: str) -> CampaignDescriptor:
    """Adopt tonight's campaign if one is already running, else start it.

    This is what makes the second unit fall into step with the first: whichever unit
    starts second reads the epoch the first one wrote.
    """
    path = _descriptor_path()
    now = datetime.datetime.now(datetime.UTC)

    if path is not None:
        try:
            if path.exists():
                descriptor = CampaignDescriptor(**json.loads(path.read_text()))
                if descriptor.mesh_version != mesh.version:
                    raise StabilityCampaignError(
                        f"tonight's campaign runs mesh '{descriptor.mesh_version}' but this unit was asked "
                        f"for '{mesh.version}'. Pooling two meshes silently is exactly what MESH_VERSION "
                        f"exists to prevent -- stop the other unit or use the same mesh."
                    )
                logger.info(f"joined the campaign started by {descriptor.created_by} at {descriptor.epoch_utc}")
                return descriptor

            descriptor = CampaignDescriptor(
                epoch_utc=now.isoformat(),
                mesh_version=mesh.version,
                slot_seconds=SLOT_SECONDS,
                stride=mesh.stride,
                created_by=hostname,
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            with MoveGuardian().protect(str(path)):
                path.write_text(json.dumps(asdict(descriptor), indent=2))
            logger.info(f"started a new campaign, epoch {descriptor.epoch_utc}")
            return descriptor
        except OSError as ex:
            logger.error(f"could not reach the campaign descriptor at '{path}': {ex}")

    logger.warning("running STANDALONE: no shared descriptor, so this unit is not in lockstep with any other")
    return CampaignDescriptor(
        epoch_utc=now.isoformat(),
        mesh_version=mesh.version,
        slot_seconds=SLOT_SECONDS,
        stride=mesh.stride,
        created_by=hostname,
        standalone=True,
    )


# ---------------------------------------------------------------------- sampler --

#: Everything the analysis needs per drive sample. Both axes' current AND servo error:
#: current measures the disturbance (wind is a torque disturbance, and a stiff loop
#: hides it in the error signal), servo error says whether it is costing data.
#: setpoint_velocity and is_slewing are here for the motion exclusion -- without it every
#: cell would read "unstable" immediately after its own goto.
SAMPLE_FIELDS = (
    "mount.axis0.position_timestamp",
    "mount.axis0.position_degs",
    "mount.axis0.servo_error_arcsec",
    "mount.axis0.measured_current_amps",
    "mount.axis0.measured_velocity_degs_per_sec",
    "mount.axis0.setpoint_velocity_degs_per_sec",
    "mount.axis1.position_degs",
    "mount.axis1.servo_error_arcsec",
    "mount.axis1.measured_current_amps",
    "mount.axis1.measured_velocity_degs_per_sec",
    "mount.axis1.setpoint_velocity_degs_per_sec",
    "mount.altitude_degs",
    "mount.azimuth_degs",
    "mount.is_slewing",
    "mount.is_tracking",
)

STAMP_FIELD = "mount.axis0.position_timestamp"


class TelemetrySampler:
    """Polls PWI4 /status at SAMPLE_HZ and writes one row per DISTINCT drive sample.

    The status source is injected rather than reached for, so the sampler can be driven
    from a recorded trace in tests -- including replaying a windy night. That is the
    pattern `tests/test_spiral_search.py` already uses.

    It does NOT go through the vendored `pwi4_client`, which opens a fresh TCP
    connection per request via `urllib.urlopen`. Invisible at 2 s, most of the cost at
    45 Hz.
    """

    def __init__(self, status_text=None, url: str = "http://127.0.0.1:8220/status"):
        self._url = url
        self._client: httpx.Client | None = None
        if status_text is None:
            # trust_env=False is load-bearing: urllib's getproxies() reads the WinINET
            # registry settings on these machines, so httpx routes even 127.0.0.1
            # through the site proxy, which answers 403 with an HTML page. That is what
            # made mast02 look like it had no PWI4 running at all. common/api.py
            # disables it for the same reason.
            self._client = httpx.Client(timeout=5.0, trust_env=False)
            status_text = self._fetch
        self._status_text = status_text

    def _fetch(self) -> str:
        assert self._client is not None
        return self._client.get(self._url).text

    @staticmethod
    def _parse(text: str) -> dict[str, str]:
        wanted = set(SAMPLE_FIELDS)
        out = {}
        for line in text.splitlines():
            key, _, value = line.partition("=")
            if key in wanted:
                out[key] = value
        return out

    def record(self, path: str, seconds: float, stop: threading.Event) -> dict[str, Any]:
        """Sample for `seconds`, writing a CSV of distinct drive samples to `path`.

        Returns a small summary for the cell metadata -- not a substitute for the raw
        rows, which are the point.
        """
        period = 1.0 / SAMPLE_HZ
        seen: set[str] = set()
        polls = 0
        rows = 0

        with MoveGuardian().protect(path), open(path, "w", newline="") as fp:
            writer = csv.writer(fp)
            writer.writerow(("host_utc", *SAMPLE_FIELDS))

            t0 = time.monotonic()
            next_t = t0
            while time.monotonic() - t0 < seconds and not stop.is_set():
                try:
                    sample = self._parse(self._status_text())
                    polls += 1
                except Exception as ex:  # noqa: BLE001 -- a dropped poll is data, not a crash
                    logger.error(f"status poll failed: {ex}")
                    sample = {}

                stamp = sample.get(STAMP_FIELD)
                if stamp is not None and stamp not in seen:
                    seen.add(stamp)
                    # host_utc is recorded alongside PWI4's own stamp so the offline join
                    # against the Davis anemometer (60 s cadence, on another host) can be
                    # checked rather than assumed. A few minutes of NTP drift would
                    # misattribute wind to the wrong cell and nothing downstream would
                    # ever reveal it.
                    writer.writerow(
                        (
                            datetime.datetime.now(datetime.UTC).isoformat(),
                            *(sample.get(f, "") for f in SAMPLE_FIELDS),
                        )
                    )
                    rows += 1

                next_t += period
                sleep = next_t - time.monotonic()
                if sleep > 0:
                    time.sleep(sleep)

        filer.move_ram_to_shared(path)
        return {"polls": polls, "distinct_samples": rows, "seconds": seconds}

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


# --------------------------------------------------------------------- campaign --


@dataclass
class CampaignState:
    """What `stability_campaign_status` reports. A campaign runs unattended for hours;
    start/stop without this leaves the operator blind."""

    active: bool = False
    run_folder: str | None = None
    mesh_version: str = ""
    epoch_utc: str | None = None
    standalone: bool = False
    visit: int | None = None
    cell: dict | None = None
    phase: str = "idle"
    visits_attempted: int = 0
    visits_completed: int = 0
    cells_skipped: int = 0
    guide_failures: int = 0
    last_error: str | None = None
    started_at: str | None = None
    coverage: dict[int, int] = field(default_factory=dict)


class StabilityCampaign:
    """One campaign at a time, owned by the Unit."""

    def __init__(self, unit: "Unit", sampler: TelemetrySampler | None = None):
        self.unit = unit
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sampler = sampler
        self.state = CampaignState()
        self.mesh: Mesh = FULL_MESH
        self.descriptor: CampaignDescriptor | None = None

    # ------------------------------------------------------------------ control --

    @property
    def is_active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, pilot: bool = False) -> CanonicalResponse:
        with self._lock:
            if self.is_active:
                # Idempotent rather than an error: a double start from an operator or a
                # retrying client must not spawn a second mesh walker on one mount.
                return asdict(self.state)

            if self._unit_is_busy():
                return CanonicalResponse(
                    errors=[f"unit is busy ({self.unit.activities_verbal}); refusing to start the campaign"]
                )

            self.mesh = PILOT_MESH if pilot else FULL_MESH
            try:
                self.descriptor = load_or_create_descriptor(self.mesh, self.unit.hostname)
            except (StabilityCampaignError, OSError) as ex:
                return CanonicalResponse(errors=[str(ex)])

            run_folder = PathMaker().make_stability_folder()
            self._write_run_metadata(run_folder)

            self._stop.clear()
            self.state = CampaignState(
                active=True,
                run_folder=run_folder,
                mesh_version=self.mesh.version,
                epoch_utc=self.descriptor.epoch_utc,
                standalone=self.descriptor.standalone,
                started_at=datetime.datetime.now(datetime.UTC).isoformat(),
            )
            self.unit.start_activity(UnitActivities.StabilityCampaigning)

            self._thread = threading.Thread(target=self.do_stability_campaign, name="stability-campaign", daemon=True)
            self._thread.start()
            logger.info(f"campaign started, mesh '{self.mesh.version}', products under '{run_folder}'")
            return CanonicalResponse(value=asdict(self.state))

    def stop(self) -> CanonicalResponse:
        """Ask the walker to stop; it finishes the sample it is inside and returns.

        A cell is only counted once both halves are written, so stopping mid-dwell
        discards that visit rather than recording a short one -- a half-length dwell
        would otherwise enter the pool with a quietly different sigma.
        """
        with self._lock:
            if not self.is_active:
                return CanonicalResponse_Ok
            logger.info("campaign stop requested")
            self._stop.set()
        return CanonicalResponse_Ok

    def status(self) -> dict:
        state = asdict(self.state)
        state["active"] = self.is_active
        if self.descriptor is not None and self.is_active:
            state["visit"] = self.descriptor.visit_now()
        return state

    def _unit_is_busy(self) -> bool:
        """The mesh owns the mount for the night; an acquisition arriving at 01:00 must
        not fight it for the same axes."""
        busy = (
            UnitActivities.Acquiring
            | UnitActivities.Guiding
            | UnitActivities.Autofocusing
            | UnitActivities.Solving
            | UnitActivities.StartingUp
            | UnitActivities.ShuttingDown
        )
        return bool(self.unit.activities & busy)

    # ------------------------------------------------------------------- walker --

    def do_stability_campaign(self) -> None:
        assert self.descriptor is not None
        deadline = time.monotonic() + MAX_CAMPAIGN_HOURS * 3600
        last_visit: int | None = None

        try:
            while not self._stop.is_set() and time.monotonic() < deadline:
                visit = self.descriptor.visit_now()
                if visit == last_visit:
                    # Inside a slot we have already served: wait for the next one rather
                    # than re-measuring the same cell. This is also what re-synchronises a
                    # unit that fell behind -- it simply picks up the current slot.
                    time.sleep(1.0)
                    continue

                last_visit = visit
                cell = self.mesh.cell_for_visit(visit)
                self._visit(visit, cell)

            if time.monotonic() >= deadline:
                logger.error(f"campaign hit its {MAX_CAMPAIGN_HOURS}h ceiling with no stop; ending")
        except Exception as ex:  # the walker owns the mount; it must land it safely
            logger.exception("campaign walker died")
            self.state.last_error = str(ex)
        finally:
            self._shutdown()

    def _visit(self, visit: int, cell: Cell) -> None:
        """One cell: slew, dwell unguided, acquire a star, dwell guided."""
        self.state.visit = visit
        self.state.cell = asdict(cell)
        self.state.visits_attempted += 1

        folder = os.path.join(self.state.run_folder or "", f"visit={visit:05d},{cell.label}")
        os.makedirs(folder, exist_ok=True)

        meta: dict[str, Any] = {
            "visit": visit,
            "cell": asdict(cell),
            "mesh_version": self.mesh.version,
            "epoch_utc": self.state.epoch_utc,
            "standalone": self.state.standalone,
            "hostname": self.unit.hostname,
            "slot_start_utc": self.descriptor.slot_start(visit).isoformat() if self.descriptor else None,
        }

        try:
            if not self._slew_to(cell, meta):
                self.state.cells_skipped += 1
                meta["outcome"] = "skipped"
                self._write_meta(folder, meta)
                return

            self.state.phase = "unguided"
            meta["unguided"] = self._sample(os.path.join(folder, "unguided.csv"), DWELL_UNGUIDED_SECONDS)

            self.state.phase = "acquire"
            settle = self._acquire_guide_star(visit)
            meta["settle"] = settle
            guiding = bool(settle.get("settled"))
            meta["guide_star_acquired"] = guiding
            # Captured per visit, not only at start: the failure this guards against is a
            # calibration that CHANGES mid-campaign, which a single start-of-run snapshot
            # cannot show. Taken after the guide attempt, so it reflects any recalibration
            # PHD2 decided to run on its way into this cell.
            meta["guider_calibration"] = self._calibration_state()
            if guiding:
                self.state.phase = "guided"
                meta["guided"] = self._sample(os.path.join(folder, "guided.csv"), DWELL_GUIDED_SECONDS)
            else:
                # Never abort the dwell over a missing star. Low-altitude cells fail
                # guiding most often and they are the cells that matter most -- a design
                # that drops them keeps only the easy half of the mesh.
                self.state.guide_failures += 1

            meta["outcome"] = "completed"
            self.state.visits_completed += 1
            self.state.coverage[cell.index] = self.state.coverage.get(cell.index, 0) + 1
        except Exception as ex:  # one bad cell must not end the night
            logger.exception(f"visit {visit} at {cell.label} failed")
            self.state.last_error = str(ex)
            meta["outcome"] = "failed"
            meta["error"] = str(ex)
        finally:
            self.state.phase = "idle"
            self._stop_guiding()
            self._write_meta(folder, meta)

    # -------------------------------------------------------------------- steps --

    def _slew_to(self, cell: Cell, meta: dict) -> bool:
        """Point at the cell, in RA/Dec.

        The mesh lives in alt/az -- wind arrives on a bearing and gravity torque follows
        altitude -- but `goto_alt_az` STOPS TRACKING, and a stationary mount is not the
        servo state being characterised. So the cell is converted at visit time and
        commanded as RA/Dec, and the analysis bins on the alt/az READ BACK from /status,
        which makes the drift over a dwell irrelevant.
        """
        mount = self.unit.mount
        if mount is None:
            raise StabilityCampaignError("no mount")

        if not self._wrap_is_safe(cell, meta):
            logger.warning(f"{cell.label}: skipped, axis0 too close to its wrap limit")
            return False

        ra_hours, dec_degs = self._altaz_to_radec(cell)
        meta["commanded"] = {"ra_hours": ra_hours, "dec_degs": dec_degs}

        from mount import SettleMode  # local import: mount imports this module's Unit type

        mount.goto_ra_dec_j2000(ra_hours, dec_degs)
        mount.wait_until_settled(SettleMode.SLEW)

        # Read back from PWI4, not from MountStatus, which carries ra/dec only and would
        # hand back None for both. The analysis bins on where the mount ACTUALLY ended
        # up: an alt/az cell drifts while it is commanded as RA/Dec, and up to ~1.4 deg
        # of azimuth over a dwell near the zenith. Every telemetry row carries the same
        # two fields, so this is the cell-level convenience, not the only copy.
        meta["read_back"] = self._read_back_alt_az()
        return True

    def _read_back_alt_az(self) -> dict:
        try:
            st = self.unit.pw.status()  # type: ignore[union-attr]
            return {"altitude_degs": st.mount.altitude_degs, "azimuth_degs": st.mount.azimuth_degs}
        except Exception as ex:  # noqa: BLE001
            logger.error(f"could not read back alt/az: {ex}")
            return {"altitude_degs": None, "azimuth_degs": None}

    def _altaz_to_radec(self, cell: Cell) -> tuple[float, float]:
        site = Config().local_site
        location = site.location
        if location.latitude is None or location.longitude is None:
            raise StabilityCampaignError("site has no coordinates; cannot convert the mesh to RA/Dec")

        earth = EarthLocation(
            lat=location.latitude * u.deg,  # type: ignore
            lon=location.longitude * u.deg,  # type: ignore
            height=(location.elevation or 0) * u.m,  # type: ignore
        )
        altaz = AltAz(
            alt=cell.alt_deg * u.deg,  # type: ignore
            az=cell.az_deg * u.deg,  # type: ignore
            obstime=Time(datetime.datetime.now(datetime.UTC)),
            location=earth,
        )
        radec = SkyCoord(altaz).transform_to("icrs")
        return float(radec.ra.hour), float(radec.dec.deg)  # type: ignore

    def _wrap_is_safe(self, cell: Cell, meta: dict) -> bool:
        """Refuse a cell when axis0 is already against its wrap limit.

        A mesh that sweeps azimuth all night WILL reach the limit -- mast02 was found
        parked on `max_mech_position_degs` with a target 378 degrees away on 2026-08-28.
        Approximate by construction: the cell's exact mech position is only known after
        the transform, so this is a guard against walking into the stop, not a predictor.
        """
        try:
            st = self.unit.pw.status()  # type: ignore[union-attr]
            pos = st.mount.axis0.position_degs
            lo = st.mount.axis0.min_mech_position_degs
            hi = st.mount.axis0.max_mech_position_degs
        except Exception as ex:  # noqa: BLE001
            logger.error(f"could not read axis0 wrap limits: {ex}")
            return True  # do not let a telemetry hiccup stall the mesh

        meta["axis0"] = {"position_degs": pos, "min_mech": lo, "max_mech": hi}
        if pos is None or lo is None or hi is None:
            return True
        return (pos - lo) > WRAP_MARGIN_DEGS and (hi - pos) > WRAP_MARGIN_DEGS

    def _sample(self, path: str, seconds: float) -> dict:
        if self._sampler is None:
            self._sampler = TelemetrySampler()
        return self._sampler.record(path, seconds, self._stop)

    def _seconds_left_in_slot(self, visit: int) -> float:
        if self.descriptor is None:
            return SETTLE_TIMEOUT_SECONDS
        elapsed = (datetime.datetime.now(datetime.UTC) - self.descriptor.slot_start(visit)).total_seconds()
        return self.descriptor.slot_seconds - elapsed

    def _acquire_guide_star(self, visit: int) -> dict:
        """Start PHD2 and wait for it to actually settle onto a star.

        The settle result is returned rather than a bool: how long it took, how close it
        got, and PHD2's own status if it gave up. Time-to-settle is a covariate worth
        keeping -- it measures how hard the star was to hold at this pointing, which is
        adjacent to the thing the campaign is trying to measure.

        The wait is bounded by what is left in the slot, minus the guided dwell it is
        waiting to make possible. A settle that cannot fit therefore forfeits the guided
        half of THIS cell instead of running past the slot boundary and costing the pair
        its next cell as well.

        PHD2 self-labels the session it starts here: every `Guiding Begins at` block
        records Alt, Az, RA, Dec, hour angle, HFD and the lock position, so a cell's guide
        data ties itself to its pointing with no cross-correlation and no risk of
        mis-joining. SNR, StarMass and HFD come from the same rows and are the nuisance
        variables that would otherwise make a fainter-star cell look like a worse-guiding
        one.
        """
        guider = getattr(self.unit, "guider", None)
        if guider is None:
            return {"settled": False, "reason": "no guider"}

        budget = min(SETTLE_TIMEOUT_SECONDS, self._seconds_left_in_slot(visit) - DWELL_GUIDED_SECONDS)
        if budget <= 0:
            return {"settled": False, "reason": "no room left in the slot for a guided dwell"}

        try:
            guider.start_guiding()
        except Exception as ex:  # noqa: BLE001
            logger.error(f"start_guiding failed: {ex}")
            return {"settled": False, "reason": f"start_guiding failed: {ex}"}

        settle = guider.wait_for_settle(timeout=budget, stop=self._stop)
        # PHD2 can report a clean settle while the unit-level activity has not been set,
        # and the campaign only wants to record a guided dwell when BOTH agree -- the
        # telemetry rows are labelled by it.
        settle["unit_guiding"] = bool(self.unit.is_active(UnitActivities.Guiding))
        if not settle.get("settled"):
            logger.warning(f"visit {visit}: no guide star -- {settle}")
        return settle

    def _calibration_state(self) -> dict | None:
        guider = getattr(self.unit, "guider", None)
        if guider is None:
            return None
        try:
            return guider.calibration_state()
        except Exception as ex:  # noqa: BLE001
            logger.error(f"could not read the guider calibration: {ex}")
            return None

    def _stop_guiding(self) -> None:
        guider = getattr(self.unit, "guider", None)
        if guider is None:
            return
        try:
            guider.stop_guiding()
        except Exception as ex:  # noqa: BLE001
            logger.error(f"stop_guiding failed: {ex}")

    # ------------------------------------------------------------------ products --

    def _write_run_metadata(self, run_folder: str) -> None:
        """The run's own provenance, written once at start.

        The mesh is written out in full rather than by name: a reader a year from now
        should not have to find the matching source revision to know which 40 pointings
        these products came from.
        """
        assert self.descriptor is not None
        path = os.path.join(run_folder, "campaign.json")
        payload = {
            "hostname": self.unit.hostname,
            "descriptor": asdict(self.descriptor),
            "mesh": {
                "version": self.mesh.version,
                "stride": self.mesh.stride,
                "cells": [asdict(c) for c in self.mesh.cells],
            },
            "dwell": {
                "unguided_seconds": DWELL_UNGUIDED_SECONDS,
                "settle_timeout_seconds": SETTLE_TIMEOUT_SECONDS,
                "guided_seconds": DWELL_GUIDED_SECONDS,
                "slot_seconds": SLOT_SECONDS,
                "sample_hz": SAMPLE_HZ,
            },
            "started_at_utc": datetime.datetime.now(datetime.UTC).isoformat(),
            # The calibration this run STARTED with, in full. Each visit records it again
            # in compact form, so a recalibration part-way through the night shows up as a
            # change between visits rather than being invisible.
            "guider_calibration_at_start": self._calibration_state(),
        }
        with MoveGuardian().protect(path), open(path, "w") as fp:
            json.dump(payload, fp, indent=2, default=str)
        filer.move_ram_to_shared(path)

    def _write_meta(self, folder: str, meta: dict) -> None:
        path = os.path.join(folder, "meta.json")
        with MoveGuardian().protect(path), open(path, "w") as fp:
            json.dump(meta, fp, indent=2, default=str)
        filer.move_ram_to_shared(path)

    # ------------------------------------------------------------------ teardown --

    def _shutdown(self) -> None:
        self.state.active = False
        self.state.phase = "idle"
        self._stop_guiding()
        if self._sampler is not None:
            self._sampler.close()
            self._sampler = None
        self.unit.end_activity(UnitActivities.StabilityCampaigning)

        # Park rather than leave an OTA at alt 15 broadside to the wind with nobody
        # driving it. The campaign is the one activity that deliberately points low into
        # weather, so the ending matters more here than elsewhere.
        try:
            if self.unit.mount is not None:
                self.unit.mount.park()
        except Exception as ex:  # noqa: BLE001
            logger.error(f"could not park at end of campaign: {ex}")
        logger.info(
            f"campaign ended: {self.state.visits_completed} completed, "
            f"{self.state.cells_skipped} skipped, {self.state.guide_failures} without a guide star"
        )


def coverage_from_products(run_root: str) -> dict[int, int]:
    """Visits per cell, recovered by listing what was actually written.

    The coverage table is DERIVED, never stored: a separate counter file is one more
    thing that can disagree with the products, and the products are the evidence. It
    also answers the question the walker actually needs -- which cells are
    under-sampled -- rather than "where did we stop".
    """
    counts: dict[int, int] = {}
    root = Path(run_root)
    if not root.exists():
        return counts
    for folder in root.glob("visit=*"):
        meta = folder / "meta.json"
        if not meta.exists():
            continue
        try:
            payload = json.loads(meta.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if payload.get("outcome") != "completed":
            continue
        index = (payload.get("cell") or {}).get("index")
        if index is not None:
            counts[index] = counts.get(index, 0) + 1
    return counts
