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

from . import __version__
from .daemon import run

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