import numpy as np
import matplotlib.pyplot as plt
from astropy.stats import SigmaClip
from astropy.io import fits
from photutils.background import Background2D, MedianBackground
import scipy.ndimage as ndi


def detect_vertical_obstruction(image_data,
                                box_size=50,
                                col_obstruction_factor=0.5,
                                umbra_th=0.3,
                                penumbra_th=0.6):
    """
    Detect a rectangular, nearly vertical obstruction in 'image_data'.
    Returns masks for umbra and penumbra.

    :param image_data: 2D numpy array of the image.
    :param box_size: Background2D box size for background estimation.
    :param col_obstruction_factor:
        Fraction of the median column flux used to decide if columns are obstructed.
    :param umbra_th: Ratio threshold for umbra (fully obstructed).
    :param penumbra_th: Ratio threshold for penumbra (partial obstruction).
    :return: (umbra_mask, penumbra_mask) boolean arrays of the same shape as image_data.
    """

    # 1. Estimate background using a robust method
    sigma_clip = SigmaClip(sigma=3.0)
    bkg_estimator = MedianBackground()
    bkg_2d = Background2D(
        image_data,
        box_size=box_size,
        filter_size=(3, 3),
        sigma_clip=sigma_clip,
        bkg_estimator=bkg_estimator
    )

    background = bkg_2d.background

    # 2. Create a ratio image: ratio < 1 => flux < background
    ratio = np.divide(image_data, background,
                      out=np.zeros_like(image_data),
                      where=(background != 0))

    # 3. Identify columns with low flux
    col_sums = ratio.sum(axis=0)  # sum over rows
    median_col_sum = np.median(col_sums)
    threshold = col_obstruction_factor * median_col_sum
    obstructed_cols = np.where(col_sums < threshold)[0]

    if obstructed_cols.size == 0:
        # No obvious obstruction detected
        return None, None

    # Get contiguous min/max of obstructed columns
    cols_min = obstructed_cols[0]
    cols_max = obstructed_cols[-1]

    # 4. Extract sub-image covering the obstructed columns
    slice_obstruction = ratio[:, cols_min:cols_max+1]

    # 5. Threshold for umbra & penumbra
    umbra_mask_sub = (slice_obstruction < umbra_th)
    penumbra_mask_sub = (slice_obstruction >= umbra_th) & (slice_obstruction < penumbra_th)

    # 6. Morphological cleaning
    structure = np.ones((3, 3), dtype=bool)
    umbra_mask_clean = ndi.binary_opening(umbra_mask_sub, structure)
    penumbra_mask_clean = ndi.binary_opening(penumbra_mask_sub, structure)

    # 7. Map these sub-masks back to full image coordinates
    #    Initialize empty (False) masks for the whole image
    umbra_mask = np.zeros_like(ratio, dtype=bool)
    penumbra_mask = np.zeros_like(ratio, dtype=bool)

    umbra_mask[:, cols_min:cols_max+1] = umbra_mask_clean
    penumbra_mask[:, cols_min:cols_max+1] = penumbra_mask_clean

    return umbra_mask, penumbra_mask


def plot_obstruction(image_data, umbra_mask, penumbra_mask, vmin=None, vmax=None):
    """
    Plot the image with overlaid contours for umbra (red) and penumbra (yellow).
    :param image_data: 2D numpy array of the image.
    :param umbra_mask: Boolean mask array for umbra.
    :param penumbra_mask: Boolean mask array for penumbra.
    :param vmin: Optional lower stretch for plt.imshow.
    :param vmax: Optional upper stretch for plt.imshow.
    """

    fig, ax = plt.subplots(figsize=(8, 6))

    # Show the image
    cax = ax.imshow(image_data, origin='lower', cmap='gray', vmin=vmin, vmax=vmax)
    fig.colorbar(cax, ax=ax, label='Pixel Value')

    # Overlay contour for umbra
    # contour(...) draws a line around regions where umbra_mask == True
    ax.contour(umbra_mask, levels=[0.5], colors='red', linewidths=1.2, label='Umbra')

    # Overlay contour for penumbra
    ax.contour(penumbra_mask, levels=[0.5], colors='yellow', linewidths=1.2, label='Penumbra')

    ax.set_title("Image with Obstruction Contours")
    ax.set_xlabel("X (columns)")
    ax.set_ylabel("Y (rows)")
    plt.show()


def main():
    with fits.open("image.fits") as hdul:
        image_data = hdul[0].data.astype(float)

    # 2) Detect the obstruction
    umbra_mask, penumbra_mask = detect_vertical_obstruction(
        image_data,
        box_size=30,
        col_obstruction_factor=0.7,  # Loosen or tighten as needed
        umbra_th=0.001,
        penumbra_th=0.01
    )

    # 3) Plot the results
    if umbra_mask is not None and penumbra_mask is not None:
        plot_obstruction(image_data, umbra_mask, penumbra_mask)
    else:
        print("No significant obstruction detected.")


if __name__ == "__main__":
    main()
