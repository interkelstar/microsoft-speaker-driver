import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ButtonConfig:
    command: str
    debounce_seconds: float = 0.0
    double_tap_command: str = ""
    hold_command: str = ""
    double_tap_window_seconds: float = 0.4
    hold_threshold_seconds: float = 0.8


@dataclass
class Config:
    vid: str
    pid: str
    alsa_card: str
    volume_up: ButtonConfig
    volume_down: ButtonConfig
    mute: ButtonConfig
    teams: ButtonConfig
    phone: ButtonConfig


def load_config(path: str | Path) -> Config:
    with open(path, "rb") as f:
        data = tomllib.load(f)

    device = data.get("device", {})

    def btn(key: str) -> ButtonConfig:
        sec = data.get(key, {})
        if "command" not in sec:
            raise ValueError(f"[{key}] section missing 'command' in {path}")
        return ButtonConfig(
            command=sec["command"],
            debounce_seconds=float(sec.get("debounce_seconds", 0.0)),
            double_tap_command=sec.get("double_tap_command", ""),
            hold_command=sec.get("hold_command", ""),
            double_tap_window_seconds=float(sec.get("double_tap_window_seconds", 0.4)),
            hold_threshold_seconds=float(sec.get("hold_threshold_seconds", 0.8)),
        )

    return Config(
        vid=device.get("vid", "045e"),
        pid=device.get("pid", "083e"),
        alsa_card=device.get("alsa_card", "Speaker"),
        volume_up=btn("volume_up"),
        volume_down=btn("volume_down"),
        mute=btn("mute"),
        teams=btn("teams"),
        phone=btn("phone"),
    )
