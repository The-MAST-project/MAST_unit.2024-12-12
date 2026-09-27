"""
The USB link the guide camera negotiated, read from the Windows PnP device tree.

PHD2 holds the camera, so the ZWO SDK's ``IsUSB3Host`` -- which needs the camera open --
cannot be asked from the unit process. The camera's parent chain answers the same question
without touching the device: a USB 2.0 hub anywhere between the camera and the host
controller caps the link at High-Speed, since such a hub cannot pass SuperSpeed.

A camera plugged straight into a root port has no hub to read, and reports ``Unknown``.

``Imager.startup()`` calls ``log_usb_link()`` once a session, before delegating to its backend.

The verdict is logged, once a session, on one line that a scraper can key on::

    DEGRADED usb-link=HighSpeed chain="<hop> > <hop> > ..." <what it costs>   (WARNING)
    usb-link=SuperSpeed chain="<hop> > ..."                                   (INFO)

``usb-link=`` appears on every verdict, so the latest one per unit says whether it is still
degraded; ``DEGRADED`` appears only on a degraded one, and is the tag any other degradation
should reuse.
"""

import subprocess
import sys
from enum import StrEnum

from common.mast_logging import get_logger
from common.utils import function_name

logger = get_logger(__name__)

DEGRADED_TAG = "DEGRADED"
USB_LINK_KEY = "usb-link"
CHAIN_SEPARATOR = " > "
USB2_HUB = "USB2.0 Hub"
USB3_HUB = "USB3.0 Hub"
# PowerShell's own start-up dominates; the walk itself is a handful of property reads.
WALK_TIMEOUT_S = 30


class UsbLink(StrEnum):
    SuperSpeed = "SuperSpeed"
    HighSpeed = "HighSpeed"
    Unknown = "unknown"


# One line per ancestor, nearest first, up to and including the root hub:
# "<FriendlyName> | <bus-reported description>". Prints nothing when no ASI camera is present.
_WALK_PARENT_CHAIN = r"""
$ProgressPreference = 'SilentlyContinue'
$cam = Get-PnpDevice -PresentOnly | Where-Object { $_.FriendlyName -match 'ASI\d+' } | Select-Object -First 1
if (-not $cam) { exit 0 }
$id = $cam.InstanceId
for ($i = 0; $i -lt 5; $i++) {
  $p = (Get-PnpDeviceProperty -InstanceId $id -KeyName 'DEVPKEY_Device_Parent').Data
  if (-not $p) { break }
  $name = (Get-PnpDevice -InstanceId $p).FriendlyName
  $bus = (Get-PnpDeviceProperty -InstanceId $p -KeyName 'DEVPKEY_Device_BusReportedDeviceDesc').Data
  '{0} | {1}' -f $name, $bus
  if ($name -match 'Root Hub') { break }
  $id = $p
}
"""


def classify(chain: list[str] | None) -> UsbLink:
    if not chain:
        return UsbLink.Unknown
    if any(USB2_HUB in hop for hop in chain):
        return UsbLink.HighSpeed
    if any(USB3_HUB in hop for hop in chain):
        return UsbLink.SuperSpeed
    return UsbLink.Unknown


def read_parent_chain() -> list[str] | None:
    """The camera's ancestors, nearest first; ``None`` off Windows, with no camera, or on failure."""
    if sys.platform != "win32":
        return None
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _WALK_PARENT_CHAIN],
            capture_output=True,
            text=True,
            check=True,
            timeout=WALK_TIMEOUT_S,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as ex:
        logger.warning(f"{function_name()}: could not read the guide camera's USB parent chain: {ex!r}")
        return None
    chain = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return chain or None


def log_usb_link() -> UsbLink:
    """Find the guide camera's link and log the verdict; a High-Speed link is logged as degraded."""
    chain = read_parent_chain()
    link = classify(chain)
    verdict = f'{USB_LINK_KEY}={link} chain="{CHAIN_SEPARATOR.join(chain or ())}"'
    if link is UsbLink.HighSpeed:
        logger.warning(f"{DEGRADED_TAG} {verdict} guide-camera frames read out several times slower than over SuperSpeed")
    else:
        logger.info(verdict)
    return link
