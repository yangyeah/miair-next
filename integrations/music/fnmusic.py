"""Optional local fnOS Music integration. The catalog and media mounts are read-only."""

import asyncio
from contextlib import asynccontextmanager, suppress
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
import ipaddress
from urllib.parse import quote
from urllib.parse import urlsplit

from aiohttp import ClientSession, ClientTimeout, UnixConnector
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

from app.engine.const import GET_ASK_BY_MINA
from app.engine.conversation import ConversationPoller

log = logging.getLogger("miair")


def search(query, database, root):
    """Search only indexed, existing files inside the configured library."""
    root = Path(root).resolve(strict=True)
    singer, sep, title = query.partition("的")
    artist_only = sep and title in ("歌", "歌曲", "音乐")
    with sqlite3.connect(
        Path(database).resolve().as_uri() + "?mode=ro", uri=True
    ) as db:
        rows = db.execute(
            """
            SELECT t.guid, f.path, t.title,
                   (SELECT group_concat(a.name,' / ') FROM track_artist ta
                    JOIN artist a ON a.id=ta.artist_id WHERE ta.track_id=t.id) AS artists
            FROM track t JOIN audio_file f ON f.id=t.audio_file_id
            WHERE instr(f.path, ?) = 1 AND NOT f.is_physical_file_deleted
              AND NOT t.is_audio_file_deleted AND NOT t.is_admin_deleted
              AND (instr(lower(t.title),lower(?)) > 0 OR
                   (instr(lower(artists),lower(?)) > 0 AND instr(lower(t.title),lower(?)) > 0))
            ORDER BY (lower(t.title)=lower(?)) DESC, t.title, t.id LIMIT 100
        """,
            (
                str(root) + "/",
                query,
                singer,
                "" if artist_only or not sep else title,
                title if sep else query,
            ),
        )
        tracks = []
        for guid, filename, name, artists in rows:
            path = Path(filename).resolve()
            if root in path.parents and path.is_file():
                tracks.append((guid, path, name))
        return tracks


async def favorite(cfg, guid):
    """Write through the music service as the configured listener."""
    token = Path(cfg["music_token_file"]).read_text().strip()
    path = "/music/api/v1/favorite-track/create"
    body = json.dumps({"trackGUID": guid}, separators=(",", ":")).encode()
    nonce, ts = str(secrets.randbelow(900000) + 100000), str(int(time.time() * 1000))
    source = "_".join(
        (
            "NDzZTVxnRKP8Z0jXg1VAMonaG8akvh",
            path,
            nonce,
            ts,
            hashlib.md5(body).hexdigest(),
            "6D5602D4-A342-4799-A0F0-BB795E7167D0",
        )
    )
    headers = {
        "authx": f"nonce={nonce}&timestamp={ts}&sign={hashlib.md5(source.encode()).hexdigest()}",
        "Cookie": "music-token=" + quote(token, safe=""),
        "Content-Type": "application/json",
    }
    async with ClientSession(
        connector=UnixConnector(path=cfg["music_socket"]),
        timeout=ClientTimeout(total=10),
    ) as session:
        async with session.post(
            "http://localhost" + path, headers=headers, data=body
        ) as res:
            res.raise_for_status()
            if str((await res.json(content_type=None)).get("code")) not in ("0", "200"):
                raise ValueError("飞牛音乐未接受收藏，请检查音乐账号凭据")


class LocalPlayer:
    def __init__(self, cfg):
        self.cfg = cfg
        self.tracks = []
        self.index = 0
        self.ticket = ""
        self.status = "等待点歌"
        self.last_command = ""

    async def command(self, speaker, text):
        text = re.sub(r"[\s，。！？、,.!?]", "", text).removeprefix("小爱同学")
        self.last_command = text
        text = {
            "换一首": "下一首",
            "换首歌": "下一首",
            "继续听": "继续播放",
            "加入收藏": "收藏",
            "暂停一下": "暂停",
        }.get(text, text)
        if text in ("停下", "停止播放", "停止"):
            if self.ticket and await speaker.stop():
                self.ticket = ""
                self.status = "已停止"
            return
        if text in ("暂停", "暂停播放"):
            if self.ticket and await speaker.pause():
                self.status = "已暂停"
            return
        if text in ("收藏", "收藏这首歌"):
            if self.ticket:
                if not self.cfg.get("music_token_file") or not self.cfg.get(
                    "music_socket"
                ):
                    raise ValueError("尚未配置飞牛音乐收藏账号")
                await favorite(self.cfg, self.tracks[self.index][0])
                self.status = "已收藏"
            return
        if text in ("继续", "继续播放", "播放") and self.tracks:
            tracks, index = self.tracks, self.index
        elif text in ("下一首", "播放下一首", "上一首", "播放上一首"):
            tracks = self.tracks
            index = self.index + (1 if "下一首" in text else -1)
            if not self.ticket or not 0 <= index < len(tracks):
                return
        else:
            match = re.fullmatch(
                r"(?:请)?(?:帮我)?(?:播放|我想听|来首|来一首)(.+)", text
            )
            if not match:
                return
            tracks = await asyncio.to_thread(
                search, match[1], self.cfg["database"], self.cfg["music_root"]
            )
            index = 0
            if not tracks:
                self.status = "本地曲库没有匹配歌曲"
                return
        previous = self.tracks, self.index, self.ticket
        self.tracks, self.index, self.ticket = tracks, index, secrets.token_urlsafe(32)
        try:
            url = (
                self.cfg["public_url"].rstrip("/")
                + "/api/v1/fnmusic/media/"
                + self.ticket
            )
            if not await speaker.play_url(url):
                raise RuntimeError("音箱未接受本地歌曲")
            self.status = "正在播放 · " + tracks[index][2]
        except BaseException:
            self.tracks, self.index, self.ticket = previous
            raise

    async def listen(self, orch):
        poller = ConversationPoller(orch.config, orch.auth, lambda: {})
        async with ClientSession(timeout=ClientTimeout(total=7)) as session:
            while True:
                await asyncio.sleep(1)
                try:
                    controller = orch.speaker_manager.get_controller(self.cfg["did"])
                    if not controller:
                        continue
                    device, hw = controller.device_id, controller.speaker.hardware
                    poller._last_timestamp.setdefault(device, int(time.time() * 1000))
                    poller.last_record = None
                    if hw in GET_ASK_BY_MINA:
                        await poller.get_latest_ask_by_mina(device)
                    else:
                        await poller.get_latest_ask_from_xiaoai(session, device, hw)
                    if poller.last_record:
                        await self.command(controller, poller.last_record["query"])
                except Exception as exc:
                    self.status = (
                        str(exc)
                        if isinstance(exc, ValueError)
                        else "音箱或音乐服务暂不可用 · " + type(exc).__name__
                    )
                    log.warning("本地音乐处理失败 (%s)", type(exc).__name__)


def register(protected_router, public_router):
    player = None
    task = None
    application = None
    config_lock = asyncio.Lock()
    config_path = Path(os.getenv("MIAIR_FNMUSIC_CONFIG", "/app/data/fnmusic.json"))

    def validate(cfg):
        if "enabled" in cfg and not isinstance(cfg["enabled"], bool):
            raise ValueError("启用状态必须是布尔值")
        for key in ("database", "music_root", "public_url", "did"):
            if not cfg.get(key):
                raise ValueError(f"飞牛音乐缺少配置 {key}")
        root = Path(cfg["music_root"]).resolve(strict=True)
        if not root.is_dir():
            raise ValueError("音乐目录不存在")
        url = urlsplit(cfg["public_url"])
        if (
            url.scheme not in ("http", "https")
            or not ipaddress.ip_address(url.hostname).is_private
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path.strip("/")
        ):
            raise ValueError("播放地址应为音箱可访问的局域网 IP 和端口")
        search("__configuration_probe__", cfg["database"], root)
        if cfg.get("music_token_file") and not Path(cfg["music_token_file"]).is_file():
            raise ValueError("收藏账号文件不存在")
        if cfg.get("music_socket") and not Path(cfg["music_socket"]).is_socket():
            raise ValueError("飞牛音乐服务 socket 不存在")

    async def replace(cfg):
        nonlocal player, task
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            task = None
        player = LocalPlayer(cfg)
        if cfg.get("enabled", True):
            task = asyncio.create_task(player.listen(application.state.orchestrator))
        else:
            player.status = "已关闭本地音乐语音联动"

    @asynccontextmanager
    async def lifespan(app):
        nonlocal player, application
        application = app
        if config_path.exists():
            cfg = json.loads(config_path.read_text())
            await replace(cfg)
        try:
            yield
        finally:
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            player = None

    router = APIRouter(lifespan=lifespan)
    admin = APIRouter()

    @router.get("/fnmusic/settings", include_in_schema=False)
    async def settings_page():
        return HTMLResponse(Path(__file__).with_name("fnmusic.html").read_text())

    @admin.get("/fnmusic/config")
    async def configuration():
        cfg = player.cfg if player else {}
        keys = (
            "database",
            "music_root",
            "public_url",
            "did",
            "music_token_file",
            "music_socket",
            "enabled",
        )
        return {
            "config": {k: cfg[k] for k in keys if k in cfg},
            "state": player.status if player else "尚未配置",
            "last": player.last_command if player else "",
        }

    @admin.post("/fnmusic/config")
    async def save_configuration(body: dict):
        async with config_lock:
            return await save_locked(body)

    async def save_locked(body):
        keys = (
            "database",
            "music_root",
            "public_url",
            "did",
            "music_token_file",
            "music_socket",
            "enabled",
        )
        cfg = {k: body[k] for k in keys if k in body}
        try:
            await asyncio.to_thread(validate, cfg)
            if not application.state.orchestrator.speaker_manager.get_controller(
                cfg["did"]
            ):
                raise ValueError("音箱尚未在 MiAir 启用")
        except (ValueError, OSError, sqlite3.Error) as exc:
            raise HTTPException(400, str(exc)[:150])
        if player and player.ticket:
            old = application.state.orchestrator.speaker_manager.get_controller(
                player.cfg["did"]
            )
            if old and not await old.stop():
                raise HTTPException(409, "旧音箱未停止，请稍后重试")
        config_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = config_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(cfg, ensure_ascii=False))
        temporary.chmod(0o600)
        temporary.replace(config_path)
        await replace(cfg)
        return {"ok": True}

    @router.api_route(
        "/fnmusic/media/{ticket}", methods=["GET", "HEAD"], include_in_schema=False
    )
    async def media(ticket: str):
        if (
            not player
            or not player.ticket
            or not secrets.compare_digest(ticket, player.ticket)
        ):
            raise HTTPException(404)
        path = player.tracks[player.index][1].resolve()
        if (
            Path(player.cfg["music_root"]).resolve() not in path.parents
            or not path.is_file()
        ):
            raise HTTPException(404)
        return FileResponse(path)

    public_router.include_router(router)
    protected_router.include_router(admin)
