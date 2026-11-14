"""Run the existing MiAir application and MiPlay in one container."""

import asyncio
import json
import os
import signal
import sys
import urllib.request
from pathlib import Path


async def supervise(commands, *, bridge_uid=None, bridge_gid=None):
    processes = []
    waits = []
    try:
        for index, command in enumerate(commands):
            options = {"start_new_session": True}
            if index == 1 and os.geteuid() == 0 and bridge_uid is not None:
                options.update(user=bridge_uid, group=bridge_gid, extra_groups=[])
            processes.append(await asyncio.create_subprocess_exec(*command, **options))
        waits = [asyncio.create_task(process.wait()) for process in processes]
        done, _ = await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        return next(iter(done)).result() or 1
    finally:
        # Stop the bridge first so it can finish the speaker's normal stop
        # command while the MiAir API is still available.
        for process in reversed(processes):
            if process.returncode is None:
                try:
                    process.terminate()
                    await asyncio.wait_for(process.wait(), timeout=20)
                except ProcessLookupError:
                    pass
                except asyncio.TimeoutError:
                    pass
            # Also reap native receiver children left by an exited parent.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
        for wait in waits:
            wait.cancel()
        await asyncio.gather(*waits, return_exceptions=True)


def healthcheck():
    config = json.loads(
        Path(os.environ.get("MIPLAY_CONFIG", "/run/miplay/config.json")).read_text()
    )
    for url in (
        "http://127.0.0.1:"
        + os.environ.get("MIAIR_WEB_PORT", "8300")
        + "/api/v1/health",
        "http://127.0.0.1:" + str(config.get("health_port", 18301)) + "/health",
    ):
        with urllib.request.urlopen(url, timeout=2) as response:
            if response.status != 200:
                raise RuntimeError("Service not healthy")


async def main():
    command = sys.argv[1:] or [sys.executable, "run.py"]
    # Respect the base image's privilege and native-receiver setup when present.
    for entrypoint in (
        "/app/backend/scripts/entrypoint.sh",
        "/usr/local/bin/entrypoint.sh",
    ):
        if Path(entrypoint).is_file():
            command = ["/bin/sh", entrypoint, *command]
            break
    current = asyncio.current_task()
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, current.cancel)
    return await supervise(
        [command, [sys.executable, "/opt/miplay/bridge.py"]],
        bridge_uid=int(os.environ.get("MIPLAY_UID", "1000")),
        bridge_gid=int(os.environ.get("MIPLAY_GID", "1000")),
    )


if __name__ == "__main__":
    if sys.argv[1:] == ["--healthcheck"]:
        healthcheck()
    else:
        try:
            sys.exit(asyncio.run(main()))
        except asyncio.CancelledError:
            pass
