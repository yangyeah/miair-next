import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

from supervisor import supervise


@unittest.skipUnless(os.name == "posix", "Linux container process supervision")
class SupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def test_child_exit_terminates_other_service(self):
        with tempfile.TemporaryDirectory() as directory:
            pidfile = Path(directory) / "pid"
            result = await asyncio.wait_for(
                supervise(
                    [
                        [sys.executable, "-c", "import time; time.sleep(0.3); exit(7)"],
                        [
                            sys.executable,
                            "-c",
                            "import os,time,pathlib; "
                            f"pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid())); "
                            "time.sleep(60)",
                        ],
                    ]
                ),
                timeout=5,
            )
            self.assertEqual(result, 7)
            with self.assertRaises(ProcessLookupError):
                os.kill(int(pidfile.read_text()), 0)

    async def test_shutdown_stops_bridge_before_main_api(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "service.py"
            script.write_text(
                "import signal,sys,time\n"
                "from pathlib import Path\n"
                "name=sys.argv[1]\n"
                f"root=Path({str(root)!r})\n"
                "def stop(*args):\n"
                "    with (root/'order').open('a') as f: f.write(name+'\\n')\n"
                "    sys.exit(0)\n"
                "signal.signal(signal.SIGTERM,stop)\n"
                "(root/name).touch()\n"
                "while True: time.sleep(0.01)\n"
            )
            task = asyncio.create_task(
                supervise(
                    [[sys.executable, str(script), name] for name in ("main", "bridge")]
                )
            )
            try:
                async with asyncio.timeout(5):
                    while not all(
                        (root / name).exists() for name in ("main", "bridge")
                    ):
                        await asyncio.sleep(0.01)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            self.assertEqual(
                (root / "order").read_text().splitlines(), ["bridge", "main"]
            )
