# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A userspace driver that makes the **Microsoft Modern USB-C Speaker** (VID/PID `045e:083e`, kernel name "Generic Modern USB-C Speaker") fully functional on Linux. Single Python asyncio daemon handles all 6 hardware buttons:

| Button | Source | Action |
|---|---|---|
| Volume Up / Down | evdev (`KEY_VOLUMEUP` / `KEY_VOLUMEDOWN`) | ask the assistant to step its volume; `amixer -M set PCM ±5%` only when it is not there |
| Mute | evdev (`KEY_MICMUTE`) — firmware mutes, daemon measures the result | `$STATE=muted\|unmuted` passed to script |
| Phone | hidraw report `\x05\x01\x00` | configurable script |
| Teams | hidraw report `\x9b\x01` | mid-answer: stop the assistant; otherwise the configured script |

Phone and Teams come through hidraw because evdev drops `BTN_0` events unreliably.

A button with `interrupts_playback = true` has two meanings, chosen by what the
speaker is doing. Quiet, the press runs `command` as always — here that opens a
conversation through a Home Assistant webhook. Mid-answer, or over a ringing
timer, it sends `stop_pipeline` instead, which is the same `satellite.stop()`
the stop word calls. Without the split, pressing it during an answer ended the
answer and immediately asked what it could help with, which is not what someone
reaching for a button mid-sentence wants. It gives the user a second way to
interrupt that does not have to be heard over the speaker — worth having, since
the stop word measures 0.36–0.99 against a 0.3 threshold and takes ~1.5 s.

Playback state comes from `tts_speaking` / `timer_ringing` against
`tts_finished` / `idle` / `pipeline_error`, and is assumed quiet on connect —
the snapshot does not carry it, so reconnecting mid-answer costs one press.
`journalctl -u speakerctl | grep "assistant is"` shows the transitions.

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
  a live room has measured as low as 39.
- **That measurement is no longer trusted on connect, and must not be again**
  (2026-08-09). It cannot fail: silence and a source that is not delivering are
  the same reading, and it answers "muted" to both. One night it ran a second
  after the assistant's container restarted, called a live microphone muted,
  and told the assistant to mute it — the microphone had been writing clips
  peaking at 1182 the second before, and the button's LED was dark, so both the
  audio and the hardware disagreed with it. The assistant then dropped a wake
  word it went on recognising at 0.996 for eleven hours. `_reconcile()` now
  *forces* the microphone on at the start of a session instead of asking, and
  tracks it from button presses after that. The errors are not symmetric:
  wrongly opening a microphone costs one press of a button the user is next to,
  wrongly closing one costs a speaker that is deaf with no symptom but silence.
  The probe survives only in the button handler, where it runs right after a
  press and the difference it is reading is 1 against 2400.
- **Never mirror that measurement back into `Headset`.** It self-locks: the next
  press clears the firmware mute, `nocap` remains, the probe still hears silence,
  and the daemon writes `nocap` again — muted forever.
- **The microphone does not hear the speaker, and that is the device's doing.**
  Measured during playback: the speaker's monitor at -25 dBFS while the raw
  microphone sits at -85 dBFS, and a person talking in the room at the same
  moment comes through at -28 dBFS. So it subtracts its own output and passes
  everything else. Two consequences worth knowing: the host's
  `module-echo-cancel` has almost nothing left to remove (it measures ~0 dB of
  cancellation, which is not a fault), and **the assistant cannot trigger its
  own wake or stop words** — the models see silence throughout its own speech.
- **At high playback levels the microphone floor collapses** — at a sink volume
  of 60 % it drops to about -82 dBFS during playback while the idle room floor
  was -40, and at 40 % it does not. Reproduced across several runs. What it
  does to a *loud* voice was never established, and it turned out **not** to be
  what blocked barge-in, so do not lower the volume for it: an earlier revision
  of this file claimed a firmware "gate" made interruption impossible and set
  `speaker_percent = 40` on that basis. That was wrong — see BACKLOG.md V10.
- **One volume, and it lives in the sink.** `speaker_percent` is only what the
  sink holds until the assistant connects. After that the level comes from the
  assistant — buttons, the Home Assistant slider and the assistant itself all
  move the same number, which `lva._apply_volume()` puts on the sink with
  `pactl set-sink-volume`. This *requires* the assistant to run with
  `--external-volume` (`EXTERNAL_VOLUME=1` in the stack env); without it the
  software player attenuates as well and everything is roughly a fifth as loud
  as the slider claims. An earlier arrangement had the level in the software
  player and left the sink pinned, which made the mixer and Home Assistant two
  numbers that never agreed and capped the range at whatever the sink sat on.
  Note the scales differ — PulseAudio's percentage is cubic (60 % = -13.3 dB;
  read the dB figure from `pactl`, never the percentage), ALSA's is its own
  again (`pactl` 57 % shows as `amixer` 78 %, both -14.6 dB), and mpv's volume
  is cubic too. Convert between stages through gain, never through percent.
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

- **One number, applied to the sink.** With sync on, the volume buttons send
  `volume_up`/`volume_down` rather than running `amixer`; the assistant steps
  its number, broadcasts it, and `lva._apply_volume()` writes it to the
  PulseAudio sink. So the buttons, the Home Assistant slider and `alsamixer`
  all show the same level. The assistant must run with `--external-volume`, or
  it attenuates in software as well and the two stages multiply.
- **Buttons never go dead.** If the assistant is unreachable the press falls
  back to the local mixer command.
- **Mute syncs both ways.** A session *starts* by forcing the microphone on, in
  the hardware (`Headset cap`, which clears a firmware mute and the LED) and in
  the assistant, and the daemon tracks it from button presses from there. A
  reconnect only repeats what it already knows, so muting by hand survives the
  assistant's container being rebuilt underneath it — but a reboot opens the
  microphone again, deliberately. See `_reconcile()` and the note above.
- **A button can interrupt.** `interrupts_playback` on any `[button]` section
  makes a press send `stop_pipeline` while the assistant is speaking, and run
  its normal command otherwise. Used for Teams; see the top of this file.
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
the forced unmute that starts a session, that connecting never probes for the
mute state, that a hand-muted microphone survives a reconnect, the `nocap`
token, reconnect, the offline fallback, degrading without `websockets`, and
that a volume from the assistant reaches the sink — including the no-op on a
repeat and the silence when no `pulse_user` is configured. The stubbed
`read_mute_state` returns "muted", so any code that starts consulting it on
connect again fails the suite loudly. The hardware paths (the probe itself,
evdev reading) are not covered — those were verified on the device.

## Installation

```bash
sudo ./install.sh
```

**The code runs from `/usr/lib/speakerctl`, not from the venv.** The unit sets
`Environment=PYTHONPATH=/usr/lib/speakerctl`, and the venv at
`/usr/lib/speakerctl-venv` only supplies the interpreter and the third-party
packages. There is a stale copy of the package inside that venv's
`site-packages` from an earlier layout; copying a fix there and restarting
looks like a successful deploy and changes nothing. To patch a running host:

```bash
sudo cp *.py /usr/lib/speakerctl/speakerctl/
sudo rm -rf /usr/lib/speakerctl/speakerctl/__pycache__
sudo systemctl restart speakerctl
```

## Config

Edit `/etc/speakerctl/config.toml`, then `sudo systemctl reload speakerctl`.

## Logs

```bash
journalctl -u speakerctl -f
```
