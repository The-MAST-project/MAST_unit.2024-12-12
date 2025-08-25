#!/bin/bash

file="/cygdrive/d/tmp/seq=0001,time=22-01-59_706,seconds=5.0,binning=1x1,gain=170,roi=1000,340,7500,3000.fits"
new_file=${file/.fits/,solver=AstrometryDotNet1.fits}

ls -l ${file}

/usr/local/astrometry/bin/solve-field \
    --scale-units arcsecperpix \
    --scale-low 0.25 \
    --scale-high 0.27 \
    --ra 305.56280343323994 \
    --dec 40.2566511405156 \
    --radius 1 \
    --no-plots \
    --overwrite \
    --solved none \
    --match none \
    --rdls none \
    --corr none \
    --dir /cygdrive/d/MAST/tmp/tmp_6ZTXKIS8KPX \
    --index-file /usr/local/astrometry/data/index-5202-13.fits \
    --temp-dir /cygdrive/d/MAST/tmp/tmp_6ZTXKIS8KPX \
    --new-fits ${new_file} \
    ${file}
