"""
Entry point: python3 -m speakerctl [--config PATH] [--debug]
SIGHUP reloads configuration without restarting the service.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

# Before anything heavy is imported. Python's default action for SIGHUP is to
# terminate, systemd's ExecReload sends exactly that, and the real handler
# cannot be installed until the event loop exists — with evdev, pyudev and
# websockets loading in between. A reload issued in that window killed the
# daemon outright, which looks like a crash and leaves the buttons dead;
# restarting and reloading a moment later is what any deploy script does.
# Losing a reload during start-up costs nothing: the config is read anyway.
signal.signal(signal.SIGHUP, signal.SIG_IGN)

from . import __version__  # noqa: E402
from .daemon import run  # noqa: E402

DEFAULT_CONFIG = "/etc/speakerctl/config.toml"



def main() -> None:
    parser = argparse.ArgumentParser(
        description=f"speakerctl {__version__} — Microsoft Modern USB-C Speaker daemon"
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG,
                        help=f"Path to config.toml (default: {DEFAULT_CONFIG})")
    parser.add_argument("--debug", action="store_true",
                        help="Enable DEBUG logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    reload_event = asyncio.Event()

    def _sighup(*_) -> None:
        loop.call_soon_threadsafe(reload_event.set)

    signal.signal(signal.SIGHUP, _sighup)

    try:
        loop.run_until_complete(run(args.config, reload_event))
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()


if __name__ == "__main__":
    main()