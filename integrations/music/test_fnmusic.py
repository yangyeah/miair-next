"""Run inside the optional image with its engine dependencies available."""

import asyncio
import json
import os
from pathlib import Path
import socket
import sqlite3
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import uvicorn
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException
from app.music_application import LocalPlayer, register, search


class MusicTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.music = self.root / "music"
        self.music.mkdir()
        self.db = self.root / "catalog.db"
        with sqlite3.connect(self.db) as c:
            c.executescript(
                """CREATE TABLE track(id, guid, audio_file_id, title, is_audio_file_deleted, is_admin_deleted);
CREATE TABLE audio_file(id,path,is_physical_file_deleted);
CREATE TABLE artist(id,name); CREATE TABLE track_artist(track_id,artist_id);"""
            )
            c.execute("INSERT INTO artist VALUES(1,?)", ("测试歌手",))
            for i in range(1, 4):
                path = (self.music if i < 3 else self.root) / f"{i}.flac"
                path.write_bytes(b"fLaCtest-audio")
                c.execute("INSERT INTO audio_file VALUES(?,?,0)", (i, str(path)))
                c.execute(
                    "INSERT INTO track VALUES(?,?,?,?,0,0)",
                    (i, str(i), i, f"测试曲{i}"),
                )
                c.execute("INSERT INTO track_artist VALUES(?,1)", (i,))
        self.cfg = dict(
            database=str(self.db),
            music_root=str(self.music),
            public_url="http://127.0.0.1:18766",
            did="test",
        )

    async def test_search_scopes_files_and_deleted_rows(self):
        tracks = search("测试歌手的歌", self.db, self.music)
        self.assertEqual([x[0] for x in tracks], ["1", "2"])
        (self.music / "1.flac").unlink()
        self.assertEqual(
            [x[0] for x in search("测试歌手的歌", self.db, self.music)], ["2"]
        )
        self.assertEqual(search("不存在", self.db, self.music), [])

    async def test_next_favorite_and_stop(self):
        p = LocalPlayer(dict(self.cfg, music_token_file="file", music_socket="socket"))
        controller = SimpleNamespace(
            play_url=AsyncMock(return_value=True),
            stop=AsyncMock(return_value=True),
            pause=AsyncMock(return_value=True),
        )
        await p.command(controller, "播放测试歌手的歌")
        await p.command(controller, "下一首")
        self.assertEqual(p.index, 1)
        with patch(
            "app.music_application.favorite", new_callable=AsyncMock
        ) as favorite:
            await p.command(controller, "收藏")
            self.assertEqual(favorite.await_args.args[1], "2")
        await p.command(controller, "停下")
        self.assertEqual(p.ticket, "")
        await p.command(controller, "下一首")
        self.assertEqual(controller.play_url.await_count, 2)

    async def test_failed_play_restores_current_ticket(self):
        p = LocalPlayer(self.cfg)
        controller = SimpleNamespace(play_url=AsyncMock(return_value=True))
        await p.command(controller, "播放测试曲1")
        ticket = p.ticket
        controller.play_url.return_value = False
        with self.assertRaises(RuntimeError):
            await p.command(controller, "播放测试曲2")
        self.assertEqual(p.ticket, ticket)
        self.assertEqual(p.tracks[p.index][0], "1")

    async def test_admin_configuration_and_media_ranges(self):
        cfg_path = self.root / "config.json"
        cfg_path.write_text(json.dumps(self.cfg))
        controller = SimpleNamespace(
            play_url=AsyncMock(return_value=True), stop=AsyncMock(return_value=True)
        )
        app = FastAPI()
        app.state.orchestrator = SimpleNamespace(
            speaker_manager=SimpleNamespace(
                get_controller=lambda did: controller if did == "test" else None
            )
        )

        async def auth(authorization: str = Header(default="")):
            if authorization != "Bearer test":
                raise HTTPException(401)

        protected = APIRouter(dependencies=[Depends(auth)])
        public = APIRouter()

        async def listen(player, orch):
            await player.command(controller, "播放测试曲1")
            app.state.ticket = player.ticket
            await asyncio.Event().wait()

        with patch.dict(os.environ, MIAIR_FNMUSIC_CONFIG=str(cfg_path)), patch.object(
            LocalPlayer, "listen", listen
        ):
            register(protected, public)
            app.include_router(protected, prefix="/api/v1")
            app.include_router(public, prefix="/api/v1")
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
            thread = threading.Thread(
                target=lambda: server.run(sockets=[sock]), daemon=True
            )
            thread.start()

            def fetch(path, data=None, headers=None):
                req = Request(
                    f"http://127.0.0.1:{port}/api/v1/" + path,
                    data=json.dumps(data).encode() if data is not None else None,
                    headers={"Content-Type": "application/json", **(headers or {})},
                )
                try:
                    with urlopen(req, timeout=5) as r:
                        return r.status, r.read()
                except HTTPError as e:
                    return e.code, e.read()

            try:
                for _ in range(100):
                    if server.started and hasattr(app.state, "ticket"):
                        break
                    await asyncio.sleep(0.05)
                self.assertTrue(server.started)
                self.assertEqual(fetch("fnmusic/config")[0], 401)
                self.assertEqual(fetch("fnmusic/media/bad")[0], 404)
                self.assertEqual(
                    fetch(
                        "fnmusic/media/" + app.state.ticket,
                        headers={"Range": "bytes=0-3"},
                    ),
                    (206, b"fLaC"),
                )
                self.assertEqual(
                    fetch(
                        "fnmusic/config",
                        dict(self.cfg, public_url="http://example.com"),
                        {"Authorization": "Bearer test"},
                    )[0],
                    400,
                )
                self.assertEqual(
                    fetch(
                        "fnmusic/config",
                        dict(self.cfg, enabled=False),
                        {"Authorization": "Bearer test"},
                    )[0],
                    200,
                )
                self.assertFalse(json.loads(cfg_path.read_text())["enabled"])
                self.assertEqual(fetch("fnmusic/media/" + app.state.ticket)[0], 404)
                self.assertEqual(fetch("fnmusic/settings")[0], 200)
            finally:
                server.should_exit = True
                await asyncio.to_thread(thread.join, 8)


if __name__ == "__main__":
    unittest.main()
