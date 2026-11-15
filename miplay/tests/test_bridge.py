import asyncio
import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiohttp
import bridge
from app.engine.audio_stream import AudioStreamServer


class FakeStream:
    def __init__(self, *args, **kwargs):
        self.data = []
        self.session = 0
        self.has_clients = False
        self.closed = False

    def set_audio_params(self, *args):
        pass

    def start_streaming(self):
        self.session += 1
        self.data = []
        self.closed = False

    def stop_streaming(self):
        self.data = []
        self.closed = True

    @property
    def stream_url(self):
        return f"http://speaker-stream/session-{self.session}"

    def write_pcm(self, data):
        self.data.append(data)

    def discard_queued_pcm(self):
        self.data.clear()


def pcm(sid, raw=b"\x01\x00\x02\x00"):
    return {
        "event": "bridge_pcm",
        "stream_id": sid,
        "pcm": base64.b64encode(raw).decode(),
    }


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        with patch.object(bridge, "AudioStreamServer", FakeStream):
            self.receiver = bridge.Bridge(
                {"host": "127.0.0.1", "speaker_did": "test", "name": "test"}
            )
        self.receiver.api = AsyncMock(return_value={"ok": True})

    async def settle(self):
        await asyncio.gather(*list(self.receiver.pending))

    async def test_no_speaker_command_until_actual_audio(self):
        self.receiver.handle({"event": "receiver_ready"})
        self.receiver.handle({"event": "bridge_open", "stream_id": 1})
        self.receiver.api.assert_not_called()
        self.receiver.handle(pcm(1))
        await self.settle()
        self.receiver.api.assert_awaited_once()

    async def test_replaced_session_rejects_old_pcm_and_close(self):
        self.receiver.handle({"event": "bridge_open", "stream_id": 1})
        self.receiver.handle(pcm(1))
        self.receiver.handle({"event": "bridge_open", "stream_id": 2})
        await self.settle()
        self.receiver.handle(pcm(2))
        self.receiver.handle(pcm(1))
        self.receiver.handle({"event": "bridge_closed", "stream_id": 1})
        await self.settle()
        self.assertTrue(self.receiver.active)
        self.assertEqual(len(self.receiver.stream.data), 1)
        self.assertEqual(self.receiver.api.await_count, 1)
        self.assertIn("session-2", self.receiver.api.call_args.args[1]["url"])

    async def test_resume_rotates_url_without_stopping_other_protocols(self):
        self.receiver.handle({"event": "bridge_open", "stream_id": 1})
        self.receiver.handle(pcm(1))
        await self.settle()
        self.receiver.request_stop()
        await self.settle()
        self.receiver.handle(pcm(1))
        await self.settle()
        calls = self.receiver.api.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(calls[0].args[1]["url"], calls[1].args[1]["url"])
        self.assertTrue(all(c.args[0].endswith("/play_url") for c in calls))

    async def test_pause_stops_consumer_before_closing_http(self):
        self.receiver.handle({"event": "bridge_open", "stream_id": 1})
        self.receiver.handle(pcm(1))
        await self.settle()
        self.receiver.stream.has_clients = True

        async def stop(path, payload):
            self.assertTrue(path.endswith("/stop"))
            self.assertFalse(self.receiver.stream.closed)
            await asyncio.sleep(0)
            self.assertEqual(self.receiver.stream.data[-1], bytes(4800 * 4))
            return {"ok": True}

        self.receiver.api.side_effect = stop
        self.receiver.handle({"event": "playback_state", "playing": False})
        self.receiver.handle({"event": "bridge_closed", "stream_id": 1})
        await self.settle()
        self.assertTrue(self.receiver.stream.closed)
        self.assertFalse(self.receiver.stopping)
        self.assertEqual(self.receiver.api.await_count, 2)

    async def test_short_pause_resumes_without_cloud_roundtrip(self):
        self.receiver.handle({"event": "bridge_open", "stream_id": 1})
        self.receiver.handle(pcm(1))
        await self.settle()
        self.receiver.stream.has_clients = True
        self.receiver.handle({"event": "playback_state", "playing": False})
        self.assertEqual(self.receiver.stream.data, [bridge.SILENCE_PCM])
        self.receiver.handle(pcm(1))
        self.assertEqual(self.receiver.stream.data, [bridge.SILENCE_PCM])
        # The measured pause between user gestures was over 1.7 seconds.
        self.receiver.paused_at -= 2
        monitor = asyncio.create_task(self.receiver.watchdog())
        try:
            await asyncio.sleep(0.08)
        finally:
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
        self.receiver.api.assert_awaited_once()
        self.receiver.handle({"event": "playback_state", "playing": True})
        self.receiver.handle(pcm(1))
        await self.settle()
        self.assertIsNone(self.receiver.paused_at)
        self.assertEqual(self.receiver.stream.session, 1)
        self.receiver.api.assert_awaited_once()

    async def test_explicit_media_open_overlaps_play_with_decoder_start(self):
        self.receiver.handle({"event": "status", "state": "media_opening"})
        self.receiver.api.assert_not_called()
        self.receiver.handle({"event": "playback_state", "playing": True})
        await self.settle()
        self.receiver.api.assert_awaited_once()
        self.receiver.handle({"event": "bridge_open", "stream_id": 1})
        self.receiver.handle(pcm(1))
        await self.settle()
        self.assertEqual(self.receiver.stream.session, 1)
        self.assertTrue(self.receiver.stream.data)
        self.receiver.api.assert_awaited_once()

    async def test_volume_change_removes_samples_with_previous_gain(self):
        self.receiver.handle({"event": "bridge_open", "stream_id": 1})
        self.receiver.handle(pcm(1))
        await self.settle()
        self.receiver.handle({"event": "volume", "percent": 20})
        self.assertFalse(self.receiver.stream.data)
        self.receiver.handle(pcm(1))
        self.receiver.handle({"event": "volume", "percent": 20})
        self.assertEqual(len(self.receiver.stream.data), 1)

    async def test_missing_pull_retries_only_twice(self):
        self.receiver.handle({"event": "bridge_open", "stream_id": 1})
        monitor = asyncio.create_task(self.receiver.watchdog())
        try:
            for _ in range(4):
                self.receiver.handle(pcm(1))
                await self.settle()
                self.receiver.pull_deadline = 1
                await asyncio.sleep(0.12)
            self.assertEqual(self.receiver.api.await_count, 3)
            self.assertEqual(self.receiver.last_error, "NoAudioClient")
        finally:
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)

    async def test_resume_waits_for_pending_stop_and_uses_fresh_url(self):
        self.receiver.handle({"event": "bridge_open", "stream_id": 1})
        self.receiver.handle(pcm(1))
        await self.settle()
        old_url = self.receiver.stream.stream_url
        self.receiver.stream.has_clients = True
        entered = asyncio.Event()
        release = asyncio.Event()

        async def command(path, payload):
            if path.endswith("/stop"):
                entered.set()
                await release.wait()
            return {"ok": True}

        self.receiver.api.side_effect = command
        self.receiver.handle({"event": "bridge_closed", "stream_id": 1})
        await entered.wait()
        self.receiver.handle({"event": "bridge_open", "stream_id": 2})
        self.receiver.handle(pcm(2))
        self.assertEqual(self.receiver.stream.stream_url, old_url)
        release.set()
        await self.settle()
        self.receiver.handle(pcm(2))
        await self.settle()
        self.assertNotEqual(self.receiver.stream.stream_url, old_url)
        self.assertTrue(self.receiver.api.call_args.args[0].endswith("/play_url"))

    async def test_stop_failure_cleans_up_without_stuck_stopping_state(self):
        self.receiver.handle({"event": "bridge_open", "stream_id": 1})
        self.receiver.handle(pcm(1))
        await self.settle()
        self.receiver.stream.has_clients = True
        self.receiver.api.side_effect = RuntimeError("unavailable")
        self.receiver.request_stop()
        await self.settle()
        self.assertFalse(self.receiver.active)
        self.assertFalse(self.receiver.stopping)
        self.assertTrue(self.receiver.stream.closed)

    def test_malformed_pcm_is_rejected(self):
        for item in [pcm(1, b"abc"), {"pcm": "!not-base64!"}, {"pcm": "a" * 65537}]:
            with self.assertRaises(ValueError):
                bridge.decode_pcm(item)

    async def test_failed_command_backs_off_instead_of_retrying_each_frame(self):
        self.receiver.api.side_effect = RuntimeError("test failure")
        self.receiver.handle({"event": "bridge_open", "stream_id": 1})
        self.receiver.handle(pcm(1))
        await self.settle()
        self.assertFalse(self.receiver.active)
        for _ in range(20):
            self.receiver.handle(pcm(1))
        await self.settle()
        self.receiver.api.assert_awaited_once()

    async def test_startup_failure_closes_http_session(self):
        self.receiver.config["interface"] = "eth0"
        self.receiver.stream.start = AsyncMock(side_effect=OSError("occupied port"))
        self.receiver.stream.stop = AsyncMock()
        client = AsyncMock()
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"MIPLAY_STATE_DIR": directory}):
                with patch.object(bridge.aiohttp, "ClientSession", return_value=client):
                    with self.assertRaises(OSError):
                        await self.receiver.run()
        client.close.assert_awaited_once()
        self.receiver.stream.stop.assert_awaited_once()


class ConfigTests(unittest.TestCase):
    def read(self, **overrides):
        config = {
            "host": "192.168.1.10",
            "name": "MiPlay",
            "interface": "eth0",
            "api": "http://127.0.0.1:8300/api/v1/",
            "username": "tester",
            "password": "example-only",
            "speaker_did": "example-device",
        }
        config.update(overrides)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            return bridge.load_config(path)

    def test_defaults_and_custom_ports(self):
        self.assertEqual(self.read()["audio_port"], 18300)
        self.assertTrue(self.read()["api"].endswith("/api/v1"))
        self.assertEqual(self.read(audio_port=18400)["audio_port"], 18400)

    def test_invalid_network_settings_are_rejected(self):
        for kwargs in (
            {"host": "127.0.0.1"},
            {"audio_port": True},
            {"audio_port": 18301},
            {"api": "file:///secret"},
            {"api": "http://host/api/v1?token=secret"},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.read(**kwargs)

    def test_validation_does_not_echo_credentials(self):
        with self.assertRaises(ValueError) as error:
            self.read(api="http://tester:secret-value@example.org/api/v1")
        self.assertNotIn("secret-value", str(error.exception))


class HttpAudioTests(unittest.IsolatedAsyncioTestCase):
    async def test_short_preroll_and_contiguous_bursts_during_playback(self):
        stream = bridge.Bridge(
            {"host": "127.0.0.1", "audio_port": 0, "name": "test"}
        ).stream
        blocks = [bytes([i]) * 4096 for i in range(20)]
        await stream.start()
        try:
            stream.start_streaming()
            for block in blocks[:10]:
                stream.write_pcm(block)
            async with aiohttp.ClientSession() as session:
                async with session.get(stream.stream_url) as response:
                    reader = response.content
                    await asyncio.wait_for(reader.readexactly(44), 2)
                    self.assertEqual(
                        await asyncio.wait_for(reader.readexactly(8192), 2),
                        b"".join(blocks[8:10]),
                    )
                    for block in blocks[10:18]:
                        stream.write_pcm(block)
                    self.assertEqual(
                        await asyncio.wait_for(reader.readexactly(32768), 2),
                        b"".join(blocks[10:18]),
                    )
                    self.assertEqual(stream.playback_dropped_blocks, 0)
                    for block in blocks[10:]:
                        stream.write_pcm(block)
                    self.assertEqual(stream.playback_dropped_blocks, 2)
        finally:
            await stream.stop()

    async def test_wav_pcm_and_session_token_rotation(self):
        stream = AudioStreamServer(
            "127.0.0.1", route_prefix="miplay", isolate_sessions=True
        )
        stream.set_audio_params(48000, 2, 2)
        await stream.start()
        try:
            stream.start_streaming()
            old_url = stream.stream_url
            data = b"\x01\x00\x02\x00" * 32
            stream.write_pcm(data)
            async with aiohttp.ClientSession() as session:
                async with session.get(old_url) as response:
                    content = await asyncio.wait_for(
                        response.content.readexactly(44 + len(data)), 2
                    )
                    self.assertEqual(content[:4], b"RIFF")
                    self.assertEqual(content[44:], data)
                stream.start_streaming()
                async with session.get(old_url) as response:
                    self.assertEqual(response.status, 410)
                async with session.head(stream.stream_url) as response:
                    self.assertEqual(response.status, 200)
        finally:
            stream.stop_streaming()
            await stream.stop()


if __name__ == "__main__":
    unittest.main()
