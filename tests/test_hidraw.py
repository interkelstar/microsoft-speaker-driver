"""
Behavioural test for the hidraw button reader: no blocking, and tap gestures.

Run it with plain python3:  python3 tests/test_hidraw.py
Needs no hardware — a FIFO stands in for /dev/hidraw0.

Two things it guards:
* the daemon froze for hours in a blocking hidraw read() after a stale
  wake-up (2026-09-27), taking the assistant connection down with it;
* double tap, which was removed in April because the shared release report
  05 00 00 fired the phone action on every mute press.
"""
import asyncio
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from speakerctl import executor, hidraw_watcher  # noqa: E402
from speakerctl.config import ButtonConfig, Config  # noqa: E402

FAILS = []
PHONE = b"\x05\x01\x00"
MUTE = b"\x05\x02\x00"
RELEASE = b"\x05\x00\x00"
TEAMS = b"\x9b\x01"


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  — ' + detail if detail else ''}")
    if not cond:
        FAILS.append(name)


def make_config(phone=None, teams=None):
    return Config(
        vid="045e", pid="083e", alsa_card="Speaker",
        volume_up=ButtonConfig("up"), volume_down=ButtonConfig("down"),
        mute=ButtonConfig("mute"),
        teams=teams or ButtonConfig("teams"),
        phone=phone or ButtonConfig("phone"),
    )


class FakeLVA:
    def __init__(self, noisy):
        self.noisy = noisy
        self.interrupts = 0

    async def interrupt_playback(self):
        if not self.noisy:
            return False
        self.noisy = False
        self.interrupts += 1
        return True


def test_read_never_blocks():
    r, w = os.pipe()
    # Control: the old way — a blocking read on an fd with nothing waiting —
    # must hang, or this test cannot tell broken from fixed.
    done = threading.Event()
    threading.Thread(target=lambda: (os.read(r, 64), done.set()), daemon=True).start()
    check("control: blocking read on an empty fd hangs", not done.wait(0.3))
    os.write(w, b"x")  # release the stuck thread
    done.wait(1)

    r2, w2 = os.pipe()
    os.set_blocking(r2, False)
    t0 = time.monotonic()
    got = hidraw_watcher.read_pending(r2)
    check("read_pending on an empty fd returns at once",
          got == [] and time.monotonic() - t0 < 0.05, f"{got!r}")
    os.write(w2, PHONE)
    check("read_pending returns what is waiting", hidraw_watcher.read_pending(r2) == [PHONE])
    check("…and then nothing, without blocking", hidraw_watcher.read_pending(r2) == [])


async def run_dispatch(presses, phone=None, teams=None, lva=None, settle=0.6):
    ran = []

    async def fake_run(command, extra_env=None, quiet=False):
        ran.append(command)
        return 0

    executor.run, orig = fake_run, executor.run
    try:
        d = hidraw_watcher.ButtonDispatcher(make_config(phone, teams), lva)
        for delay, report in presses:
            await asyncio.sleep(delay)
            await d.report(report)
        await asyncio.sleep(settle)
    finally:
        executor.run = orig
    return ran


async def test_gestures():
    tap = ButtonConfig("single", double_tap_command="double", double_tap_seconds=0.3)

    ran = await run_dispatch([(0, PHONE)], phone=tap)
    check("single tap runs the single command once", ran == ["single"], f"{ran}")

    ran = await run_dispatch([(0, PHONE), (0.2, PHONE)], phone=tap)
    check("double tap runs only the double command", ran == ["double"], f"{ran}")

    ran = await run_dispatch([(0, PHONE), (0.5, PHONE)], phone=tap, settle=0.5)
    check("two presses outside the window are two singles", ran == ["single", "single"], f"{ran}")

    ran = await run_dispatch([(0, PHONE), (0.02, PHONE)], phone=tap)
    check("a press bouncing within 80 ms is one press", ran == ["single"], f"{ran}")

    ran = await run_dispatch([(0, MUTE), (0.1, RELEASE), (0.1, MUTE), (0.1, RELEASE)], phone=tap)
    check("mute presses and shared releases never fire the phone", ran == [], f"{ran}")

    ran = await run_dispatch([(0, PHONE), (0.05, RELEASE), (0.15, PHONE), (0.05, RELEASE)], phone=tap)
    check("releases between taps do not break a double tap", ran == ["double"], f"{ran}")

    ran = await run_dispatch([(0, PHONE), (0.2, PHONE)], phone=ButtonConfig("single"), settle=0.1)
    check("without double_tap_command every press acts at once", ran == ["single", "single"], f"{ran}")

    ran = await run_dispatch([(0, PHONE), (0.2, PHONE)],
                             phone=ButtonConfig("single", debounce_seconds=0.5), settle=0.1)
    check("control: a 0.5 s debounce swallows the second press", ran == ["single"], f"{ran}")

    lva = FakeLVA(noisy=True)
    t_tap = ButtonConfig("teams", interrupts_playback=True,
                         double_tap_command="teams2", double_tap_seconds=0.3)
    ran = await run_dispatch([(0, TEAMS)], teams=t_tap, lva=lva, settle=0.4)
    check("mid-answer, an interrupting button stops at once and runs nothing",
          ran == [] and lva.interrupts == 1, f"{ran} interrupts={lva.interrupts}")


async def test_watch_keeps_loop_alive():
    with tempfile.TemporaryDirectory() as tmp:
        fifo = os.path.join(tmp, "hidraw")
        os.mkfifo(fifo)
        ran = []

        async def fake_run(command, extra_env=None, quiet=False):
            ran.append(command)
            return 0

        executor.run, orig = fake_run, executor.run
        writer = None
        task = asyncio.create_task(hidraw_watcher.watch(fifo, make_config()))
        try:
            await asyncio.sleep(0.05)
            writer = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
            ticks = 0
            for i in range(20):
                if i in (2, 10):
                    os.write(writer, PHONE)
                await asyncio.sleep(0.02)
                ticks += 1
            await asyncio.sleep(0.05)
        finally:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, OSError):
                pass
            executor.run = orig
            if writer is not None:
                os.close(writer)
        check("event loop keeps running between and after presses", ticks == 20)
        check("both presses reached the command", ran == ["phone", "phone"], f"{ran}")


async def main():
    test_read_never_blocks()
    await test_gestures()
    await test_watch_keeps_loop_alive()


asyncio.run(main())
print(f"\n{'ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'}")
sys.exit(1 if FAILS else 0)
