import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ButtonConfig:
    command: str
    debounce_seconds: float = 0.0


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
    startup_speaker_percent: int | None = None
    startup_mic_percent: int | None = None
    startup_pulse_user: str | None = None


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
        )

    startup = data.get("startup", {})

    return Config(
        vid=device.get("vid", "045e"),
        pid=device.get("pid", "083e"),
        alsa_card=device.get("alsa_card", "Speaker"),
        volume_up=btn("volume_up"),
        volume_down=btn("volume_down"),
        mute=btn("mute"),
        teams=btn("teams"),
        phone=btn("phone"),
        startup_speaker_percent=startup.get("speaker_percent"),
        startup_mic_percent=startup.get("mic_percent"),
        startup_pulse_user=startup.get("pulse_user"),
    )
