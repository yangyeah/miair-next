import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from install_stop_api import install


class StopApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_route_is_added_once_and_uses_existing_controller(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "speakers.py"
            path.write_text(
                "def _get_controller(orch, did):\n"
                "    assert did == 'test-speaker'\n"
                "    return orch\n"
            )
            self.assertTrue(install(path))
            installed = path.read_bytes()
            self.assertFalse(install(path))
            self.assertEqual(installed, path.read_bytes())
            routes = {}

            class Router:
                def post(self, route):
                    def register(function):
                        routes[route] = function
                        return function

                    return register

            namespace = {
                "router": Router(),
                "Orchestrator": object,
                "Depends": lambda value: value,
                "get_orchestrator": object(),
            }
            exec(compile(installed, str(path), "exec"), namespace)
            controller = AsyncMock()
            controller.stop.return_value = True
            response = await routes["/speakers/{did}/stop"]("test-speaker", controller)
            self.assertEqual(response, {"ok": True})
            controller.stop.assert_awaited_once_with()

    def test_unknown_base_image_fails_build_instead_of_silently_missing_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "speakers.py"
            path.write_text("# unknown API layout\n")
            with self.assertRaises(RuntimeError):
                install(path)
