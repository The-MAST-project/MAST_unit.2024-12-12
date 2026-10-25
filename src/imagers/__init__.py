import threading
import time

import numpy as np
from fastapi import APIRouter

from common.activities import ImagerActivities
from common.canonical import CanonicalResponse, CanonicalResponse_Ok
from common.const import Const
from common.dlipowerswitch import OutletDomain, SwitchedOutlet
from common.endpoints import Completion, Tier, add_api_route, endpoint, register_component_endpoints
from common.interfaces.imager import ImagerExposureSeries, ImagerInterface, ImagerTypes
from common.mast_logging import get_logger
from common.models.statuses import COOLING_DOWN_REASON, ImagerSettings, ImagerStatus
from failure_streaks import FailureStreaks
from imagers.usb_link import log_usb_link

logger = get_logger(__name__)
__all__ = ["Imager"]

#: Within this many degrees above the camera's reported set point, it counts as cooled:
#: `ImagerActivities.CoolingDown` ends and the imager is operational again. Also the margin
#: for warning that PHD2's profile set point differs from `imager.cooler_set_point`.
COOLING_TOLERANCE_DEGREES: float = 1.0

#: How often the cool-down watcher reads the camera when `imager.temp_check_interval` is unset.
COOLING_POLL_SECONDS_DEFAULT: float = 10.0

#: Still cooling after this long, the watcher warns once: the set point may be out of the
#: cooler's reach at this ambient. It keeps waiting -- the imager stays non-operational until
#: it gets there, which is the point.
COOLING_SLOW_WARNING_SECONDS: float = 900.0


class Imager(ImagerInterface, SwitchedOutlet):
    """
    This is the base class for all imagers.
    It provides the common interface and some common functionality.
    """

    _instance = None
    _initialized = False
    #: The cool-down watcher's last (temperature, set point) reading, for why_not_operational.
    _cooling_reading: tuple[float, float] | None = None

    @staticmethod
    def valid_imager_types() -> list[str]:
        from common.config import Config

        valid_types = []

        unit_conf = Config().get_unit()
        assert unit_conf is not None

        for t in unit_conf.imager.valid_imager_types:
            valid_types.append("ascom" if t.startswith("ascom") else t)
        return valid_types

    @staticmethod
    def configured_imager():
        from common.config import Config

        unit_conf = Config().get_unit()
        assert unit_conf is not None

        return unit_conf.imager.imager_type

    @property
    def conf(self):
        """This component's configuration, live.

        Was snapshotted in ``__init__``, which is why a value edited in the database
        reached a running unit only at the next service restart. Within one configuration
        generation this returns the same object every time, so it is a memo lookup, not a
        rebuild -- which is what makes a property affordable here.
        """
        assert self.unit is not None and self.unit.unit_conf is not None
        return self.unit.unit_conf.imager

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(
        self,
        unit=None,
        imager_type: str | None = None,
        params: dict | None = None,  # type: ignore
    ):
        """
        Initializes the backend of an Imager instance according to the unit configuration.

        :param unit: "Unit" instance
        :param imager_type: The type of imager to use, e.g., "ascom:prog_id", "phd2", "zwo"
        :param imager_params: Parameters for the imager
        """
        if self._initialized:
            return  # already initialized, do not re-initialize

        SwitchedOutlet.group(
            domain=OutletDomain.UnitOutlets, group_name="Camera", outlet_names=["Camera", "CameraUSB"]
        ).transfer_attributes(self)
        if not self.is_on():
            self.power_on()

        ImagerInterface.__init__(self)

        self.unit = unit
        assert unit is not None and unit.unit_conf is not None, "Imager: no unit configuration"

        imager_type = imager_type or self.conf.imager_type.lower()
        if not (imager_type.startswith("ascom") or (imager_type in Imager.valid_imager_types())):
            raise ValueError(f"bad imager type '{imager_type}', must be one of {Imager.valid_imager_types()}")

        if imager_type.startswith("ascom:"):
            from imagers.ascom import ASCOMImager

            self._prog_id = imager_type[6:]
            self._backend = ASCOMImager(parent_imager=self, prog_id=self._prog_id, _from_imager=True)
            self.backend_type = ImagerTypes.Ascom
        elif imager_type == "phd2":
            from phd2 import phd2

            self._backend = phd2.PHD2Connector(parent=self, _from_imager=True)
            self.backend_type = ImagerTypes.Phd2
        elif imager_type == "zwo":
            from zwo import ZWOImager

            self._backend = ZWOImager(parent_imager=self, imager_params=params, _from_imager=True)
            self.backend_type = ImagerTypes.Zwo
        else:
            raise ValueError(f"Unknown imager type: {self.conf.imager_type}")

        self.imager_params = params
        self.latest_settings: ImagerSettings | None = None
        self.current_exposure_series: ImagerExposureSeries | None = None
        self._streaks = FailureStreaks()
        self._initialized = True

    def __repr__(self):
        return f"Imager(_backend='{self._backend.__repr__()}')"

    @property
    def can_send_image_ready_event(self) -> bool:
        return self._backend.can_send_image_ready_event

    @property
    def can_send_image_saved_event(self) -> bool:
        return self._backend.can_send_image_saved_event

    # ------------------------------------------------------------------- the cooler

    @property
    def set_point(self) -> float | None:
        return self._backend.set_point

    @property
    def can_set_cooler_set_point(self) -> bool:
        return self._backend.can_set_cooler_set_point

    def set_cooler_set_point(self, degrees: float) -> None:
        self._backend.set_cooler_set_point(degrees)

    @property
    def _configured_set_point(self) -> float | None:
        conf = getattr(getattr(self, "unit", None), "unit_conf", None)
        return getattr(getattr(conf, "imager", None), "cooler_set_point", None)

    def _apply_set_point(self) -> None:
        """Before the backend starts: write `imager.cooler_set_point` where the backend can.

        Read live, so a config edit takes effect at the next startup. A backend that cannot
        set it (PHD2: the set point is its profile's) is checked after startup instead -- see
        `_check_set_point`. A failure to apply is logged, not raised: the camera still cools,
        to whatever target it held.
        """
        wanted = self._configured_set_point
        if wanted is None or not self.can_set_cooler_set_point:
            return
        try:
            self.set_cooler_set_point(wanted)
            logger.info(f"imager: cooler set point {wanted:.1f} C (imager.cooler_set_point)")
        except Exception as ex:  # noqa: BLE001 -- reported; the camera keeps its previous target
            logger.exception(f"imager: could not apply cooler set point {wanted:.1f} C: {ex}")

    def _check_set_point(self) -> None:
        """After startup, on a backend that cannot set the target: warn if the camera's set
        point differs from the configured one, so a drifted PHD2 profile is noticed."""
        wanted = self._configured_set_point
        if wanted is None or self.can_set_cooler_set_point:
            return
        actual = self.set_point
        if actual is not None and abs(float(actual) - wanted) > COOLING_TOLERANCE_DEGREES:
            logger.warning(
                f"imager: the camera's set point is {float(actual):.1f} C but imager.cooler_set_point is "
                f"{wanted:.1f} C, and this backend cannot set it -- update the {self.backend_type} profile"
            )

    def start_cooling_down(self) -> None:
        """Hold `ImagerActivities.CoolingDown` until the sensor reaches the set point.

        Called whenever the cooler is switched on: by `startup`, by `turn_cooler_on`, and by a
        backend that switches it on itself (PHD2's constructor and exposure path). A watcher
        thread reads the camera every `imager.temp_check_interval` seconds and ends the flag
        once the sensor is within COOLING_TOLERANCE_DEGREES of the set point. While it is up
        the imager is not operational. One implementation for every backend.
        """
        if self.is_active(ImagerActivities.CoolingDown):
            return
        # Not before the backend exists: a backend that switches its cooler on during its own
        # construction (inside `__init__`, before `_backend` is assigned) would start a watcher
        # that reads a missing `_backend`. `startup()` starts the cool-down for it instead.
        if getattr(self, "_backend", None) is None:
            return
        self._cooling_reading = None
        self.start_activity(ImagerActivities.CoolingDown)
        threading.Thread(name="imager-cooldown", target=self.do_cool_down, daemon=True).start()

    def end_cooling_down(self) -> None:
        if self.is_active(ImagerActivities.CoolingDown):
            self.end_activity(ImagerActivities.CoolingDown)

    @property
    def _cooling_poll_seconds(self) -> float:
        conf = getattr(getattr(self, "unit", None), "unit_conf", None)
        interval = getattr(getattr(conf, "imager", None), "temp_check_interval", None)
        return float(interval) if interval else COOLING_POLL_SECONDS_DEFAULT

    def do_cool_down(self) -> None:
        """End `CoolingDown` once the sensor is within tolerance of the set point.

        Exits, ending the flag, if the backend disconnects (a shutdown). Keeps waiting
        otherwise -- a set point out of the cooler's reach keeps the imager non-operational,
        reported in `why_not_operational` and warned about once after
        COOLING_SLOW_WARNING_SECONDS.
        """
        started = time.monotonic()
        warned = False
        while self.is_active(ImagerActivities.CoolingDown):
            if not self._backend.connected:
                self.end_cooling_down()
                return
            try:
                temperature, set_point = self._backend.temperature, self._backend.set_point
            except Exception as ex:  # noqa: BLE001 -- a failed read is retried on the next poll
                logger.warning(f"imager: cool-down could not read the camera: {ex}")
                temperature = set_point = None
            if temperature is not None and set_point is not None:
                self._cooling_reading = (float(temperature), float(set_point))
                if float(temperature) <= float(set_point) + COOLING_TOLERANCE_DEGREES:
                    logger.info(
                        f"imager: cooled to {float(temperature):.1f} C (set point {float(set_point):.1f} C) "
                        f"in {time.monotonic() - started:.0f}s"
                    )
                    self.end_cooling_down()
                    return
            if not warned and time.monotonic() - started > COOLING_SLOW_WARNING_SECONDS:
                logger.warning(
                    f"imager: still cooling after {time.monotonic() - started:.0f}s, reading {self._cooling_reading}; "
                    "the set point may be out of the cooler's reach at this ambient temperature"
                )
                warned = True
            time.sleep(self._cooling_poll_seconds)

    @endpoint(tier=Tier.INTERFACE, completion=Completion.IMMEDIATE)
    def startup(self) -> CanonicalResponse:
        # Here rather than in a backend: the walk reads the PnP tree, not the camera, so it
        # answers the same whichever backend holds the camera (#264).
        log_usb_link()
        self._apply_set_point()
        response = self._backend.startup()
        self._check_set_point()
        if self._backend.connected and self._backend.cooler_on:
            self.start_cooling_down()
        return response

    @endpoint(tier=Tier.INTERFACE, completion=ImagerActivities.ShuttingDown)
    def shutdown(self) -> CanonicalResponse:
        response = self._backend.shutdown()
        # The machine's power_down_on_shutdown (opmode-design 4a); off by default. On a thread:
        # powerdown() waits for the backend's shutdown (a warm-up, on the ASCOM camera) with no
        # deadline, which must not hold the shutdown request. It calls the backend's shutdown,
        # not this one, so there is no recursion.
        if self._power_down_on_shutdown():
            threading.Thread(name="imager-powerdown-thread", target=self.do_powerdown, daemon=True).start()
        return response

    def _power_down_on_shutdown(self) -> bool:
        conf = getattr(getattr(self, "unit", None), "unit_conf", None)
        return bool(getattr(conf, "power_down_on_shutdown", False))

    @property
    def is_shutting_down(self) -> bool:
        return self._backend.is_shutting_down

    def powerdown(self):
        self.do_powerdown()

    def do_powerdown(self):
        """The power-down itself: shut the backend down if needed, wait for it, power off.

        Separate from `powerdown()` so it can be a thread target (do_-named, by convention)."""
        if not self._backend.was_shut_down:
            self._backend.shutdown()
        while self._backend.is_shutting_down:
            time.sleep(1)
        self.power_off()

    @property
    def name(self) -> str:
        """
        The getter method for the imager's name.
        :return: The name of the imager
        """
        return self._backend.name

    @property
    def connected(self) -> bool:
        """
        Check if the imager is connected.
        :return: True if connected, False otherwise
        """
        return self._backend.connected

    @connected.setter
    def connected(self, value: bool):
        """
        Connect to the imager.
        This method should be called before any other methods that require a connection.
        :param value: True to connect, False to disconnect
        """
        self._backend.connected = value

    @property
    def camera_x_size(self) -> int | None:
        """
        Get the camera's X size in pixels.
        :return: The X size of the camera
        """
        return self._backend.camera_x_size

    @property
    def camera_y_size(self) -> int | None:
        """
        Get the camera's Y size in pixels.
        :return: The Y size of the camera
        """
        return self._backend.camera_y_size

    @property
    def cooler_power(self) -> float | None:
        """
        Get the current power of the camera cooler.
        :return: The cooler power in watts
        """
        return self._backend.cooler_power

    @property
    def operational(self) -> bool:
        """
        Check if the imager is operational: the backend is, and the camera is not still
        cooling to its set point (`start_cooling_down`).
        """
        return self._backend.operational and not self.is_active(ImagerActivities.CoolingDown)

    @property
    def why_not_operational(self) -> list[str]:
        """
        Get the reason why the imager is not operational.
        :return: A string explaining why the imager is not operational, or None if it is operational
        """
        reasons = list(self._backend.why_not_operational)
        if self.is_active(ImagerActivities.CoolingDown):
            reading = self._cooling_reading
            if reading:
                reasons.append(f"{COOLING_DOWN_REASON}, {reading[0]:.1f} C, set point {reading[1]:.1f} C")
            else:
                reasons.append(f"{COOLING_DOWN_REASON} to the set point")
        return reasons

    def abort(self) -> CanonicalResponse:
        """
        Immediately terminates any in-progress activities and returns the imager to its default state.
        :return: CanonicalResponse indicating the result of the operation
        """
        return self._backend.endpoint_abort()

    @endpoint(tier=Tier.INTERFACE, completion=Completion.IMMEDIATE)
    def status(self) -> ImagerStatus:
        """
        Returns the imager's current status.
        :return: ImagerStatus object containing the status information
        """
        if not self.connected:
            if self._streaks.begins("connected"):
                logger.error(f"imager: {self.name} is not connected; status reads no hardware until it is")
            return ImagerStatus(
                connected=False,
                powered=self.is_on(),
                latest_settings=self.latest_settings,
                activities=self.activities,
                activities_verbal=self.activities_verbal,
                backend=self._backend.status(),
            )
        if self._streaks.ends("connected"):
            logger.info(f"imager: {self.name} is connected again")

        return ImagerStatus(
            # detected=self.detected,
            connected=self.connected,
            # operational=self.operational,
            # why_not_operational=self.why_not_operational,
            camera_x_size=self.camera_x_size,
            camera_y_size=self.camera_y_size,
            temperature=self.temperature,
            powered=self.is_on(),
            cooler_on=self.cooler_on,
            cooler_power=self.cooler_power,
            set_point=self._backend.set_point,
            latest_settings=self.latest_settings,
            activities=self.activities,
            activities_verbal=self.activities_verbal,
            backend=self._backend.status(),
        )

    def connect(self) -> CanonicalResponse:  # obsoleted by connected property
        self._backend.connected = True
        return CanonicalResponse_Ok

    def disconnect(self) -> CanonicalResponse:  # obsoleted by connected property
        self.connected = False
        return CanonicalResponse_Ok

    def start_exposure_series(self, purpose: str | None = None) -> ImagerExposureSeries:
        """
        An exposure series allows the imager backend to perform pre/post exposure activities.

        For example: the _`phd2`_ backend needs to stop/restart guiding if it was guiding when the series started
        """
        self.current_exposure_series = ImagerExposureSeries(purpose=purpose)
        logger.info(
            f"Starting exposure series id='{self.current_exposure_series.series_id}' "
            + f"purpose='{self.current_exposure_series.purpose}'"
        )
        return self.current_exposure_series

    def end_exposure_series(self, series: ImagerExposureSeries):
        """
        Ends the exposure series and cleans up resources.
        :param series: The ImagerExposureSeries to end
        """
        assert self.current_exposure_series is not None, "No current exposure series to end"
        if self.current_exposure_series.series_id != series.series_id:
            raise ValueError(
                f"Cannot end exposure series {series.series_id}, "
                + f"current series is {self.current_exposure_series.series_id}"
            )
        logger.info(f"Ending exposure series id='{series.series_id}', purpose='{series.purpose}'")
        self._backend.end_exposure_series(series)

    @endpoint(tier=Tier.OPERATION)
    def start_exposure(self, settings: ImagerSettings) -> CanonicalResponse:
        """
        Starts an exposure with the given settings.
        :param settings: ImagerSettings object containing the exposure settings
        :return: CanonicalResponse indicating the result of the operation
        """
        self.latest_settings = settings
        return self._backend.start_exposure(settings)

    @endpoint(tier=Tier.OPERATION)
    def stop_exposure(self) -> CanonicalResponse:
        """
        Stops the current exposure.
        :return: CanonicalResponse indicating the result of the operation
        """
        return self._backend.stop_exposure()

    def abort_exposure(self) -> CanonicalResponse:
        """
        Aborts the current exposure.
        :return: CanonicalResponse indicating the result of the operation
        """
        return self._backend.abort_exposure()

    @property
    def can_image_to_memory(self) -> bool:
        """
        Check if the imager can capture images to memory.
        """
        return self._backend.can_image_to_memory

    @property
    def image_array(self) -> np.ndarray | None:
        """
        Gets the image data from the imager.
        This method should be called after an exposure has been taken.
        :return: The image data as bytes
        """
        return self._backend.image_array if self._backend.can_image_to_memory else None

    @property
    def temperature(self) -> float | None:
        """
        Gets the current temperature of the camera.
        """
        return self._backend.temperature

    def wait_for_image_ready(self):
        """
        Waits for the image to be ready after an exposure.
        """
        if self.can_image_to_memory:
            self._backend.wait_for_image_ready()

    def wait_for_image_saved(self):
        """
        Waits for the image to be saved after an exposure.
        """
        self._backend.wait_for_image_saved()

    @property
    def cooler_on(self) -> bool | None:
        """
        Checks if the camera cooler is currently on.
        """
        return self._backend.cooler_on

    @cooler_on.setter
    def cooler_on(self, onoff: bool):
        """
        Turns the camera cooler on or off.
        :param onoff: True to turn on, False to turn off
        """
        self._backend.cooler_on = onoff

    @property
    def detected(self) -> bool:
        return self._backend.detected

    @property
    def was_shut_down(self) -> bool:
        return self._backend.was_shut_down

    @property
    def default_settings(self):
        return self._backend.default_settings

    @endpoint(tier=Tier.OPERATION, completion=Completion.IMMEDIATE)
    def turn_cooler_on(self) -> CanonicalResponse:
        self.cooler_on = True
        self.start_cooling_down()
        return CanonicalResponse_Ok

    @endpoint(tier=Tier.OPERATION, completion=Completion.IMMEDIATE)
    def turn_cooler_off(self) -> CanonicalResponse:
        self.end_cooling_down()
        self.cooler_on = False
        return CanonicalResponse_Ok

    @property
    def api_router(self) -> APIRouter:
        """
        Returns the API router for the imager.
        This is used to register the imager's API endpoints.
        """
        base_imager_path = Const.BASE_UNIT_PATH + "/imager"

        router = APIRouter()
        register_component_endpoints(router, self, base_imager_path)
        add_api_route(
            router,
            base_imager_path + "/start_exposure",
            endpoint=self.start_exposure,
            methods=["PUT"],
        )
        add_api_route(router, base_imager_path + "/stop_exposure", endpoint=self.stop_exposure, methods=["PUT"])
        add_api_route(router, base_imager_path + "/cooler_on", endpoint=self.turn_cooler_on, methods=["PUT"])
        add_api_route(router, base_imager_path + "/cooler_off", endpoint=self.turn_cooler_off, methods=["PUT"])

        return router
