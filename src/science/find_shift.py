# !/usr/bin/env python3
"""
Created on Sun Oct 27 16:51:56 2024

@author: Oyaron
"""

from astropy.io import fits
from skimage.registration import phase_cross_correlation


def trim_fits(input_fits_path, x_dim, y_dim):
    # Open the input FITS file
    with fits.open(input_fits_path) as hdul:
        # Access the primary data (assuming it's in the primary HDU, index 0)
        data = hdul[0].data

        center_x = data.shape[1] // 2  # Width / 2
        center_y = data.shape[0] // 2  # Height / 2

        x_start, x_end = center_x - x_dim, center_x + x_dim  # Column range (horizontal)
        y_start, y_end = center_y - y_dim, center_y + y_dim  # Row range (vertical)

        print(f"Central (x, y) = ({center_x}, {center_y}); trim-x: {x_start}-{x_end}; trim-y: {y_start}-{y_end}")

        # Trim the data array
        trimmed_data = data[y_start:y_end, x_start:x_end]

        # Update the data in the primary HDU
        hdul[0].data = trimmed_data

        # Save the trimmed data to a new FITS file
        hdul.writeto(input_fits_path, overwrite=True)

    print(f"Trimmed FITS file saved as {input_fits_path}")


def load_fits_data(file_list):
    data = []
    obsdate = []
    for file in file_list:
        with fits.open(file) as hdul:
            # data.append(hdul[0].data[y_start:y_end, x_start:x_end])
            data.append(hdul[0].data)
            # Getting obsdate from header
            header = hdul[0].header
            obsdate.append(header.get("DATE-OBS", "Keyword not found"))
    return data, obsdate


def find_shift_reg(image1, image2):
    # Use register_translation to find the shift between two images
    # (high upsample_factor value for obtaining sub-pixel accuracy)
    shift, error, diffphase = phase_cross_correlation(image1, image2, upsample_factor=100)
    # error is always 1.0 for some reason
    return shift, error, diffphase


########################
# Main

image1 = "sky-last.fits"
image2 = "spec-first.fits"

with fits.open(image1) as hdul:
    data = hdul[0].data
    dimensions = data.shape

# Define the dimensions (half) of the required trimmed image around the center
y_dim = dimensions[0] // 2
x_dim = dimensions[1] // 2

trim_fits(image2, x_dim, y_dim)

[data, obsdate] = load_fits_data([image1, image2])
reference_image = data[0]

for image in data[1:]:
    [shift, error, diffphase] = find_shift_reg(reference_image, image)
    print("Shift (dy,dx): ", shift, " error: ", error, " Diffphase", diffphase)
