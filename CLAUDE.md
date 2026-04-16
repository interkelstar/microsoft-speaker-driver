# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A userspace driver that makes the **Microsoft Modern USB-C Speaker** (VID/PID `045e:083e`, kernel name "Generic Modern USB-C Speaker") fully functional on Linux. Single Python asyncio daemon handles all 6 hardware buttons:

| Button | Mechanism | Action |
|---|---|---|
| Volume Up / Down | evdev → configurable command | PCM volume ±5% |
| Mute | evdev → kernel handles ALSA, daemon reports state | `$STATE=muted\|unmuted` passed to script |
| Phone button | hidraw HID report `\x05\x01\x00` | configurable script (tap + double-tap) |
| Teams button | evdev BTN_0 (distinguished from mute by LED capability) | configurable script |

## Architecture

```
speakerctl/
├── __main__.py       # entry point; SIGHUP → reload config
├── config.py         # loads /etc/speakerctl/config.toml (tomllib)
├── daemon.py         # asyncio supervisor; handles unplug/replug via pyudev
├── discovery.py      # finds device nodes by VID/PID in sysfs
├── evdev_watcher.py  # reads volume/mute/teams from evdev (python-evdev)
├── hidraw_watcher.py # reads phone button from hidraw
├── gesture.py        # single-tap / double-tap detection
└── executor.py       # runs shell commands via asyncio.create_subprocess_shell
```

## Key facts

- **Python 3.11+** required (for `tomllib`)
- **Dependencies**: `evdev`, `pyudev` (pip, installed in venv by install.sh)
- Device discovery by **VID/PID** (`045e:083e`), never by name string
- Teams vs mute BTN_0 distinguished by LED capability in sysfs
- Mute button: kernel syncs ALSA state; daemon reads post-toggle state and passes `$STATE` to command
- Unplug: watcher raises OSError → supervisor waits for udev reconnect → restarts watchers
- Runs as dedicated system user `speakerctl` (groups: `input`, `plugdev`, `audio`) — never root
- All button actions are shell commands configured in `/etc/speakerctl/config.toml`
- Example scripts in `examples/` are installed to `/etc/speakerctl/scripts/`

## Installation

```bash
sudo ./install.sh [--with-pulseaudio] [--user USERNAME]
```

## Config

Edit `/etc/speakerctl/config.toml`, then `sudo systemctl reload speakerctl`.

## Logs

```bash
journalctl -u speakerctl -f
```
