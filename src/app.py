import logging
import os
import socket
import subprocess
import time
from contextlib import asynccontextmanager
from pathlib import Path

import psutil
import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, ORJSONResponse, RedirectResponse
from pydantic import ValidationError

from common.config import Config
from common.mast_logging import init_log
from common.process import ensure_process_is_running
from PlaneWave import pwi4_client

#
# Log level configuration from the 'global' section of the 'config' file
#
unit_conf = Config().get_unit(site_name=None, unit_name=socket.gethostname().split('.')[0])

# if 'log_level' in unit_conf.global:
#     log_level = getattr(logging, unit_conf.global.log_level.upper())
# else:
log_level = logging.WARNING
logging.basicConfig(level=log_level)
logger = logging.getLogger("mast.unit." + __name__)
init_log(logger)

logger.info("+--------------+")
logger.info("| Starting ... |")
logger.info("+--------------+")

# Get rid of HTTP proxy environment variables.  We're talking to PWI4 which lives on this same machine
if "http_proxy" in os.environ:
    del os.environ["http_proxy"]
if "https_proxy" in os.environ:
    del os.environ["https_proxy"]


def app_quit(reason: str):
    logger.info(f"Quiting ({reason=}) !")
    parent_pid = os.getpid()
    parent = psutil.Process(parent_pid)
    for child in parent.children(
        recursive=True
    ):  # or parent.children() for recursive=False
        logger.info(f"killing process {child.pid=}, '{child.name()}'")
        child.kill()
    parent.kill()


ensure_process_is_running(
    name="PWI4.exe",
    cmd="C:\\Program Files (x86)\\PlaneWave Instruments\\PlaneWave Interface 4\\PWI4.exe",
    logger=logger,
    shell=True,
)

# Try to talk to PWI4 at startup; proceed without it if unavailable.
_pwi4_ok = False
_pwi4_deadline = time.monotonic() + 30
while time.monotonic() < _pwi4_deadline:
    try:
        pw = pwi4_client.PWI4()
        pw.status()
        logger.info("OK, established connection to PWI4")
        _pwi4_ok = True
        break
    except pwi4_client.PWException:
        logger.warning("PWI4 not ready yet, retrying ...")
        time.sleep(1)
    except Exception as ex:
        logger.error(f"cannot connect to PWI4: {ex}")
        break
if not _pwi4_ok:
    logger.warning("PWI4 unavailable at startup - unit will start with mount unavailable")

ensure_process_is_running(
    name="PWShutter.exe",
    cmd="C:\\Program Files (x86)\\PlaneWave Instruments\\"
    + "PlaneWave Shutter Control\\PWShutter.exe",
    logger=logger,
    shell=True,
)


def check_ps3cli() -> None:
    """Verify ps3cli.exe is present and executable. It is a one-shot solver tool,
    not a persistent process, so we only probe it at startup rather than keep it running."""
    ps3cli_exe = "C:\\Users\\mast\\Documents\\PlaneWave\\ps3cli\\ps3cli\\ps3cli.exe"
    if not Path(ps3cli_exe).exists():
        logger.error(f"ps3cli health check: exe not found at {ps3cli_exe}")
        return
    try:
        result = subprocess.run(
            [ps3cli_exe],
            capture_output=True,
            timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        # Exit code 1 = invalid arguments (no args given) -- binary loaded and ran correctly.
        if result.returncode in (0, 1):
            logger.info(f"ps3cli health check: OK (exit code {result.returncode})")
        else:
            logger.warning(f"ps3cli health check: unexpected exit code {result.returncode}")
    except Exception as e:
        logger.error(f"ps3cli health check: failed to run: {e}")


check_ps3cli()


# Configure logging for WebSocketProtocol
# logging.basicConfig(level=logging.DEBUG)
# logger = logging.getLogger("uvicorn.protocols.websockets.websockets_impl.WebSocketProtocol")
# logger.setLevel(logging.DEBUG)


# @app.websocket_route(Const.BASE_UNIT_PATH + '/unit_visual_ws')
# async def unit_visual_websocket(websocket: WebSocket):
#     await unit.unit_visual_ws(websocket)


@asynccontextmanager
async def lifespan(fast_app: FastAPI):

    unit = Unit()
    if unit:
        unit.start_lifespan()
        yield
        unit.end_lifespan()


async def websocket_disconnect_handler(websocket: WebSocket, exc: WebSocketDisconnect):
    logger.info(f"websocket disconnected: {exc.code}")
    await websocket.close()


app = FastAPI(
    docs_url="/docs",
    redocs_url=None,
    lifespan=lifespan,
    openapi_url="/openapi.json",
    debug=True,
    default_response_class=ORJSONResponse,
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

@app.get("/favicon.ico")
def read_favicon():
    return RedirectResponse(url="/static/favicon.ico")


@app.exception_handler(Exception)
async def generic_exception_handler(request, exc: Exception):
    from common.utils import function_name

    return ORJSONResponse(
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

if __name__ == "__main__":
    service_conf = Config().get_service(service_name="unit")
    if service_conf is None:
        logger.error("No server configuration found for 'unit', exiting ...")
        app_quit(reason="no server configuration")
    else:
        host = service_conf.listen_on
        port = service_conf.port

    from unit import Unit

    unit = Unit()
    if not unit:
        logger.error("Unit is not initialized, exiting ...")
        app_quit(reason="unit not initialized")

    if unit:
        app.include_router(unit.api_router)
        if unit.mount:
            app.include_router(unit.mount.api_router)
        if unit.covers:
            app.include_router(unit.covers.api_router)
        if unit.focuser:
            app.include_router(unit.focuser.api_router)
        if unit.stage:
            app.include_router(unit.stage.api_router)
        if unit.imager:
            app.include_router(unit.imager.api_router)

        logger.info(f"The MAST Unit server is starting on {host}:{port} ...")

        uvicorn.run(app, host=host, port=port, log_level=log_level)
    else:
        logger.error("Unit is not initialized, cannot start the server.")
        app_quit(reason="unit not initialized")
