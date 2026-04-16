"""
Run user-configured shell commands without blocking the asyncio event loop.
"""
from __future__ import annotations

import asyncio
import logging
import os

_LOG = logging.getLogger(__name__)


async def run(command: str, extra_env: dict[str, str] | None = None) -> None:
    """Execute a shell command asynchronously, merging extra_env into the environment."""
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)

    _LOG.debug("Running: %s  env=%s", command, extra_env)
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await proc.communicate()
        if stdout:
            for line in stdout.decode(errors="replace").splitlines():
                _LOG.debug("  cmd> %s", line)
        if proc.returncode != 0:
            _LOG.warning("Command exited %d: %s", proc.returncode, command)
    except Exception as exc:
        _LOG.error("Failed to run command %r: %s", command, exc)
