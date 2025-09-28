import datetime
import logging
import socket
from pathlib import Path
from venv import logger

import astropy.io.fits as fits

from common.activities import ImagerActivities
from common.mast_logging import init_log
from common.utils import function_name

logger = logging.getLogger("mast.unit.imagers." + __name__)
init_log(logger)


def save_to_fits_file(imager_backend):
    op = function_name()

    settings = imager_backend.latest_settings
    assert (
        settings is not None
        and settings.roi is not None
        and settings.binning is not None
        and settings.image_path is not None
    )

    imager_backend.parent_imager.start_activity(ImagerActivities.Saving)

    header = fits.Header()
    header["SIMPLE"] = (True, "file conforms to FITS standard")
    if settings.format == "raw8":
        header["BITPIX"] = (8, "uint8 array data type")
    elif settings.format == "raw16":
        header["BITPIX"] = (16, "int16 array data type")
        header["BZERO"] = (32768,)
        header["BSCALE"] = (1,)
    header["NAXIS"] = (2, "number of array dimensions")
    header["NAXIS1"] = (settings.roi.width, "length of data axis 1")
    header["NAXIS2"] = (settings.roi.height, "length of data axis 2")
    header["EXTEND"] = (True, "FITS data sets may contain extensions")
    header["DATE-OBS"] = (
        datetime.datetime.now(datetime.UTC).isoformat(),
        "observation datetime",
    )
    header["XBINNING"] = (settings.binning.x, "horizontal binning")
    header["YBINNING"] = (settings.binning.y, "vertical binning")
    header["EXPTIME"] = (settings.seconds, "exposure time in seconds")
    header["INSTRUME"] = (socket.gethostname(), "the instrument")
    if imager_backend.ccd_temp_at_mid_exposure:
        header["CCDTEMP"] = (
            imager_backend.ccd_temp_at_mid_exposure,
            "ccd temp. at mid exposure",
        )
        imager_backend.ccd_temp_at_mid_exposure = None

    header["IMAGER"] = (imager_backend.name, "the imager backend")

    if imager_backend.parent_imager.unit:
        header["FOCUSPOS"] = imager_backend.parent_imager.unit.focuser.position
        header.comments["FOCUSPOS"] = "focuser position"
        header["STAGEPOS"] = imager_backend.parent_imager.unit.stage.position
        header.comments["STAGEPOS"] = "FCU stage position"

    if settings.fits_cards:
        for k, v in settings.fits_cards.items():
            header[k] = v

    assert imager_backend.image_array is not None
    hdu = fits.PrimaryHDU(data=imager_backend.image_array, header=header)
    hdu.header.update(header)
    hdu_list = fits.HDUList([hdu])
    logger.info(f"{op}: saving image to {Path(settings.image_path).as_posix()} ...")
    try:
        hdu_list.writeto(settings.image_path, checksum=True, overwrite=True)
    except Exception as ex:
        logger.error(f"failed to save to '{settings.image_path}', {ex=}")

    imager_backend.parent_imager.end_activity(ImagerActivities.Saving)
    imager_backend.parent_imager.end_activity(ImagerActivities.Exposing)
