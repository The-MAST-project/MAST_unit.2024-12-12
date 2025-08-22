import time

import matplotlib.pyplot as plt
import numpy as np
import win32com.client as com
from astropy.io import fits

# prog_id = 'ASCOM.AlpacaDynamic3.Camera'
prog_id = "ASCOM.PlaneWaveVirtual.Camera"
cam = com.Dispatch(prog_id)

cam.Connected = True
cam.BinX = cam.BinY = 1
# Make ROI = full frame (values are in *binned* pixels)
if getattr(cam, "CanSubframe", False):
    cam.StartX = 0
    cam.StartY = 0
    cam.NumX = cam.CameraXSize // cam.BinX
    cam.NumY = cam.CameraYSize // cam.BinY
exposure_s = 5.0
cam.StartExposure(exposure_s, True)

while not cam.ImageReady:
    time.sleep(0.05)

arr = np.transpose(np.array(cam.ImageArray, dtype=np.uint16))  # use ImageArrayVariant if needed
fits.writeto("frame.fits", arr, overwrite=True)

plt.imshow(arr, origin="lower")
plt.title("ASCOM frame")
plt.show()

cam.Connected = False
