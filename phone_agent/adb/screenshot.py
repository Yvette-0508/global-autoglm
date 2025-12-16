"""Screenshot utilities for capturing Android device screen."""

import base64
import os
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from io import BytesIO
from typing import Tuple

from PIL import Image


@dataclass
class Screenshot:
    """Represents a captured screenshot."""

    base64_data: str
    width: int
    height: int
    is_sensitive: bool = False


def get_screenshot(device_id: str | None = None, timeout: int = 10) -> Screenshot:
    """
    Capture a screenshot from the connected Android device.

    Args:
        device_id: Optional ADB device ID for multi-device setups.
        timeout: Timeout in seconds for screenshot operations.

    Returns:
        Screenshot object containing base64 data and dimensions.

    Note:
        If the screenshot fails (e.g., on sensitive screens like payment pages),
        a black fallback image is returned with is_sensitive=True.
    """
    adb_prefix = _get_adb_prefix(device_id)

    # Fast path: stream PNG bytes via exec-out (avoids /sdcard write + pull).
    # This is typically much faster, especially when controlling multiple devices.
    try:
        result = subprocess.run(
            adb_prefix + ["exec-out", "screencap", "-p"],
            capture_output=True,
            timeout=timeout,
        )

        if result.returncode == 0 and result.stdout:
            try:
                img = Image.open(BytesIO(result.stdout))
                width, height = img.size
                buffered = BytesIO()
                img.save(buffered, format="PNG")
                base64_data = base64.b64encode(buffered.getvalue()).decode("utf-8")
                return Screenshot(
                    base64_data=base64_data,
                    width=width,
                    height=height,
                    is_sensitive=False,
                )
            except Exception:
                # Fall through to legacy path below
                pass
    except Exception:
        # Fall through to legacy path below
        pass

    # Legacy fallback: save to /sdcard then pull.
    temp_path = os.path.join(tempfile.gettempdir(), f"screenshot_{uuid.uuid4()}.png")
    try:
        result = subprocess.run(
            adb_prefix + ["shell", "screencap", "-p", "/sdcard/tmp.png"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        output = (result.stdout or "") + (result.stderr or "")
        if "Status: -1" in output or "Failed" in output:
            return _create_fallback_screenshot(is_sensitive=True)

        subprocess.run(
            adb_prefix + ["pull", "/sdcard/tmp.png", temp_path],
            capture_output=True,
            text=True,
            timeout=5,
        )

        if not os.path.exists(temp_path):
            return _create_fallback_screenshot(is_sensitive=False)

        img = Image.open(temp_path)
        width, height = img.size

        buffered = BytesIO()
        img.save(buffered, format="PNG")
        base64_data = base64.b64encode(buffered.getvalue()).decode("utf-8")

        try:
            os.remove(temp_path)
        except OSError:
            pass

        return Screenshot(
            base64_data=base64_data, width=width, height=height, is_sensitive=False
        )
    except Exception as e:
        print(f"Screenshot error: {e}")
        return _create_fallback_screenshot(is_sensitive=False)


def _get_adb_prefix(device_id: str | None) -> list:
    """Get ADB command prefix with optional device specifier."""
    if device_id:
        return ["adb", "-s", device_id]
    return ["adb"]


def _create_fallback_screenshot(is_sensitive: bool) -> Screenshot:
    """Create a black fallback image when screenshot fails."""
    default_width, default_height = 1080, 2400

    black_img = Image.new("RGB", (default_width, default_height), color="black")
    buffered = BytesIO()
    black_img.save(buffered, format="PNG")
    base64_data = base64.b64encode(buffered.getvalue()).decode("utf-8")

    return Screenshot(
        base64_data=base64_data,
        width=default_width,
        height=default_height,
        is_sensitive=is_sensitive,
    )
