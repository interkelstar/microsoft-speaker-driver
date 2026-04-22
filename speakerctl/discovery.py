"""
Discover Microsoft Modern USB-C Speaker device nodes by VID/PID.
Never uses the device name string — VID/PID is the stable identifier.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

_LOG = logging.getLogger(__name__)

VENDOR_ID = "045e"
PRODUCT_ID = "083e"


@dataclass
class DeviceSet:
    hidraw: str | None = None          # /dev/hidrawN — phone + teams buttons
    volume_evdev: str | None = None    # Consumer Control — KEY_VOLUMEUP/DOWN
    mute_evdev: str | None = None      # LED interface — KEY_MICMUTE


def _read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def _has_led(input_dir: Path) -> bool:
    led_caps = _read(input_dir / "capabilities" / "led")
    return bool(led_caps) and led_caps != "0"


def _has_key(input_dir: Path, key_bit: int) -> bool:
    key_caps = _read(input_dir / "capabilities" / "key")
    if not key_caps:
        return False
    words = [int(w, 16) for w in key_caps.split()]
    words.reverse()
    word_idx = key_bit // 64
    bit_idx = key_bit % 64
    if word_idx >= len(words):
        return False
    return bool(words[word_idx] & (1 << bit_idx))


KEY_VOLUMEUP = 115
KEY_MICMUTE = 248


def discover(vid: str = VENDOR_ID, pid: str = PRODUCT_ID) -> DeviceSet:
    """Scan /sys/bus/usb/devices for the speaker and return its device nodes."""
    result = DeviceSet()

    usb_root = Path("/sys/bus/usb/devices")
    if not usb_root.exists():
        _LOG.warning("sysfs not available at %s", usb_root)
        return result

    for device_dir in usb_root.iterdir():
        vendor_file = device_dir / "idVendor"
        product_file = device_dir / "idProduct"
        if not vendor_file.exists():
            continue
        if _read(vendor_file) != vid or _read(product_file) != pid:
            continue

        _LOG.debug("Found USB device at %s", device_dir)

        for root, dirs, files in os.walk(device_dir):
            root_path = Path(root)

            if root_path.name.startswith("hidraw") and "dev" in files:
                candidate = f"/dev/{root_path.name}"
                if os.path.exists(candidate):
                    result.hidraw = candidate
                    _LOG.debug("hidraw: %s", candidate)

            if root_path.name.startswith("input") and (root_path / "capabilities").exists():
                for child in root_path.iterdir():
                    if not child.name.startswith("event"):
                        continue
                    evdev_path = f"/dev/input/{child.name}"
                    if not os.path.exists(evdev_path):
                        continue

                    if _has_key(root_path, KEY_VOLUMEUP):
                        result.volume_evdev = evdev_path
                        _LOG.debug("volume evdev: %s", evdev_path)
                    elif _has_led(root_path) and _has_key(root_path, KEY_MICMUTE):
                        result.mute_evdev = evdev_path
                        _LOG.debug("mute evdev: %s", evdev_path)

    return result
