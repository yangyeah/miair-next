"""Experimental MiPlay -> existing MiAir HTTP API bridge (AGPL-3.0-only)."""

import asyncio
import base64
import ipaddress
import json
import logging
import os
import secrets
import signal
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web
from app.engine.audio_stream import AudioStreamServer

log = logging.getLogger("miplay")


def load_config(path):
    """Validate deployment settings without printing credential values."""
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    for key in (
        "host",
        "name",
        "interface",
        "api",
        "username",
        "password",
        "speaker_did",
    ):
        if not isinstance(config.get(key), str) or not config[key].strip():
            raise ValueError(f"Missing or invalid configuration field: {key}")
    address = ipaddress.IPv4Address(config["host"])
    if address.is_loopback or address.is_unspecified or address.is_multicast:
        raise ValueError("host must be an IPv4 address reachable by the speaker")
    api = urlsplit(config["api"])
    if (
        api.scheme not in ("http", "https")
        or not api.hostname
        or api.username
        or api.password
    ):
        raise ValueError("api must be an HTTP(S) URL without embedded credentials")
    if api.query or api.fragment or not api.path.rstrip("/").endswith("/api/v1"):
        raise ValueError("api must end with /api/v1 without a query or fragment")
    config["api"] = config["api"].rstrip("/")
    for key, default in (("audio_port", 18300), ("health_port", 18301)):
        config.setdefault(key, default)
        if type(config[key]) is not int or not 1024 <= config[key] <= 65535:
            raise ValueError(f"{key} must be an integer between 1024 and 65535")
    if config["audio_port"] == config["health_port"]:
        raise ValueError("audio_port and health_port must be different")
    return config


def decode_pcm(value):
    encoded = value.get("pcm", "")
    if len(encoded) > 65536:
        raise ValueError("oversized PCM frame")
    data = base64.b64decode(encoded, validate=True)
    if not data or len(data) % 4:
        raise ValueError("incomplete stereo PCM frame")
    return data


class Bridge:
    def __init__(self, config):
        self.config = config
        self.stream = AudioStreamServer(
            config["host"],
            port=config.get("audio_port", 18300),
            label="MiPlay",
            route_prefix="miplay",
            isolate_sessions=True,
        )
        self.stream.set_audio_params(48000, 2, 2)
        self.token = ""
        self.source_id = 0
        self.active = False
        self.stopping = False
        self.revision = 0
        self.last_pcm = 0
        self.frames = 0
        self.ready = False
        self.stage = "starting"
        self.last_error = ""
        self.retry_after = 0
        self.discovery_queries = 0
        self.command_lock = asyncio.Lock()
        self.pending = set()

    async def api(self, path, payload):
        for attempt in range(2):
            if not self.token:
                async with self.http.post(
                    self.config["api"] + "/login",
                    json={
                        "username": self.config["username"],
                        "password": self.config["password"],
                    },
                ) as response:
                    response.raise_for_status()
                    self.token = (await response.json())["access_token"]
            async with self.http.post(
                self.config["api"] + path,
                json=payload,
                headers={"Authorization": "Bearer " + self.token},
            ) as response:
                if response.status == 401 and attempt == 0:
                    self.token = ""
                    continue
                response.raise_for_status()
                result = await response.json()
                if result.get("ok") is False:
                    raise RuntimeError("MiAir rejected playback command")
                return result
        raise RuntimeError("MiAir authentication failed")

    async def play(self, revision, url):
        async with self.command_lock:
            if revision != self.revision or not self.active or self.stopping:
                return
            try:
                await self.api(
                    "/speakers/" + self.config["speaker_did"] + "/play_url",
                    {"url": url},
                )
                self.last_error = ""
                log.info("Speaker play command accepted; waiting for HTTP consumer")
            except Exception as error:
                self.last_error = type(error).__name__
                log.error("Speaker command failed: %s", self.last_error)
                if revision == self.revision:
                    self.retire()
                    self.retry_after = time.monotonic() + 5

    def retire(self):
        if self.active:
            self.revision += 1
            self.active = False
            self.stream.stop_streaming()
            # Do not issue a global stop that could interrupt another protocol.
            log.info("Retired this MiPlay HTTP audio session")

    def request_stop(self):
        """Stop the current consumer before closing its unfinished WAV response."""
        if not self.active or self.stopping:
            return
        self.stopping = True
        task = asyncio.create_task(self.stop_speaker())
        self.pending.add(task)
        task.add_done_callback(self.pending.discard)

    async def stop_speaker(self):
        # Serialize with play_url: a delayed play response must not arrive after
        # stop. New PCM is discarded until the old downstream session is closed.
        silence = None
        try:
            async with self.command_lock:
                if self.stream.has_clients:
                    silence = asyncio.create_task(self.keep_stop_alive())
                    await self.api(
                        "/speakers/" + self.config["speaker_did"] + "/stop", {}
                    )
                    # Cloud acknowledgement may precede the speaker closing
                    # its socket. Let it finish while silence keeps WAV valid.
                    for _ in range(10):
                        if not self.stream.has_clients:
                            break
                        await asyncio.sleep(0.05)
                    log.info("Speaker stopped before closing MiPlay HTTP audio")
        except Exception as error:
            self.last_error = type(error).__name__
            log.error("Speaker stop failed: %s", self.last_error)
        finally:
            if silence:
                silence.cancel()
                await asyncio.gather(silence, return_exceptions=True)
            self.retire()
            self.stopping = False

    async def keep_stop_alive(self):
        # The speaker treats a truncated/idle live WAV as a network failure.
        # Supply silence only while its normal stop command is in flight.
        while True:
            self.stream.write_pcm(bytes(4800 * 4))
            await asyncio.sleep(0.1)

    def handle(self, event):
        kind = event.get("event")
        sid = event.get("stream_id", 0)
        if kind == "bridge_open":
            if sid > self.source_id:
                self.request_stop()
                self.source_id = sid
                self.stage = "audio_decoder_ready"
        elif kind == "bridge_pcm":
            if sid != self.source_id or not sid:
                return
            if self.stopping:
                return
            if time.monotonic() < self.retry_after:
                return
            data = decode_pcm(event)
            self.last_pcm = time.monotonic()
            if not self.active:
                self.active = True
                self.revision += 1
                self.stream.start_streaming()
                task = asyncio.create_task(
                    self.play(self.revision, self.stream.stream_url)
                )
                self.pending.add(task)
                task.add_done_callback(self.pending.discard)
                log.info(
                    "Receiving decoded PCM: 48 kHz stereo; starting speaker bridge"
                )
            self.frames += len(data) // 4
            self.stream.write_pcm(data)
            self.stage = "receiving_pcm"
        elif kind == "bridge_closed":
            if sid == self.source_id:
                self.request_stop()
                self.stage = "disconnected"
        elif kind == "receiver_ready":
            self.ready = True
            self.stage = "discoverable_pending_phone_test"
            log.info("Receiver ready: %s", self.config["name"])
        elif kind == "discovery_query":
            self.discovery_queries += 1
        elif kind in (
            "status",
            "interconnect_stage",
            "stream_started",
            "stream_stopped",
            "playback_state",
            "error",
            "warning",
        ):
            # Never log payloads, credentials, identities, audio bytes, or tokens.
            details = {
                key: event[key]
                for key in ("event", "state", "stage", "code", "playing")
                if key in event
            }
            log.info("Protocol: %s", json.dumps(details, ensure_ascii=False))
            if kind in ("status", "interconnect_stage"):
                self.stage = str(event.get("stage", event.get("state", self.stage)))
            if kind == "error":
                self.last_error = str(event.get("code", "receiver_error"))
            if kind == "playback_state" and event.get("playing") is False:
                self.request_stop()

    async def health(self, request):
        return web.json_response(
            {
                "ready": self.ready,
                "stage": self.stage,
                "pcm_frames": self.frames,
                "audio_clients": self.stream.client_count,
                "active": self.active,
                "stopping": self.stopping,
                "last_error": self.last_error,
                "discovery_queries": self.discovery_queries,
            },
            status=200 if self.ready else 503,
        )

    async def watchdog(self):
        while True:
            await asyncio.sleep(0.5)
            if self.active and time.monotonic() - self.last_pcm > 2.5:
                self.request_stop()

    async def run(self):
        identity_path = (
            Path(os.environ.get("MIPLAY_STATE_DIR", "/state")) / "identity.json"
        )
        if not identity_path.exists():
            identity_path.write_text(
                json.dumps(
                    {
                        "idm": str(uuid.uuid4()),
                        "lyra": secrets.token_hex(4).upper(),
                        "media": secrets.token_hex(4).upper(),
                    }
                ),
                encoding="utf-8",
            )
            identity_path.chmod(0o600)
        identity = json.loads(identity_path.read_text(encoding="utf-8"))
        env = dict(
            os.environ,
            MIPLAY_HOST=self.config["host"],
            MIPLAY_NAME=self.config["name"],
            MIPLAY_INTERFACE=self.config["interface"],
            MIPLAY_IDM_ID=identity["idm"],
            MIPLAY_LYRA_ID=identity["lyra"],
            MIPLAY_MEDIA_ID=identity["media"],
        )
        self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12))
        runner = None
        process = None
        watchdog = None
        try:
            await self.stream.start()
            health_app = web.Application()
            health_app.router.add_get("/health", self.health)
            runner = web.AppRunner(health_app, access_log=None)
            await runner.setup()
            await web.TCPSite(
                runner, "127.0.0.1", self.config.get("health_port", 18301)
            ).start()
            process = await asyncio.create_subprocess_exec(
                "/usr/local/bin/miair-miplay-receiver",
                stdout=asyncio.subprocess.PIPE,
                env=env,
                limit=1024 * 1024,
            )
            watchdog = asyncio.create_task(self.watchdog())
            while line := await process.stdout.readline():
                self.handle(json.loads(line))
            raise RuntimeError("MiPlay receiver exited")
        finally:
            self.ready = False
            if watchdog:
                watchdog.cancel()
            if process and process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
            self.request_stop()
            # Keep the HTTP session and consumer alive until stop is accepted.
            await asyncio.gather(*list(self.pending), return_exceptions=True)
            self.retire()
            tasks = [*self.pending, *([watchdog] if watchdog else [])]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.stream.stop()
            if runner:
                await runner.cleanup()
            await self.http.close()


async def main():
    config = load_config(os.environ.get("MIPLAY_CONFIG", "/run/miplay/config.json"))
    current = asyncio.current_task()
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, current.cancel)
    await Bridge(config).run()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    # HTTP request paths contain private stream session tokens.
    logging.getLogger("aiohttp.access").disabled = True
    try:
        asyncio.run(main())
    except asyncio.CancelledError:
        pass
