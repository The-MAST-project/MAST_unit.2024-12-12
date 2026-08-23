import argparse
import os
import sys
import time
from contextlib import asynccontextmanager

import psutil
import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import ValidationError

from common.config import Config, ConfigError
from common.endpoints import OPENAPI_TAGS
from common.filer import Filer
from common.mast_logging import configure_logging, get_logger
from common.process import ensure_process_is_running
from common.utils import boxed_info
from PlaneWave import pwi4_client
from PlaneWave.ps3cli_locate import locate_ps3cli_catalog, locate_ps3cli_dir

# Logging is configured once, here, before anything logs. Every 'mast.*' logger
# inherits the handlers and level from root by propagation.
# Precedence: --log-level > MAST_LOG_LEVEL > default.
_parser = argparse.ArgumentParser(add_help=False)
_parser.add_argument("--log-level", default=None, help="DEBUG, INFO, WARNING, ... (overrides MAST_LOG_LEVEL)")
configure_logging(_parser.parse_known_args()[0].log_level)

logger = get_logger(__name__)


def app_quit(reason: str):
    boxed_info(logger, f"Quiting ({reason=}) !")
    parent_pid = os.getpid()
    parent = psutil.Process(parent_pid)
    for child in parent.children(recursive=True):  # or parent.children() for recursive=False
        logger.info(f"killing process {child.pid=}, '{child.name()}'")
        child.kill()
    parent.kill()


def start_supporting_processes() -> None:
    """Start the third-party programs the unit drives, and wait for PWI4 to answer.

    This ran at module scope until #114, which means merely importing this module
    started PWI4, the shutter and the plate solver -- on a unit machine, against the
    real telescope -- and blocked up to 30 s doing it. `tests/conftest.py` still blocks
    process launches as a backstop, but the fix is here: nothing spawns until `main()`
    asks for it.

    The sequence is unchanged from what module import used to perform, in the same
    order, so `python app.py` starts exactly as before.
    """
    # Get rid of HTTP proxy environment variables.  We're talking to PWI4 which lives on this same machine
    if "http_proxy" in os.environ:
        del os.environ["http_proxy"]
    if "https_proxy" in os.environ:
        del os.environ["https_proxy"]

    ensure_process_is_running(
        name="PWI4.exe",
        cmd="C:\\Program Files (x86)\\PlaneWave Instruments\\PlaneWave Interface 4\\PWI4.exe",
        logger=logger,
        shell=True,
    )

    # Try to talk to PWI4 at startup; proceed without it if unavailable.
    pwi4_ok = False
    pwi4_deadline = time.monotonic() + 30
    while time.monotonic() < pwi4_deadline:
        try:
            pw = pwi4_client.PWI4()
            pw.status()
            logger.info("OK, established connection to PWI4")
            pwi4_ok = True
            break
        except pwi4_client.PWException:
            logger.warning("PWI4 not ready yet, retrying ...")
            time.sleep(1)
        except Exception:
            logger.exception("cannot connect to PWI4")
            break
    if not pwi4_ok:
        logger.warning("PWI4 unavailable at startup - unit will start with mount unavailable")

    ps3cli_dir = locate_ps3cli_dir()
    ps3cli_catalog = locate_ps3cli_catalog()
    if ps3cli_dir is None:
        logger.error("ps3cli.exe not found in any known location; skipping ps3cli startup")
    elif ps3cli_catalog is None:
        logger.error(
            "PlateSolve catalog (a directory containing UC4 and Orca subdirectories) "
            "not found; ps3cli --server cannot start. Install the catalog or set "
            "PS3CLI_CATALOG to its location. Skipping ps3cli startup."
        )
    else:
        # --root-path tells ps3cli where the UC4/Orca catalog lives; without it the
        # server cannot auto-detect a catalog and exits immediately.
        ensure_process_is_running(
            name="ps3cli.exe",
            cwd=ps3cli_dir,
            cmd=f'ps3cli.exe --server --port=8998 --root-path="{ps3cli_catalog}"',
            logger=logger,
            shell=True,
            log_stdout_and_stderr=True,
        )


# Configure logging for WebSocketProtocol
# logging.basicConfig(level=logging.DEBUG)
# logger = logging.getLogger("uvicorn.protocols.websockets.websockets_impl.WebSocketProtocol")
# logger.setLevel(logging.DEBUG)


# @app.websocket_route(Const.BASE_UNIT_PATH + '/unit_visual_ws')
# async def unit_visual_websocket(websocket: WebSocket):
#     await unit.unit_visual_ws(websocket)


async def websocket_disconnect_handler(websocket: WebSocket, exc: WebSocketDisconnect):
    logger.info(f"websocket disconnected: {exc.code}")
    await websocket.close()


# The unit attributes that carry a component router, in mounting order. A component is
# None when it failed to build, and a unit still serves the ones it has.
COMPONENT_ATTRIBUTES = ("mount", "covers", "focuser", "stage", "imager")


def mount_routers(app: FastAPI, unit) -> None:
    """Mount the unit router and every component router the unit managed to build.

    Every route reachable from here was registered through `common.endpoints.add_api_route`,
    which refuses a handler that declares no tier. Adding one: see
    `docs/adding-an-endpoint.md`.
    """
    app.include_router(unit.api_router)
    for attribute in COMPONENT_ATTRIBUTES:
        component = getattr(unit, attribute)
        if component:
            app.include_router(component.api_router)


def create_app(unit=None) -> FastAPI:
    """Build the unit's ASGI app: handlers, middleware, and the component routers.

    Router mounting lived in the `if __name__ == "__main__"` block until #114, so an
    imported `app` carried exactly one route (`/favicon.ico`) and none of the 74
    registered operations could be reached from a test. It lives here now, which is
    what lets `TestClient(create_app(unit))` see the real surface (#52).

    `unit` is left unannotated on purpose: annotating it `Unit` would need a
    module-scope `from unit import Unit`, and that import is the whole Windows /
    hardware / Mongo chain this factory exists to stay clear of. Passing `None`
    yields the bare app -- handlers and middleware, no component routes.

    Which routers mount stays conditional per component, exactly as before: a unit
    that failed to build a focuser still serves the components it does have.
    """

    @asynccontextmanager
    async def lifespan(fast_app: FastAPI):
        """Run the unit's lifespan hooks around the served app.

        Closes over the `unit` this factory was given rather than calling `Unit()`
        itself, as it did until #114. `Unit` was never imported at module scope --
        only inside the `__main__` block -- so off the `python app.py` path the call
        raised `NameError`, the `except Exception` swallowed it, and `start_lifespan`
        / `end_lifespan` were silently skipped. (`Unit` is a singleton with an
        `_initialized` guard, so the old second construction was a no-op, not a
        duplicate instance; what was broken was the dependency on a global that
        another code path happened to bind.)
        """
        # Before anything is operational, so everything on the ram disk is by definition a
        # leftover from a previous run -- no live folder to race, and no product/scratch
        # judgement to get wrong. Deliberately here rather than in a component's startup():
        # that is an HTTP endpoint an operator can call again mid-night, when a sweep would
        # relocate folders that are in use. MAST_common#52.
        #
        # Runs even when `unit` is None: a sweep of last night's leftovers does not need a
        # unit, and the bare app is also what a test builds.
        Filer(logger).start_product_relocation_sweep(logger=logger)

        if unit is None:
            yield
        else:
            unit.start_lifespan()
            yield
            unit.end_lifespan()

        # Drain outstanding ram->shared moves while the process is still healthy. Without
        # this they are abandoned at interpreter teardown, which is how MAST_common#52 was
        # first seen: a solve's cleanup racing service shutdown.
        Filer(logger).flush()

    app = FastAPI(
        docs_url="/docs",
        redoc_url=None,
        lifespan=lifespan,
        openapi_url="/openapi.json",
        openapi_tags=OPENAPI_TAGS,
        debug=True,
        # exception_handlers={WebSocketDisconnect: websocket_disconnect_handler},
    )

    @app.exception_handler(RequestValidationError)
    async def request_validation_exception_handler(request: Request, exc: RequestValidationError):
        logger.error("Request validation error", exc_info=exc)
        # Optionally return the default structure so client still sees details
        return JSONResponse(status_code=422, content={"detail": exc.errors(), "body": exc.body})

    @app.exception_handler(ValidationError)
    async def pydantic_validation_exception_handler(request: Request, exc: ValidationError):
        logger.error("Pydantic validation error", exc_info=exc)
        return JSONResponse(status_code=400, content={"error": exc.errors()})

    @app.get("/", include_in_schema=False)
    def read_root():
        """Send the bare root to the tier-grouped Swagger page (#170).

        Out of the schema on purpose: every documented route carries exactly one tier tag
        (#39), and this one is navigation rather than an operation to call.
        """
        return RedirectResponse(url="/docs")

    @app.get("/favicon.ico")
    def read_favicon():
        return RedirectResponse(url="/static/favicon.ico")

    @app.exception_handler(Exception)
    async def generic_exception_handler(request, exc: Exception):
        from common.utils import function_name

        return JSONResponse(
            status_code=500,
            content={"message": f"{function_name()}: Exception occurred: {exc}"},
        )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    if unit is not None:
        mount_routers(app, unit)

    return app


def main():
    boxed_info(logger, "Starting ...")

    # Kept ahead of the configuration check because that is the order module import
    # used to impose: the spawns ran at import, the check ran in the __main__ block
    # below them.
    start_supporting_processes()

    # Validate the configuration before doing anything else. A missing or invalid
    # config file (or a config that disagrees with the DB 'sites' document) must
    # fail startup loudly with a detailed reason, not limp along with bad values.
    try:
        Config()
    except ConfigError as ex:
        logger.error(f"Configuration error, cannot start:\n{ex}")
        app_quit(reason=f"configuration error: {ex}")
        sys.exit(1)

    service_conf = Config().get_service(service_name="unit")
    if service_conf is None:
        logger.error("No server configuration found for 'unit', exiting ...")
        app_quit(reason="no server configuration")
    else:
        host = service_conf.listen_on
        port = service_conf.port

    from unit import Unit

    try:
        unit = Unit()
    except Exception as ex:
        logger.exception("Unit initialization failed")
        app_quit(reason=f"unit initialization failed: {ex}")
        unit = None

    if unit:
        app = create_app(unit)

        logger.info(f"The MAST Unit server is starting on {host}:{port} ...")

        uvicorn.run(app, host=host, port=port)
    else:
        logger.error("Unit is not initialized, cannot start the server.")
        app_quit(reason="unit not initialized")


if __name__ == "__main__":
    main()
