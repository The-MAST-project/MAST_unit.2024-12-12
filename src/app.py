import socket

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from PlaneWave import pwi4_client
from common.mast_logging import init_log
import logging
from contextlib import asynccontextmanager
import psutil
import os
from fastapi.responses import RedirectResponse, ORJSONResponse
from fastapi.staticfiles import StaticFiles
from common.process import ensure_process_is_running
from common.config import Config
from fastapi import WebSocket, WebSocketDisconnect

#
# Log level configuration from the 'global' section of the 'config' file
#
unit_conf = Config().get_unit(socket.gethostname())

# if 'log_level' in unit_conf['global']:
#     log_level = getattr(logging, unit_conf['global']['log_level'].upper())
# else:
log_level = logging.DEBUG
logging.basicConfig(level=log_level)
logger = logging.getLogger('mast.unit.' + __name__)
init_log(logger)

logger.info('+--------------+')
logger.info('| Starting ... |')
logger.info('+--------------+')

if 'http_proxy' in os.environ:
    del os.environ['http_proxy']
if 'https_proxy' in os.environ:
    del os.environ['https_proxy']

pw = None


def app_quit():
    logger.info('Quiting!')
    parent_pid = os.getpid()
    parent = psutil.Process(parent_pid)
    for child in parent.children(recursive=True):  # or parent.children() for recursive=False
        logger.info(f"killing process {child.pid=}, '{child.name()}'")
        child.kill()
    parent.kill()


ensure_process_is_running(name='PWI4.exe',
                          cmd='C:\\Program Files (x86)\\PlaneWave Instruments\\PlaneWave Interface 4\\PWI4.exe',
                          logger=logger, shell=True)
ensure_process_is_running(name='PWShutter.exe',
                          cmd="C:\\Program Files (x86)\\PlaneWave Instruments\\" +
                              "PlaneWave Shutter Control\\PWShutter.exe",
                          logger=logger,
                          shell=True)


ensure_process_is_running(name='ps3cli.exe',
                          cwd='C:\\Program Files (x86)\\PlaneWave Instruments\\ps3cli\\ps3cli-2024-09-10',
                          cmd=f'ps3cli.exe --server --port=8998',
                          logger=logger,
                          shell=True,
                          log_stdout_and_stderr=True)

from camera import router as camera_router
from covers import router as covers_router
from mount import router as mount_router
from focuser import router as focuser_router
from stage import router as stage_router
from unit import router as unit_router
from unit import unit

while True:
    try:
        pw = pwi4_client.PWI4()
        pw.status()
        logger.info(f"Connected to PWI4")
        break
    except pwi4_client.PWException as ex:
        logger.info(f"no PWI4 yet ...")
        continue
    except Exception as ex:
        logger.error("cannot connect to PWI4", exc_info=ex)
        app_quit()


@asynccontextmanager
async def lifespan(fast_app: FastAPI):
    unit.start_lifespan()
    yield
    unit.end_lifespan()


async def websocket_disconnect_handler(websocket: WebSocket, exc: WebSocketDisconnect):
    logger.info(f"websocket disconnected: {exc.code}")
    await websocket.close()


app = FastAPI(
    docs_url='/docs',
    redocs_url=None,
    lifespan=lifespan,
    openapi_url='/openapi.json',
    debug=True,
    default_response_class=ORJSONResponse,
    # exception_handlers={WebSocketDisconnect: websocket_disconnect_handler},
)

# Configure logging for WebSocketProtocol
# logging.basicConfig(level=logging.DEBUG)
# logger = logging.getLogger("uvicorn.protocols.websockets.websockets_impl.WebSocketProtocol")
# logger.setLevel(logging.DEBUG)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.exception_handler(Exception)
async def generic_exception_handler(request, exc: Exception):
    return ORJSONResponse(status_code=500, content={'message': f"Exception occurred: {exc}"})

# @app.websocket_route(BASE_UNIT_PATH + '/unit_visual_ws')
# async def unit_visual_websocket(websocket: WebSocket):
#     await unit.unit_visual_ws(websocket)

app.include_router(unit_router)
app.include_router(mount_router)
app.include_router(covers_router)
app.include_router(focuser_router)
app.include_router(stage_router)
app.include_router(camera_router)


@app.get("/favicon.ico")
def read_favicon():
    return RedirectResponse(url="/static/favicon.ico")


if __name__ == "__main__":
    server_conf = Config().get_service(service_name='unit')
    host = server_conf['listen_on'] if 'listen_on' in server_conf else '0.0.0.0'
    port = server_conf['port'] if 'port' in server_conf else 8000

    logger.info("The MAST Unit server is starting ...")

    uvicorn.run(app, host=host, port=port, log_level=log_level)
