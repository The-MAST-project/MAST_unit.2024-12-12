import os.path
import socket
import time
import numpy as np
from multiprocessing.shared_memory import SharedMemory
from time import sleep
from astropy.io import fits
import os
import logging
import guiding
import json
from unit import PLATE_SOLVING_SHM_NAME

image_params_shm: SharedMemory | None = None
image_shm: SharedMemory | None = None
results_shm: SharedMemory | None = None

image_params_dict: dict

image_dir = "images"
logger = logging.getLogger("PSSimulator")
default_log_level = logging.DEBUG


class ImageCounter:
    filename: str = os.path.join(image_dir, ".counter")

    @property
    def value(self) -> int:
        try:
            with open(self.filename, "r") as f:
                ret = int(f.readline())
        except FileNotFoundError:
            ret = 0
        return ret

    @value.setter
    def value(self, v: int):
        with open(self.filename, "w") as f:
            f.write(f"{v}\n")


image_counter = ImageCounter()


def solve_image(params: dict) -> dict:
    """
    A simulated plate solver.  It:
    - gets the image parameters (a dictionary) from the guider process
    - saves the image from the 'PlateSolving_Image' shared memory segment into a FITS file (just to check
        the sharing mechanism works)
    - copies the input parameters to the 'PlateSolving_Results' shared memory segment

    Parameters
    ----------
    params: dict - A dictionary previously created from a name=value list in the 'PlateSolving_Params'
                     shared memory segment

    Returns
    -------

    """
    ret = {
        "ra": params["ra"],
        "dec": params["dec"],
        "success": True,
        "reasons": None,
    }

    width = params["width"]
    height = params["height"]
    image = np.ndarray((width, height), dtype=np.uint32, buffer=image_shm.buf)
    header = fits.Header()
    header["NAXIS1"] = width
    header["NAXIS2"] = height
    header["RA"] = ret["ra"]
    header["DEC"] = ret["dec"]
    hdu = fits.hdu.PrimaryHDU(image, header=header)

    counter = image_counter.value
    os.makedirs("images", exist_ok=True)
    image_counter.value = counter + 1

    hdu.writeto(os.path.join(image_dir, f"image-{counter}.fits"))
    logger.info(f"solved image: ra={ret['ra']} dec={ret['dec']}")
    return ret


def init_log(lg: logging.Logger):
    lg.setLevel(default_log_level)
    handler = logging.StreamHandler()
    handler.setLevel(default_log_level)
    formatter = logging.Formatter(
        "%(asctime)s - %(levelname)s - {%(name)s:%(threadName)s:%(thread)s} - %(message)s"
    )
    handler.setFormatter(formatter)
    lg.addHandler(handler)

    handler = logging.FileHandler(filename="PSSimulator.log", mode="a")
    handler.setLevel(default_log_level)
    handler.setFormatter(formatter)
    lg.addHandler(handler)


if __name__ == "__main__":

    init_log(logger)
    logger.info("---------------")
    logger.info("New PSSimulator")
    logger.info("---------------")

    #
    # Wait patiently for the guiding software to create the memory segment
    #
    image_shm = None
    while not image_shm:
        try:
            image_shm = SharedMemory(name=PLATE_SOLVING_SHM_NAME)
        except FileNotFoundError:
            logger.info(
                "Waiting for the shared memory segment (not found, sleeping 2) ..."
            )
            sleep(2)

    hello = {
        "ready": True,
        "reasons": None,
    }

    # logger.info(f"creating socket")
    socket_to_guider = socket.socket(family=socket.AF_INET)
    # logger.info(f"connecting socket")
    socket_to_guider.connect(guiding.guider_address_port)
    logger.info(f"connected socket to {guiding.guider_address_port}")

    msg = json.dumps(hello).encode("utf-8")
    # logger.info(f"sending '{msg}' on socket")
    socket_to_guider.send(msg)
    # logger.info("sent on socket")

    while True:
        """
        Loop forever (or until killed by the guiding process)
        """
        try:
            # wait for the guider software to acquire the image and place it in the shared segment
            # logger.info("receiving on socket")
            s = socket_to_guider.recv(1024)
            # logger.info(f"received '{s}' on socket")
            image_params = None
            try:
                image_params = json.loads(s.decode("utf-8"))
            except Exception as ex:
                pass  # TBD
            if not image_params:
                logger.warning("bad image_params")
                continue

            response = solve_image(image_params)
            # logger.info(f"sending '{response}' on socket")
            socket_to_guider.send(json.dumps(response).encode("utf-8"))
            # logger.info('sent response to guider')
            time.sleep(1)
        except Exception as e:
            logger.error("exception: ", e)
