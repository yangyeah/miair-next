"""Add the upstream stop route to older MiAir base images at build time."""

import ast
from pathlib import Path

ROUTE = "/speakers/{did}/stop"
EXTENSION = '''

@router.post("/speakers/{did}/stop")
async def stop_speaker(did: str, orch: Orchestrator = Depends(get_orchestrator)):
    """Stop through the existing authenticated speaker router."""
    controller = _get_controller(orch, did)
    return {"ok": await controller.stop()}
'''


def install(path):
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                if (
                    isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Attribute)
                    and decorator.func.attr == "post"
                    and decorator.args
                    and isinstance(decorator.args[0], ast.Constant)
                    and decorator.args[0].value == ROUTE
                ):
                    return False
    if not any(
        isinstance(node, ast.FunctionDef) and node.name == "_get_controller"
        for node in tree.body
    ):
        raise RuntimeError("Unsupported MiAir speaker API layout")
    updated = source.rstrip() + "\n" + EXTENSION
    compile(updated, str(path), "exec")
    path.write_text(updated, encoding="utf-8")
    return True


if __name__ == "__main__":
    # Backport the upstream event-loop fix on older integrated base images.
    server = Path("/app/backend/app/engine/airplay/server.py")
    if server.is_file():
        source = server.read_text(encoding="utf-8")
        old = "        self._mdns.stop()\n"
        if old in source and "import asyncio" in source:
            updated = source.replace(
                old, "        await asyncio.to_thread(self._mdns.stop)\n"
            )
            compile(updated, str(server), "exec")
            server.write_text(updated, encoding="utf-8")
    changed = install(Path("/app/backend/app/api/v1/speakers.py"))
    print(
        "MiAir stop route installed" if changed else "MiAir stop route already exists"
    )
