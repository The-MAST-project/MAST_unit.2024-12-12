import numpy as np
import os
from scipy.interpolate import CubicSpline
from astropy.io import fits
import argparse


def main(filepath, fiber_px=4041, plate_scale_arcsec_px=206265 * 2.3 / 1800000, shift=0):
    # Open FITS file to get CCD width
    with fits.open(filepath) as hd:
        ccd_width_px = hd[0].header["NAXIS1"]
        image_data = hd[0].data

    def fiffa_shadow_at_col(w_arr):
        # Load data
        folder = os.path.dirname(__file__)
        a = np.fromfile(os.path.join(folder, "MAST_BFD17_ASI1600_GuideMode.dat"), sep=" ")
        y = np.fromfile(os.path.join(folder, "MAST_BFD17_ASI1600_YCord.dat"), sep=" ")

        # Process data
        w_vec0 = np.unique(y)
        eta_vec0 = [np.mean(a[y == w0]) for w0 in w_vec0]
        w_vec_deg = (w_arr - fiber_px - shift) * plate_scale_arcsec_px / 3600

        # Interpolate transmission
        cs = CubicSpline(w_vec0, eta_vec0)
        eta_vec = cs(w_vec_deg) / 100
        eta_vec[eta_vec > 1] = 1
        eta_vec[eta_vec < 0] = 0

        return np.flip(eta_vec)

    def add_fiffa_shadow(image):
        # Compute shadow and apply it to the image
        columns = np.arange(0, ccd_width_px, 1)
        fiffa_shadow = np.diag(fiffa_shadow_at_col(columns))
        image = image @ fiffa_shadow
        image = image + 100  # Add a baseline
        return image

    # Apply shadow to the image
    processed_image = add_fiffa_shadow(image_data)

    # Save the processed image with a new filename
    base, ext = os.path.splitext(filepath)
    output_filepath = f"{base}_vig{ext}"
    fits.writeto(output_filepath, processed_image, overwrite=True)
    print(f"Processed image saved to {output_filepath}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Apply FIFFA shadow correction to an image.")
    parser.add_argument("filepath", type=str, help="Path to the FITS file.")
    parser.add_argument("--fiber_px", type=int, default=4041, help="Fiber pixel value (default: 4041).")
    parser.add_argument(
        "--plate_scale_arcsec_px",
        type=float,
        default=206265 * 2.3 / 1800000,
        help="Plate scale in arcseconds per pixel (default: 206265 * 2.3 / 1800000).",
    )
    parser.add_argument(
        "--shift",
        type=int,
        default=0,
        help="shift fiber pixel value left by this value",
    )

    args = parser.parse_args()
    main(
        args.filepath,
        fiber_px=args.fiber_px,
        plate_scale_arcsec_px=args.plate_scale_arcsec_px,
        shift=args.shift,
    )
    """
    example usage in another python script- filepath input mandatory, fiber_px and plate_scale_arcsec_px optional.
    
    import vignetting as vig
    vig.main(path.join('.','images','image.fits'), fiber_px = 4041, plate_scale_arcsec_px = 0.25)

    the vignetted image will be saved in the same directory as the input image with 
      the filename appended with '_vig.fits'
    """
