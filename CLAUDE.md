# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A userspace driver that makes the **Microsoft Modern USB-C Speaker** (VID/PID `045e:083e`, kernel name "Generic Modern USB-C Speaker") fully functional on Linux. Single Python asyncio daemon handles all 6 hardware buttons:

| Button | Source | Action |
|---|---|---|
| Volume Up / Down | evdev (`KEY_VOLUMEUP` / `KEY_VOLUMEDOWN`) | `amixer -M set PCM ±5%` |
| Mute | evdev (`KEY_MICMUTE`) — firmware mutes, daemon measures the result | `$STATE=muted\|unmuted` passed to script |
| Phone | hidraw report `\x05\x01\x00` | configurable script |
| Teams | hidraw report `\x9b\x01` | configurable script |

Phone and Teams come through hidraw because evdev drops `BTN_0` events unreliably.

## The mute button is not what it looks like (measured 2026-08-06)

Every obvious assumption about it is wrong, so do not "simplify" this back:

- **The speaker mutes in its own firmware.** Nothing on the host reflects that.
  The `Headset` capture switch reads `[on]` on *both* sides of a transition, no
  other mixer control moves, evdev's `LED_MUTE` stays inactive, and the HID
  reports encode "button pressed", not "now muted".
- **`Headset` is write-authoritative but read-blind.** Writing `nocap` really
  silences the mic (peak 0) and lights the LED; writing `cap` really unsilences
  it *and clears a firmware mute, LED included*. Reading it tells you nothing.
  It is also a **capture** switch — the tokens are `cap`/`nocap`; `amixer set
  Headset mute` exits 1.
- **So state is measured, not read**: `read_mute_state()` records ~1 s of audio
  and calls it muted below a peak of 8. Muted reads 1 (0 under an ALSA mute);
  live in a quiet room never measured below 50.
- **Never mirror that measurement back into `Headset`.** It self-locks: the next
  press clears the firmware mute, `nocap` remains, the probe still hears silence,
  and the daemon writes `nocap` again — muted forever.
- **evdev drops events.** `async_read_loop` raises `InvalidStateError` when a
  batch lands before the previous one is consumed, and *loses that event* — a
  mute press was observed lighting the LED, silencing the mic, arriving on
  hidraw, and leaving no evdev trace at all. `_events()` reads the descriptor
  with `add_reader` instead, like `hidraw_watcher` already did.

## Architecture

```
speakerctl/
├── __main__.py       # entry point; SIGHUP → reload config
├── config.py         # loads /etc/speakerctl/config.toml (tomllib)
├── daemon.py         # asyncio supervisor; handles unplug/replug via pyudev
├── discovery.py      # finds device nodes by VID/PID in sysfs
├── evdev_watcher.py  # volume + mute
├── hidraw_watcher.py # phone + teams
├── aec.py            # loads module-echo-cancel into the desktop user's session
├── lva.py            # peripheral-API client: volume/mute sync with the assistant
└── executor.py       # runs shell commands via asyncio.create_subprocess_shell
```

## Voice assistant sync (`[lva]`, off by default)

`lva.py` holds a WebSocket to a local **linux-voice-assistant**'s peripheral API
(`ws://127.0.0.1:6055`), so the buttons and the assistant share one idea of
volume and mute.

- **Volume is owned by the assistant, not the mixer.** With sync on, the volume
  buttons send `volume_up`/`volume_down` instead of running `amixer`. Doing both
  would attenuate twice, and the hardware half would also rescale what the
  speaker emits *after* `module-echo-cancel` taps its playback reference,
  forcing the adaptive filter to reconverge on every press.
- **Buttons never go dead.** If the assistant is unreachable the press falls
  back to the local mixer command.
- **Mute syncs both ways**, but hardware wins on connect: a stale remote "not
  muted" must never silently reopen a microphone the user muted by hand.
- The `Headset` control is a **capture** switch (`cswitch`, no `pswitch`) —
  the tokens are `cap`/`nocap`; `amixer set Headset mute` exits 1.
- The peripheral API has **no authentication**. Run the assistant with
  `--peripheral-host 127.0.0.1` so it is not an open control surface on the LAN.

## Key facts

- **Python 3.11+** required (for `tomllib`)
- **Dependencies**: `evdev`, `pyudev`, `websockets` (installed in venv by install.sh).
  `websockets` is imported lazily — an older venv without it disables `[lva]`
  sync and logs why, rather than crash-looping the daemon.
- Device discovery by **VID/PID** (`045e:083e`), never by name string
- Mute/volume evdev nodes distinguished by LED capability + key bits
- Unplug: watcher raises `OSError` → supervisor waits for udev reconnect → restarts watchers
- Runs as dedicated system user `speakerctl` (groups: `input`, `plugdev`, `audio`) — never root
- All button actions are shell commands configured in `/etc/speakerctl/config.toml`
- Example scripts in `examples/` are installed to `/etc/speakerctl/scripts/`
- Startup volumes optionally applied to both ALSA (`amixer -M`) and PulseAudio (`pactl` via sudoers, when `[startup] pulse_user` is set)

## Tests

```bash
python3 tests/test_lva.py     # needs `websockets`; no hardware, no assistant
```

Covers the LVA client against a stand-in peripheral API server: the echo guard,
one-way connect reconciliation, the `nocap` token, reconnect, the offline
fallback, and degrading without `websockets`. The hardware paths (mute probe,
evdev reading) are not covered — those were verified on the device.

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
