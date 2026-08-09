"""
Config parsing and the self-check, neither of which had any coverage.

`config.toml` is the one file every user edits, and `--check` is the thing they
run when it goes wrong, so between them they carry most of a stranger's first
hour. Both are pure functions over strings and files — no hardware, no daemon.

    python3 tests/test_config_and_check.py
"""
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# evdev is imported at module scope by parts of the package and is not needed
# for anything here.
_stub = types.ModuleType("evdev")
_stub.ecodes = types.SimpleNamespace(
    EV_KEY=1, KEY_VOLUMEUP=115, KEY_VOLUMEDOWN=114, KEY_MICMUTE=248
)
_stub.InputDevice = object
sys.modules.setdefault("evdev", _stub)

from speakerctl import check  # noqa: E402
from speakerctl.config import load_config  # noqa: E402

FAILS = []


def ok(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  — ' + detail if detail else ''}")
    if not cond:
        FAILS.append(name)


def write(text):
    fd = tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False)
    fd.write(text)
    fd.close()
    return fd.name


MINIMAL = """
[device]
vid = "045e"
pid = "083e"

[volume_up]
command = "true"
[volume_down]
command = "true"
[mute]
command = "true"
[teams]
command = "true"
[phone]
command = "true"
"""


def test_config():
    cfg = load_config(write(MINIMAL))
    ok("a minimal config loads", cfg is not None)
    ok("alsa_card defaults to Speaker", cfg.alsa_card == "Speaker", cfg.alsa_card)
    ok("lva is off unless asked for", cfg.lva_enabled is False)

    # The failure a user hits by commenting a section out to disable a button.
    # It raises rather than defaulting, which is defensible — but the daemon
    # then dies at start-up and systemd restarts it every three seconds, so it
    # is worth knowing that this is the designed behaviour and not a surprise.
    missing = MINIMAL.replace('[phone]\ncommand = "true"\n', "")
    try:
        load_config(write(missing))
        ok("a missing button section is rejected", False, "it was accepted")
    except Exception as exc:
        ok("a missing button section is rejected", True, type(exc).__name__)

    enabled = MINIMAL + '\n[lva]\nenabled = true\nurl = "ws://127.0.0.1:6055"\n'
    cfg = load_config(write(enabled))
    ok("lva can be enabled", cfg.lva_enabled is True)
    ok("lva syncs both by default when enabled",
       cfg.lva_sync_volume and cfg.lva_sync_mute)


def test_alsa_card_parsing(monkeypatched=None):
    """The bug this replaced: `card in file_contents`.

    A host whose card is *described* as "USB Speaker" made a substring test
    report that a card *named* Speaker was ready, and every amixer call after
    that failed against a card that did not exist.
    """
    sample = (
        " 0 [PCH            ]: HDA-Intel - HDA Intel PCH\n"
        "                      HDA Intel PCH at 0xf7f10000 irq 33\n"
        " 1 [Generic        ]: USB-Audio - USB Speaker\n"
    )
    tmp = Path(tempfile.mkdtemp()) / "cards"
    tmp.write_text(sample)

    real = check.Path
    class FakePath:
        def __init__(self, p):
            self._p = tmp if str(p) == "/proc/asound/cards" else real(p)
        def read_text(self):
            return self._p.read_text()
    check.Path = FakePath
    try:
        names = check.alsa_cards()
        ok("card names are parsed from the brackets", names == ["PCH", "Generic"], str(names))
        ok("a description is not mistaken for a name", "Speaker" not in names, str(names))
    finally:
        check.Path = real


def test_check_report():
    rep = check.Report()
    rep.add(check.OK, "fine")
    ok("a report with no failures does not fail", rep.failed() is False)
    rep.add(check.WARN, "meh")
    ok("a warning is not a failure", rep.failed() is False and rep.warned() is True)
    rep.add(check.FAIL, "broken")
    ok("a failure is a failure", rep.failed() is True)


def test_ws_url():
    ok("a ws url splits into host and port",
       check._split_ws_url("ws://127.0.0.1:6055") == ("127.0.0.1", 6055))
    ok("a malformed url is reported, not guessed",
       check._split_ws_url("nonsense")[0] is None)


test_config()
test_alsa_card_parsing()
test_check_report()
test_ws_url()

print("\n" + ("ALL PASS" if not FAILS else f"FAILURES: {FAILS}"))
sys.exit(1 if FAILS else 0)
