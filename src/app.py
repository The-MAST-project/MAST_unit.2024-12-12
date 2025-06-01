import logging
import os
import socket
from contextlib import asynccontextmanager
from pathlib import Path

import psutil
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import ORJSONResponse, RedirectResponse

from common.config import Config
from common.mast_logging import init_log
from common.process import ensure_process_is_running
from PlaneWave import pwi4_client

#
# Log level configuration from the 'global' section of the 'config' file
#
unit_conf = Config().get_unit(socket.gethostname())

# if 'log_level' in unit_conf['global']:
#     log_level = getattr(logging, unit_conf['global']['log_level'].upper())
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

# try, as soon as possible, to talk to PWI4 and quit if not possible
while True:
    try:
        pw = pwi4_client.PWI4()
        pw.status()
        logger.info("OK, established connection to PWI4")
        break
    except pwi4_client.PWException as ex:
        logger.error("no PWI4 yet, waiting ...", exc_info=ex)
        continue
    except Exception as ex:
        logger.error("cannot connect to PWI4, giving up!", exc_info=ex)
        app_quit(reason="cannot talk to PWI4")

ensure_process_is_running(
    name="PWShutter.exe",
    cmd="C:\\Program Files (x86)\\PlaneWave Instruments\\"
    + "PlaneWave Shutter Control\\PWShutter.exe",
    logger=logger,
    shell=True,
)


ensure_process_is_running(
    name="ps3cli.exe",
    cwd=str(Path("C:\\Program Files (x86)\\PlaneWave Instruments\\ps3cli\\ps3cli-2024-09-10").as_posix()),
    cmd="ps3cli.exe --server --port=8998",
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



if __name__ == "__main__":
    server_conf = Config().get_service(service_name="unit")
    host = server_conf.get("listen_on", "0.0.0.0")
    port = server_conf.get("port", 8000)

    from unit import unit

    if not unit:
        logger.error("Unit is not initialized, exiting ...")
        app_quit(reason="unit not initialized")

    @asynccontextmanager
    async def lifespan(fast_app: FastAPI):
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

    if unit:
        app.include_router(unit.api_router)
        app.include_router(unit.mount.api_router)
        app.include_router(unit.covers.api_router)
        app.include_router(unit.focuser.api_router)
        app.include_router(unit.stage.api_router)
        app.include_router(unit.imager.api_router)

        logger.info(f"The MAST Unit server is starting on {host}:{port} ...")

        uvicorn.run(app, host=host, port=port, log_level=log_level)
    else:
        logger.error("Unit is not initialized, cannot start the server.")
        app_quit(reason="unit not initialized")
