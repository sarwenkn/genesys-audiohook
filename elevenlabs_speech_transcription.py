import asyncio
import audioop
import base64
import json
import queue
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import websockets

from config import (
    ELEVENLABS_API_KEY,
    ELEVENLABS_SCRIBE_LANG,
    ELEVENLABS_SCRIBE_START_MESSAGE_JSON,
    ELEVENLABS_SCRIBE_STREAM_MODE,
    ELEVENLABS_SCRIBE_WS_URL,
    ELEVENLABS_SCRIBE_TARGET_SAMPLE_RATE,
)


@dataclass
class Word:
    word: str
    start_offset: Optional[float] = None
    end_offset: Optional[float] = None
    confidence: Optional[float] = None


@dataclass
class Alternative:
    transcript: str
    confidence: Optional[float] = None
    words: List[Word] = field(default_factory=list)


@dataclass
class Result:
    alternatives: List[Alternative]
    is_final: bool = False


@dataclass
class MockResponse:
    results: List[Result]


class StreamingTranscription:
    """
    ElevenLabs Scribe v2 streaming adapter.

    Notes:
    - This is implemented as a WebSocket client running in a background thread
      to match the existing connector interface (feed_audio/get_response).
    - ElevenLabs message schema can vary; parsing is defensive and can be tuned
      via ELEVENLABS_SCRIBE_START_MESSAGE_JSON / ELEVENLABS_SCRIBE_STREAM_MODE.
    """

    def __init__(self, language: str, channels: int, logger):
        self.language = language
        self.channels = channels
        self.logger = logger

        self.audio_queues = [queue.Queue() for _ in range(channels)]
        self.response_queues = [queue.Queue() for _ in range(channels)]
        self.streaming_threads = [None] * channels
        self.running = True
        self._ratecv_states = [None] * channels

    def start_streaming(self):
        for channel in range(self.channels):
            t = threading.Thread(target=self._thread_main, args=(channel,), daemon=True)
            self.streaming_threads[channel] = t
            t.start()

    def stop_streaming(self):
        self.running = False
        for channel in range(self.channels):
            self.audio_queues[channel].put(None)
        for channel in range(self.channels):
            t = self.streaming_threads[channel]
            if t:
                t.join(timeout=2.0)

    def feed_audio(self, audio_stream: bytes, channel: int):
        """Feed audio data (PCM16) into the streaming queue for a specific channel."""
        if not audio_stream or channel >= self.channels:
            return
        self.audio_queues[channel].put(audio_stream)

    def get_response(self, channel: int):
        if channel >= self.channels:
            return None
        try:
            return self.response_queues[channel].get_nowait()
        except queue.Empty:
            return None

    def _thread_main(self, channel: int):
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(self._run_ws(channel))
            except asyncio.CancelledError:
                # Expected during shutdown / reconnect; don't print a traceback from the background thread.
                return
        finally:
            try:
                loop.stop()
            except Exception:
                pass
            loop.close()

    async def _run_ws(self, channel: int):
        if not ELEVENLABS_SCRIBE_WS_URL or not ELEVENLABS_API_KEY:
            self.response_queues[channel].put(
                RuntimeError("ElevenLabs not configured: set ELEVENLABS_SCRIBE_WS_URL and ELEVENLABS_API_KEY")
            )
            return

        headers = {"xi-api-key": ELEVENLABS_API_KEY}
        backoff = 0.25

        while self.running:
            try:
                ws_ctx = None
                try:
                    ws_ctx = websockets.connect(
                        ELEVENLABS_SCRIBE_WS_URL,
                        additional_headers=headers,  # websockets>=12
                        ping_interval=20,
                        ping_timeout=20,
                        max_size=2 * 1024 * 1024,
                    )
                except TypeError:
                    ws_ctx = websockets.connect(  # pragma: no cover
                        ELEVENLABS_SCRIBE_WS_URL,
                        extra_headers=headers,  # websockets<=11
                        ping_interval=20,
                        ping_timeout=20,
                        max_size=2 * 1024 * 1024,
                    )

                async with ws_ctx as ws:
                    await self._send_start(ws)
                    recv_task = asyncio.create_task(self._recv_loop(ws, channel))
                    try:
                        await self._send_audio_loop(ws, channel)
                    finally:
                        recv_task.cancel()
                        try:
                            await recv_task
                        except asyncio.CancelledError:
                            pass
                backoff = 0.25
            except Exception as exc:
                self.logger.warning(f"ElevenLabs WS error (channel={channel}): {exc}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 5.0)

    async def _send_start(self, ws):
        if not ELEVENLABS_SCRIBE_START_MESSAGE_JSON:
            return
        try:
            raw = ELEVENLABS_SCRIBE_START_MESSAGE_JSON
            if ELEVENLABS_SCRIBE_LANG:
                raw = raw.replace("{language}", ELEVENLABS_SCRIBE_LANG)
            else:
                raw = raw.replace("{language}", self.language)
            msg = json.loads(raw)
            await ws.send(json.dumps(msg))
        except Exception as exc:
            self.logger.warning(f"Failed to send ElevenLabs start message: {exc}")

    async def _send_audio_loop(self, ws, channel: int):
        loop = asyncio.get_running_loop()
        while self.running:
            pcm16_data = await loop.run_in_executor(None, self._queue_get, channel)
            if pcm16_data is None:
                return
            if pcm16_data == b"":
                await asyncio.sleep(0.005)
                continue

            # Genesys AudioHook media arrives at 8kHz. ElevenLabs realtime STT defaults to 16kHz PCM input,
            # so we resample to ELEVENLABS_SCRIBE_TARGET_SAMPLE_RATE (default 16000).
            if ELEVENLABS_SCRIBE_TARGET_SAMPLE_RATE and ELEVENLABS_SCRIBE_TARGET_SAMPLE_RATE != 8000:
                try:
                    converted, state = audioop.ratecv(
                        pcm16_data,
                        2,  # sample width bytes (16-bit PCM)
                        1,  # channels
                        8000,
                        ELEVENLABS_SCRIBE_TARGET_SAMPLE_RATE,
                        self._ratecv_states[channel],
                    )
                    self._ratecv_states[channel] = state
                    pcm16_data = converted
                except Exception as exc:
                    self.logger.warning(f"ElevenLabs resample failed (channel={channel}): {exc}")

            if ELEVENLABS_SCRIBE_STREAM_MODE in ("json_base64", "json"):
                # Preferred protocol for ElevenLabs realtime STT: send base64-encoded audio in JSON messages.
                payload = {
                    "message_type": "input_audio_chunk",
                    "audio_base_64": base64.b64encode(pcm16_data).decode("ascii"),
                    "sample_rate": ELEVENLABS_SCRIBE_TARGET_SAMPLE_RATE or 8000,
                }
                await ws.send(json.dumps(payload))
            else:
                # Fallback legacy mode (may not work with current ElevenLabs realtime STT endpoint).
                await ws.send(pcm16_data)

    def _queue_get(self, channel: int):
        try:
            return self.audio_queues[channel].get(timeout=0.1)
        except queue.Empty:
            return b""

    async def _recv_loop(self, ws, channel: int):
        try:
            while self.running:
                msg = await ws.recv()
                if isinstance(msg, bytes):
                    continue
                event = self._parse_transcript_event(msg)
                if event:
                    self.response_queues[channel].put(event)
        except asyncio.CancelledError:
            # Normal cancellation path when shutting down / reconnecting.
            return

    def _parse_transcript_event(self, msg: str) -> Optional[MockResponse]:
        try:
            data: Dict[str, Any] = json.loads(msg)
        except Exception:
            return None

        # ElevenLabs realtime STT messages typically use message_type + text.
        if isinstance(data.get("message_type"), str) and isinstance(data.get("text"), str):
            msg_type = data.get("message_type")
            text = data.get("text")
            is_final = msg_type in ("committed_transcript", "committed_transcript_with_timestamps")
            alt = Alternative(transcript=text, confidence=None)
            result = Result(alternatives=[alt], is_final=is_final)
            return MockResponse(results=[result])

        # Try a few common legacy shapes.
        text = None
        is_final = None
        confidence = None

        if isinstance(data.get("text"), str):
            text = data.get("text")
            is_final = bool(data.get("is_final", data.get("final", False)))
            confidence = data.get("confidence")
        elif isinstance(data.get("transcript"), dict):
            t = data["transcript"]
            text = t.get("text")
            is_final = bool(t.get("is_final", t.get("final", False)))
            confidence = t.get("confidence")
        elif isinstance(data.get("data"), dict) and isinstance(data["data"].get("text"), str):
            t = data["data"]
            text = t.get("text")
            is_final = bool(t.get("is_final", t.get("final", False)))
            confidence = t.get("confidence")

        if not text:
            return None

        alt = Alternative(transcript=text, confidence=confidence)
        result = Result(alternatives=[alt], is_final=bool(is_final))
        return MockResponse(results=[result])
