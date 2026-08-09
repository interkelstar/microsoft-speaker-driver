"""
Behavioural test for speakerctl's LVA peripheral client.

Run it with a python that has `websockets`:  python3 tests/test_lva.py
Needs no hardware and no running assistant — it starts a stand-in peripheral
API server on 127.0.0.1:6155 and stubs evdev.

Runs a stand-in peripheral API server that speaks the real protocol, and
checks the things that would be expensive to get wrong on live hardware:
the echo guard, the direction of connect-time reconciliation, and that the
volume buttons stay usable when the assistant is not there.
"""
import asyncio
import json
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_stub = types.ModuleType("evdev")
_stub.ecodes = types.SimpleNamespace(
    EV_KEY=1, KEY_VOLUMEUP=115, KEY_VOLUMEDOWN=114, KEY_MICMUTE=248
)
_stub.InputDevice = object
sys.modules.setdefault("evdev", _stub)

from websockets.asyncio.server import serve  # noqa: E402

from speakerctl import evdev_watcher, executor, hidraw_watcher, lva, pulse  # noqa: E402
from speakerctl.config import ButtonConfig, Config  # noqa: E402

PORT = 6155
FAILS = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  — ' + detail if detail else ''}")
    if not cond:
        FAILS.append(name)


def make_config(**kw):
    b = ButtonConfig(command="true")
    return Config(
        vid="045e", pid="083e", alsa_card="Speaker",
        volume_up=ButtonConfig(command="echo LOCAL_UP"),
        volume_down=ButtonConfig(command="echo LOCAL_DOWN"),
        mute=b, teams=b, phone=b,
        lva_enabled=True, lva_url=f"ws://127.0.0.1:{PORT}", **kw
    )


async def main():
    lva._RECONNECT_SECONDS = 0.2

    ran = []
    received = []
    conns = []

    async def real_run(cmd, extra_env=None):
        ran.append(cmd)
        return 0
    executor.run = real_run

    # Records every attempt to guess the mute state from the audio. Connecting
    # must never do this: the probe cannot tell a silent room from a source
    # that is not delivering, and one wrong "muted" left the speaker deaf for
    # eleven hours. Returning True makes a regression loud rather than subtle.
    probed = []

    async def fake_read(config):
        probed.append(config)
        return True
    evdev_watcher.read_mute_state = fake_read

    # There is no "tester" login here and no PulseAudio session behind it. The
    # question under test is what the client decides to run, not whether pwd
    # can resolve the user.
    pulse.socket_ready = lambda user: True

    async def handler(ws):
        conns.append(ws)
        await ws.send(json.dumps({"event": "snapshot", "data": {
            "muted": True, "volume": 0.8, "ha_connected": True}}))
        try:
            async for raw in ws:
                received.append(json.loads(raw))
        except Exception:
            pass

    async with serve(handler, "127.0.0.1", PORT):
        client = lva.LVAClient(make_config())
        task = asyncio.create_task(client.run())
        await asyncio.sleep(0.6)

        check("connects", client.connected)
        check("forces the microphone on at the start of a session",
              received and received[0]["command"] == "unmute_mic",
              str(received[:1]))
        check("clears the firmware mute in the hardware as well",
              ran == ["amixer -c Speaker set Headset cap"], f"ran={ran}")
        check("never guesses the mute state from the audio",
              probed == [], f"probed={len(probed)}")
        check("does NOT apply a stale snapshot mute to the microphone",
              not any("nocap" in c for c in ran), f"ran={ran}")

        ran.clear()
        await conns[0].send(json.dumps({"event": "muted", "data": {"muted": False}}))
        await asyncio.sleep(0.2)
        check("ignores the echo of its own report", ran == [], f"ran={ran}")

        await conns[0].send(json.dumps({"event": "muted", "data": {"muted": True}}))
        await asyncio.sleep(0.3)
        check("applies a real remote mute", len(ran) == 1, f"ran={ran}")
        check("uses the capture-switch token",
              ran and ran[0].endswith("set Headset nocap"), str(ran))

        await conns[0].send(json.dumps({"event": "muted", "data": {"muted": True}}))
        await asyncio.sleep(0.2)
        check("repeat of the same state is a no-op", len(ran) == 1, f"ran={ran}")

        received.clear()
        await client.volume_step(up=True)
        await asyncio.sleep(0.2)
        check("volume button asks the assistant to step",
              received and received[-1]["command"] == "volume_up", str(received))

        # ── the level lands on the system mixer ─────────────────────────────
        # Only when a PulseAudio user is configured: without one there is no
        # session to talk to, and the assistant's software volume is still in
        # charge, so touching the sink would attenuate twice.
        ran.clear()
        vol_client = lva.LVAClient(make_config(startup_pulse_user="tester"))
        vol_task = asyncio.create_task(vol_client.run())
        await asyncio.sleep(0.6)
        check("applies the snapshot volume to the sink",
              any("set-sink-volume" in c and " 80%" in c for c in ran), f"ran={ran}")

        ran.clear()
        await conns[-1].send(json.dumps(
            {"event": "volume_changed", "data": {"volume": 0.35}}))
        await asyncio.sleep(0.3)
        check("follows a volume change to the sink",
              any("set-sink-volume" in c and " 35%" in c for c in ran), f"ran={ran}")

        ran.clear()
        await conns[-1].send(json.dumps(
            {"event": "volume_changed", "data": {"volume": 0.35}}))
        await asyncio.sleep(0.2)
        check("same volume again is a no-op", ran == [], f"ran={ran}")

        ran.clear()
        await conns[-1].send(json.dumps(
            {"event": "volume_changed", "data": {"volume": None}}))
        await asyncio.sleep(0.2)
        check("a malformed volume is ignored, not crashed on",
              ran == [] and vol_client.connected, f"ran={ran}")

        vol_task.cancel()
        try:
            await vol_task
        except asyncio.CancelledError:
            pass

        ran.clear()
        no_pulse = lva.LVAClient(make_config())
        await no_pulse._apply_volume(0.4)
        check("leaves the sink alone with no PulseAudio user", ran == [], f"ran={ran}")

        # ── the Teams button means two things ───────────────────────────────
        teams = make_config().teams
        teams.interrupts_playback = True
        teams.command = "echo TEAMS_NORMAL"

        received.clear()
        ran.clear()
        await hidraw_watcher.handle_press("teams", teams, client)
        check("quiet: the button does its configured job",
              ran == ["echo TEAMS_NORMAL"] and not received, f"ran={ran}")

        await conns[0].send(json.dumps({"event": "tts_speaking", "data": {}}))
        await asyncio.sleep(0.2)
        received.clear()
        ran.clear()
        await hidraw_watcher.handle_press("teams", teams, client)
        await asyncio.sleep(0.2)
        check("speaking: the button stops the assistant instead",
              ran == [] and received and received[-1]["command"] == "stop_pipeline",
              f"ran={ran} received={received}")

        received.clear()
        ran.clear()
        await hidraw_watcher.handle_press("teams", teams, client)
        await asyncio.sleep(0.2)
        check("a second press does not send stop twice",
              ran == ["echo TEAMS_NORMAL"] and not received, f"ran={ran}")

        await conns[0].send(json.dumps({"event": "tts_speaking", "data": {}}))
        await asyncio.sleep(0.2)
        await conns[0].send(json.dumps({"event": "tts_finished", "data": {}}))
        await asyncio.sleep(0.2)
        received.clear()
        ran.clear()
        await hidraw_watcher.handle_press("teams", teams, client)
        check("after the answer ends the button opens a conversation again",
              ran == ["echo TEAMS_NORMAL"] and not received, f"ran={ran}")

        await conns[0].send(json.dumps({"event": "timer_ringing", "data": {}}))
        await asyncio.sleep(0.2)
        received.clear()
        ran.clear()
        await hidraw_watcher.handle_press("teams", teams, client)
        await asyncio.sleep(0.2)
        check("a ringing timer is silenced, not talked over",
              received and received[-1]["command"] == "stop_pipeline", f"received={received}")

        plain = make_config().teams
        plain.command = "echo TEAMS_NORMAL"
        await conns[0].send(json.dumps({"event": "tts_speaking", "data": {}}))
        await asyncio.sleep(0.2)
        received.clear()
        ran.clear()
        await hidraw_watcher.handle_press("teams", plain, client)
        await asyncio.sleep(0.2)
        check("a button not marked as interrupting is unaffected",
              ran == ["echo TEAMS_NORMAL"] and not received, f"ran={ran}")
        await conns[0].send(json.dumps({"event": "idle", "data": {}}))
        await asyncio.sleep(0.2)

        # Pressing the physical button is the only thing that can make the
        # daemon believe the microphone is off, and that belief has to survive
        # the assistant restarting underneath it — the assistant is a container
        # that gets rebuilt, and a redeploy that quietly reopened a microphone
        # the user had closed by hand would be the opposite failure to the one
        # this reconciliation was rewritten to stop.
        await client.report_mute(True)
        ran.clear()
        received.clear()
        await conns[0].close()
        await asyncio.sleep(1.2)
        check("reconnects after the assistant drops", client.connected)
        check("a hand-muted microphone stays muted across a reconnect",
              any(m["command"] == "mute_mic" for m in received), str(received))
        check("a reconnect does not touch the hardware switch",
              ran == [], f"ran={ran}")
        check("a reconnect still does not guess",
              probed == [], f"probed={len(probed)}")

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    ran.clear()
    offline = lva.LVAClient(make_config())
    sent = await offline.volume_step(up=True)
    check("volume_step reports failure when disconnected", sent is False)
    await evdev_watcher._volume_step(make_config(), offline, up=True)
    check("button falls back to the local mixer",
          ran == ["echo LOCAL_UP"], f"ran={ran}")

    lva._import_connect = lambda: None
    await asyncio.wait_for(lva.LVAClient(make_config()).run(), timeout=2)
    check("degrades quietly when websockets is absent", True)

    # Sync fully disabled must not open a connection at all.
    await asyncio.wait_for(
        lva.LVAClient(make_config(lva_sync_volume=False, lva_sync_mute=False)).run(),
        timeout=2)
    check("does nothing when neither volume nor mute is synced", True)

    print("\n" + ("ALL PASS" if not FAILS else f"FAILURES: {FAILS}"))
    return 1 if FAILS else 0


sys.exit(asyncio.run(main()))
