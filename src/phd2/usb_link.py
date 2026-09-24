"""
The USB link the guide camera negotiated, read from the Windows PnP device tree.

PHD2 holds the camera, so the ZWO SDK's ``IsUSB3Host`` -- which needs the camera open --
cannot be asked from the unit process. The camera's parent chain answers the same question
without touching the device: a USB 2.0 hub anywhere between the camera and the host
controller caps the link at High-Speed, since such a hub cannot pass SuperSpeed.

A camera plugged straight into a root port has no hub to read, and reports ``Unknown``.
"""

import subprocess
import sys

from common.mast_logging import get_logger
from common.models.statuses import UsbLink
from common.utils import function_name

logger = get_logger(__name__)

USB2_HUB = "USB2.0 Hub"
USB3_HUB = "USB3.0 Hub"
# PowerShell's own start-up dominates; the walk itself is a handful of property reads.
WALK_TIMEOUT_S = 30

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
