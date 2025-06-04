# API
* `camera`: add settings for:
  * binning
  * gain
  * temperature set-point
  * turn cooler on/off
* `save_image`:
  * option for type of file, e.g. `fits`.
  * how to know when image was successfully saved.  (_Suggestion_: new `Saved` event, including full path of saved image)
* `set_connected`:
  * besides `0` or `1`, list of equipment(s) to be `connected`/`disconnected`, e.g. `['mount', 'camera']`