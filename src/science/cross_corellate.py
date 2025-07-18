# import matplotlib.pyplot as plt
import numpy as np
from astropy.io import fits
# from scipy.ndimage import shift as ndi_shift
from skimage.registration import phase_cross_correlation

pixel_scale = 0.2612

# Load FITS data and convert to float32
def load_fits_float32(path):
    assert path and path.endswith('.fits'), "Path must end with .fits"
    data = fits.getdata(path)
    assert(data is not None), "Failed to load data from the FITS file"
    return data.astype(np.float32) # type: ignore
    # with fits.open(path) as hdul:
    #     return hdul[0].data.astype(np.float32)

# Load two images
for i in range(1, 9):
    image1 = load_fits_float32(f"d:/{i}.fits")  # reference
    image2 = load_fits_float32(f"d:/{i+1}.fits")  # shifted

    # Estimate the pixel shift from image2 to align with image1
    shift, error, _ = phase_cross_correlation(image1, image2, upsample_factor=100)

    print(f"{i} -> {i+1}: Estimated shift: dy={shift[0]*pixel_scale:.2f}, dx={shift[1]*pixel_scale:.2f} (arcsec)")
    # print(f"{i} -> {i+1}: Subpixel registration error: {error:.4f}")

# Optionally apply the shift to align image2
# image2_aligned = ndi_shift(image2, shift)

# # Plot before and after alignment
# fig, axes = plt.subplots(1, 3, figsize=(15, 5))
# axes[0].imshow(image1, cmap='gray')
# axes[0].set_title("Reference Image")
# axes[1].imshow(image2, cmap='gray')
# axes[1].set_title("Unaligned Image")
# axes[2].imshow(image2_aligned, cmap='gray')
# axes[2].set_title("Aligned Image2")
# plt.tight_layout()
# plt.show()
