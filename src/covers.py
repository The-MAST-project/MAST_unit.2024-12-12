import logging
import time
from typing import TYPE_CHECKING

from fastapi.routing import APIRouter

from common.activities import CoverActivities
from common.canonical import CanonicalResponse, CanonicalResponse_Ok
from common.const import Const
from common.dlipowerswitch import OutletDomain, SwitchedOutlet
from common.endpoints import (
    Completion,
    Stability,
    Tier,
    add_api_route,
    endpoint,
    register_component_endpoints,
)
from common.interfaces.components import Component
from common.models.statuses import CoversState, CoverStatus
from common.utils import RepeatTimer, time_stamp
from PlaneWave import pwi4_client

if TYPE_CHECKING:
    from unit import Unit

logger: logging.Logger = logging.getLogger("mast.unit." + __name__)

#: The oldest PWI4 exposing `/mirrorcover/*`. Below this the endpoints 404 and the status
#: has no `mirrorcover` section, so the covers must report unavailable rather than guess.
MINIMUM_PWI4_VERSION = (4, 1, 6)

#: How long a full open or close takes, measured over three cycles on mast00 (MAST_unit#134):
#: `Closing(3) @0.4s -> PartlyOpen(5) @24.7s -> Closed(1) @25.5s`. The timeout is derived
#: from that rather than guessed, with room for a slower unit.
MOVE_TIMEOUT_SECONDS = 60

#: PWI4's `mirrorcover.overall_state_name` -> our `CoversState`.
#:
#: Keyed on the NAME, never the integer. The two enumerations overlap numerically and
#: disagree: PWI4's 0 is `Open` where `CoversState(0)` is `NotPresent`, and PWI4's 3 is
#: `Closing` where `CoversState(3)` is `Open`. Only 1 (`Closed`) coincides. A `CoversState(int)`
#: cast on a PWI4 value therefore returns a wrong answer rather than raising -- and reports a
#: closing cover as open, which is the reading that matters most.
#:
#: `PartlyOpen` is a normal transitional state in BOTH directions, not a fault; it maps to
#: `Moving` alongside `Opening`/`Closing`.
_PWI4_STATE_NAMES: dict[str, CoversState] = {
    "Open": CoversState.Open,
    "Closed": CoversState.Closed,
    "Opening": CoversState.Moving,
    "Closing": CoversState.Moving,
    "PartlyOpen": CoversState.Moving,
}


class Covers(Component, SwitchedOutlet):
    """
    Drives the **MAST** mirror covers through PWI4's `mirrorcover` HTTP API.

    Replaces the ASCOM `CoverCalibrator` driver, whose ProgID is unregistered on at least one
    unit so the component failed at construction (#95). PWI4 is already installed, running and
    connected to the hardware, and its state machine is sensor-backed rather than a command
    echo. See MAST_unit#134 for the on-hardware verification and `retire-pwshutter-and-ascom-covers.md`
    for the plan.

    The `mirrorcover` section is not exposed by the vendored `pwi4_client` -- that client
    predates the feature ("Added in 4.0.99 Beta 2" is its newest annotation; PWI4 here is
    4.1.6). Rather than patch vendored PlaneWave code, the four commands and the status read
    are wrapped below on top of `PWI4.request()` and `PWI4Status.raw`, which already carries
    every key PWI4 returns.
    """

    _instance = None
    _initialized = False

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, unit: "Unit"):  # type: ignore[name]
        if self._initialized:
            return

        self.unit = unit
        assert self.unit is not None and self.unit.unit_conf is not None
        self.conf = self.unit.unit_conf.covers

        # A private client, as Mount and Focuser hold. Not a style choice: Unit builds Covers
        # before it assigns `self.pw`, so `unit.pw` does not exist yet at this point. It costs
        # nothing -- PWI4 holds a host, a port and a timeout, opening a connection per request.
        self.pw: pwi4_client.PWI4 = pwi4_client.PWI4()

        SwitchedOutlet.__init__(self, OutletDomain.UnitOutlets, outlet_name="Covers")
        Component.__init__(self, CoverActivities)
        self.activities = CoverActivities(0)

        if not self.is_on():
            self.power_on()

        self.timer: RepeatTimer = RepeatTimer(2, self.ontimer)
        self.timer.name = "covers-timer-thread"
        self.timer.start()

        self._was_shut_down = False

        self._initialized = True
        logger.info("initialized")

    # ----------------------------------------------------------------- PWI4 wrappers

    def _mirrorcover_status(self) -> dict[str, str] | None:
        """The `mirrorcover.*` keys from PWI4's status, or None if PWI4 cannot be reached.

        Returns the raw strings; interpretation is the callers' job. None means "PWI4 did not
        answer", which is different from "PWI4 answered and the covers are absent" -- the
        distinction the `state` property depends on.
        """
        try:
            raw = self.pw.status().raw
        except Exception as e:  # noqa: BLE001 -- PWException, urllib errors, socket timeouts
            logger.debug(f"PWI4 status unavailable: {e}")
            return None
        return {k: v for k, v in raw.items() if k.startswith("mirrorcover.")}

    def _mirrorcover_command(self, verb: str) -> CanonicalResponse:
        """Issue `/mirrorcover/<verb>`, returning the failure rather than raising.

        These endpoints exist only from PWI4 4.1.6; an older PWI4 answers 404, which surfaces
        here as a failed response rather than an exception on the path that closes the mirror.
        """
        try:
            self.pw.request(f"/mirrorcover/{verb}")
        except Exception as e:  # noqa: BLE001 -- as above; a cover command must not raise
            msg = f"covers: PWI4 '/mirrorcover/{verb}' failed ({e})"
            logger.error(msg)
            return CanonicalResponse(errors=[msg])
        return CanonicalResponse_Ok

    @property
    def pwi4_is_viable(self) -> bool:
        """Whether PWI4 answered, and is new enough to have `/mirrorcover/*`.

        NOT `self.pw is not None`: `PWI4()` never contacts the server -- it stores a host and a
        port -- so constructing one succeeds on a machine with no PWI4 installed at all. Only a
        `status()` that answered means anything.
        """
        try:
            raw = self.pw.status().raw
        except Exception:  # noqa: BLE001 -- unreachable PWI4 is a normal, reportable state
            return False

        # Read the version out of `raw` rather than `status.pwi4.version_field`. `Section` is
        # an empty class that PWI4Status populates by assigning attributes to the instance, so
        # a type checker sees no `version_field` on it at all. `raw` is a plain dict and is
        # typed. Its values are strings, hence the int().
        try:
            version = tuple(int(raw[f"pwi4.version_field[{i}]"]) for i in range(3))
        except (KeyError, ValueError):
            return False
        if version < MINIMUM_PWI4_VERSION:
            return False

        # The version is a proxy; this is the capability itself.
        return "mirrorcover.overall_state_name" in raw

    # ----------------------------------------------------------------- connection

    @endpoint(tier=Tier.OPERATION, stability=Stability.DEPRECATED, completion=Completion.IMMEDIATE)
    def connect(self):
        """
        Connects to the **MAST** mirror covers, through PWI4.

        Explicit, not implicit in open/close: PWI4 reports the last known `overall_state`
        verbatim while disconnected -- observed saying `Closed` with `is_connected=false`, and
        `Open` for covers that were physically shut.
        """
        return self._mirrorcover_command("connect")

    @endpoint(tier=Tier.OPERATION, stability=Stability.DEPRECATED, completion=Completion.IMMEDIATE)
    def disconnect(self):
        """
        Disconnects from the **MAST** mirror covers.
        """
        return self._mirrorcover_command("disconnect")

    @property
    def connected(self) -> bool:
        """PWI4's own `mirrorcover.is_connected`, not a local flag.

        Reading it from PWI4 rather than caching a boolean removes the bug in the ASCOM
        version, where the setter's `finally: self._connected = False` overwrote the success
        case unconditionally, so `connected` was never true and `shutdown()` never closed the
        covers (#133).
        """
        mirrorcover = self._mirrorcover_status()
        if not mirrorcover:
            return False
        return mirrorcover.get("mirrorcover.is_connected", "false").lower() == "true"

    @property
    def state(self) -> CoversState:
        mirrorcover = self._mirrorcover_status()
        if mirrorcover is None:
            return CoversState.Unknown  # PWI4 unreachable -- not the same as "no covers"
        if mirrorcover.get("mirrorcover.is_connected", "false").lower() != "true":
            # The state field is stale while disconnected; do not report it as fact.
            return CoversState.NotPresent

        name = mirrorcover.get("mirrorcover.overall_state_name")
        if name not in _PWI4_STATE_NAMES:
            logger.error(f"covers: unmapped PWI4 state '{name}'")
            return CoversState.Error
        return _PWI4_STATE_NAMES[name]

    # ----------------------------------------------------------------- status

    @endpoint(tier=Tier.INTERFACE, completion=Completion.IMMEDIATE)
    def status(self) -> CoverStatus:
        state = self.state
        return CoverStatus(
            **self.power_status().model_dump(),
            **self.component_status().model_dump(),
            state=state,
            state_verbal=state.name,
            target_verbal=(
                "Open"
                if self.is_active(CoverActivities.Opening)
                else "Close"
                if self.is_active(CoverActivities.Closing)
                else None
            ),
            date=time_stamp(),
        )

    # ----------------------------------------------------------------- motion

    @endpoint(tier=Tier.OPERATION, completion=CoverActivities.Opening)
    def open(self):
        """
        Starts opening the **MAST** mirror covers
        """
        if not self.connected:
            return CanonicalResponse(errors=["covers: not connected"])

        self.start_activity(CoverActivities.Opening)
        response = self._mirrorcover_command("open")
        if response.failed:
            self.end_activity(CoverActivities.Opening)
        return response

    @endpoint(tier=Tier.OPERATION, completion=CoverActivities.Closing)
    def close(self):
        """
        Starts closing the **MAST** mirror covers
        """
        if not self.connected:
            return CanonicalResponse(errors=["covers: not connected"])

        logger.info("closing covers")
        self.start_activity(CoverActivities.Closing)
        response = self._mirrorcover_command("close")
        if response.failed:
            self.end_activity(CoverActivities.Closing)
        return response

    @endpoint(tier=Tier.INTERFACE, completion=CoverActivities.Aborting)
    def abort(self):
        """
        Halts cover motion.

        `/mirrorcover/stop` is the PWI4 equivalent of ASCOM's `HaltCover()`. It is not listed
        in MAST_unit#134 -- it was found by probing 4.1.6, which answers 200 with a status body
        for `/mirrorcover/stop` and 404 for `/mirrorcover/halt` and `/mirrorcover/abort`.
        """
        was_moving = any(
            self.is_active(activity)
            for activity in (
                CoverActivities.StartingUp,
                CoverActivities.ShuttingDown,
                CoverActivities.Closing,
                CoverActivities.Opening,
            )
        )

        response = self._mirrorcover_command("stop")
        for activity in (
            CoverActivities.StartingUp,
            CoverActivities.ShuttingDown,
            CoverActivities.Closing,
            CoverActivities.Opening,
        ):
            if self.is_active(activity):
                self.end_activity(activity)

        if was_moving:
            self.start_activity(CoverActivities.Aborting)
        return response

    # ----------------------------------------------------------------- lifecycle

    @endpoint(tier=Tier.INTERFACE, completion=CoverActivities.StartingUp)
    def startup(self):
        """
        Performs the ``startup`` routine for the **MAST** mirror covers
        """
        self._was_shut_down = False
        if not self.is_on():
            self.power_on()
        if not self.connected:
            self.connect()
        if self.connected and self.state != CoversState.Open:
            self.start_activity(CoverActivities.StartingUp)
            self.open()
        return CanonicalResponse_Ok

    @endpoint(tier=Tier.INTERFACE, completion=CoverActivities.ShuttingDown)
    def shutdown(self):
        """
        Performs the ``shutdown`` procedure for the **MAST** mirror covers
        """
        if not self.connected:
            self.power_off()
            return CanonicalResponse_Ok

        if self.state != CoversState.Closed:
            self.start_activity(CoverActivities.ShuttingDown)
            self.close()
        return CanonicalResponse_Ok

    @property
    def is_shutting_down(self) -> bool:
        return self.is_active(CoverActivities.ShuttingDown)

    def powerdown(self):
        if not self._was_shut_down:
            self.shutdown()
        deadline = time.time() + MOVE_TIMEOUT_SECONDS
        while self.is_shutting_down and time.time() < deadline:
            time.sleep(1)
        if self.is_shutting_down:
            logger.error(f"covers: still closing after {MOVE_TIMEOUT_SECONDS}s; powering off anyway")
        self.power_off()

    def ontimer(self):
        if self.unit.unit_shutdown_event.is_set():
            self.timer.cancel()
            return

        if not self.connected:
            return

        # Terminal states matched exactly. `PartlyOpen` CONTAINS `Open` as a substring, so any
        # `in` / `startswith` / unanchored check here reports completion ~0.8 s early, in both
        # directions, with the covers still moving (MAST_unit#134).
        state = self.state

        if self.is_active(CoverActivities.Opening) and state == CoversState.Open:
            self.end_activity(CoverActivities.Opening)
            if self.is_active(CoverActivities.StartingUp):
                self.end_activity(CoverActivities.StartingUp)

        if self.is_active(CoverActivities.Closing) and state == CoversState.Closed:
            self.end_activity(CoverActivities.Closing)
            if self.is_active(CoverActivities.ShuttingDown):
                self.end_activity(CoverActivities.ShuttingDown)
                self._was_shut_down = True
                self.power_off()

        self._end_abort_when_at_rest()

    def _end_abort_when_at_rest(self) -> None:
        """End `Aborting` once the covers leave `Moving`. `Error` and `Unknown` are at rest too."""
        if self.is_active(CoverActivities.Aborting) and self.state != CoversState.Moving:
            self.end_activity(CoverActivities.Aborting)

    # ----------------------------------------------------------------- component contract

    @property
    def name(self) -> str:
        return "covers"

    @property
    def operational(self) -> bool:
        return all(
            [
                self.is_on(),
                self.pwi4_is_viable,
                self.connected,
                self.state == CoversState.Open,
            ]
        )

    @property
    def why_not_operational(self) -> list[str]:
        ret = []
        if not self.is_on():
            ret.append(f"{self.name}: not powered")
        elif not self.pwi4_is_viable:
            ret.append(f"{self.name}: PWI4 not answering, or older than {'.'.join(map(str, MINIMUM_PWI4_VERSION))}")
        elif not self.connected:
            ret.append(f"{self.name}: (via PWI4) not connected")
        else:
            state = self.state
            if state != CoversState.Open:
                ret.append(f"{self.name}: not open (state='{state.name}')")
        return ret

    @property
    def detected(self) -> bool:
        return self.connected

    @property
    def was_shut_down(self) -> bool:
        return self._was_shut_down

    @property
    def api_router(self) -> APIRouter:
        """
        Returns the API router for the Covers component.
        :return: APIRouter instance with Covers endpoints
        """
        base_path = Const.BASE_UNIT_PATH + "/covers"

        router = APIRouter()
        register_component_endpoints(router, self, base_path)
        add_api_route(router, base_path + "/connect", endpoint=self.connect)
        add_api_route(router, base_path + "/disconnect", endpoint=self.disconnect)
        add_api_route(router, base_path + "/open", endpoint=self.open, methods=["PUT"])
        add_api_route(router, base_path + "/close", endpoint=self.close, methods=["PUT"])

        return router
