# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A userspace driver that makes the **Microsoft Modern USB-C Speaker** (VID/PID `045e:083e`, kernel name "Generic Modern USB-C Speaker") fully functional on Linux. Single Python asyncio daemon handles all 6 hardware buttons:

| Button | Source | Action |
|---|---|---|
| Volume Up / Down | evdev (`KEY_VOLUMEUP` / `KEY_VOLUMEDOWN`) | `amixer -M set PCM ±5%` |
| Mute | evdev (`KEY_MICMUTE`) — kernel toggles ALSA, daemon reports state | `$STATE=muted\|unmuted` passed to script |
| Phone | hidraw report `\x05\x01\x00` | configurable script |
| Teams | hidraw report `\x9b\x01` | configurable script |

Phone and Teams come through hidraw because evdev drops `BTN_0` events unreliably.

## Architecture

```
speakerctl/
├── __main__.py       # entry point; SIGHUP → reload config
├── config.py         # loads /etc/speakerctl/config.toml (tomllib)
├── daemon.py         # asyncio supervisor; handles unplug/replug via pyudev
├── discovery.py      # finds device nodes by VID/PID in sysfs
├── evdev_watcher.py  # volume + mute
├── hidraw_watcher.py # phone + teams
└── executor.py       # runs shell commands via asyncio.create_subprocess_shell
```

## Key facts

- **Python 3.11+** required (for `tomllib`)
- **Dependencies**: `evdev`, `pyudev` (installed in venv by install.sh)
- Device discovery by **VID/PID** (`045e:083e`), never by name string
- Mute/volume evdev nodes distinguished by LED capability + key bits
- Unplug: watcher raises `OSError` → supervisor waits for udev reconnect → restarts watchers
- Runs as dedicated system user `speakerctl` (groups: `input`, `plugdev`, `audio`) — never root
- All button actions are shell commands configured in `/etc/speakerctl/config.toml`
- Example scripts in `examples/` are installed to `/etc/speakerctl/scripts/`
- Startup volumes optionally applied to both ALSA (`amixer -M`) and PulseAudio (`pactl` via sudoers, when `[startup] pulse_user` is set)

## Installation

```bash
sudo ./install.sh
```

## Config

Edit `/etc/speakerctl/config.toml`, then `sudo systemctl reload speakerctl`.

## Logs

```bash
journalctl -u speakerctl -f
```
