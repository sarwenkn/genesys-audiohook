import asyncio
import uuid
import json
import time
import websockets
import tempfile
import logging
import importlib
import os
import wave
from datetime import datetime, timezone
from websockets.exceptions import ConnectionClosed

from config import (
    RATE_LIMIT_MAX_RETRIES,
    GENESYS_MSG_RATE_LIMIT,
    GENESYS_BINARY_RATE_LIMIT,
    GENESYS_MSG_BURST_LIMIT,
    GENESYS_BINARY_BURST_LIMIT,
    MAX_AUDIO_BUFFER_SIZE,
    SUPPORTED_LANGUAGES,
    DEFAULT_SPEECH_PROVIDER,
    DEBUG_SAVE_AUDIO,
    DEBUG_AUDIO_DIR,
)
from rate_limiter import RateLimiter
from utils import format_json, parse_iso8601_duration

from language_mapping import normalize_language_code
from google_gemini_translation import translate_with_gemini
from audio_processing import deinterleave_pcmu_frames, pcmu_to_pcm16
from daisy_client import DaisyClient
from agent_assist import AgentAssistEngine

from collections import deque
logger = logging.getLogger("AudioHookServer")

class AudioHookServer:
    def __init__(self, websocket, debug_hub=None):
        self.session_id = str(uuid.uuid4())
        self.ws = websocket
        self.debug_hub = debug_hub
        self.client_seq = 0
        self.server_seq = 0
        self.running = True
        self.negotiated_media = None
        self.start_time = time.time()
        self.logger = logger.getChild(f"AudioHookServer_{self.session_id}")
        self.audio_frames_sent = 0
        self.audio_frames_received = 0
        self.events_allowed = True
        self.rate_limit_state = {
            "retry_count": 0,
            "last_retry_time": 0,
            "in_backoff": False
        }

        self.message_limiter = RateLimiter(GENESYS_MSG_RATE_LIMIT, GENESYS_MSG_BURST_LIMIT)
        self.binary_limiter = RateLimiter(GENESYS_BINARY_RATE_LIMIT, GENESYS_BINARY_BURST_LIMIT)

        self.audio_buffer = deque(maxlen=MAX_AUDIO_BUFFER_SIZE)
        self.last_frame_time = 0
        self._last_debug_audio_ts = 0.0
        self._wav_writers = []
        self._wav_paths = []

        self.total_samples = 0
        self.offset_adjustment = 0
        self.pause_start_time = None

        self.input_language = "en-US"
        self.destination_language = "en-US"

        self.enable_translation = False
        
        self.speech_provider = DEFAULT_SPEECH_PROVIDER
        
        self.StreamingTranscription = None

        self.streaming_transcriptions = []
        self.process_responses_tasks = []

        self.conversation_id = None
        self.channel_speakers = {}
        self._last_assist_text_by_speaker = {}

        self.daisy = DaisyClient(self.logger)
        self.assist_engine = AgentAssistEngine(self.logger)

        self.logger.info(f"New session started: {self.session_id}")
        if self.debug_hub:
            asyncio.create_task(
                self.debug_hub.publish(
                    "session_start",
                    {
                        "session_id": self.session_id,
                        "remote_address": str(getattr(self.ws, "remote_address", "")),
                    },
                )
            )

    def _load_transcription_provider(self, provider_name=None):
        provider = provider_name or self.speech_provider
        provider = provider.lower()
        
        try:
            if provider in ('elevenlabs', 'elevenlabs_scribe', 'scribe'):
                module = importlib.import_module('elevenlabs_speech_transcription')
            elif provider == 'openai':
                module = importlib.import_module('openai_speech_transcription')
            else:  # default to google
                module = importlib.import_module('google_speech_transcription')
                
            self.StreamingTranscription = module.StreamingTranscription
            self.logger.info(f"Loaded transcription provider: {provider}")
        except ImportError as e:
            self.logger.error(f"Failed to load transcription provider '{provider}': {e}")
            if provider != 'google':
                self.logger.warning(f"Falling back to Google transcription provider")
                self._load_transcription_provider('google')
            else:
                raise

    async def handle_error(self, msg: dict):
        error_params = msg.get("parameters") or {}
        error_code_raw = error_params.get("code")
        try:
            error_code = int(error_code_raw) if error_code_raw is not None else None
        except (TypeError, ValueError):
            error_code = None

        if self.debug_hub:
            asyncio.create_task(
                self.debug_hub.publish(
                    "genesys_error",
                    {"session_id": self.session_id, "code": error_code_raw, "parameters": error_params},
                )
            )

        # Some AudioHook modes (e.g., Monitor) don't accept server->client event messages.
        # If Genesys tells us events aren't allowed, stop sending transcript events to avoid spamming errors.
        if error_code == 400:
            msg_text = str(error_params.get("message") or "")
            if "event messages are not allowed" in msg_text.lower():
                if self.events_allowed:
                    self.logger.warning("Genesys reports events are not allowed in this mode; disabling outgoing event messages.")
                    self.events_allowed = False
                    if self.debug_hub:
                        asyncio.create_task(
                            self.debug_hub.publish(
                                "events_disabled",
                                {"session_id": self.session_id, "reason": msg_text},
                            )
                        )
                return True

        if error_code == 429:
            retry_after = None

            if "retryAfter" in error_params:
                retry_after_duration = error_params["retryAfter"]
                try:
                    retry_after = parse_iso8601_duration(retry_after_duration)
                    self.logger.info(
                        f"[Rate Limit] Using Genesys-provided retryAfter duration: {retry_after}s "
                        f"(parsed from {retry_after_duration})"
                    )
                except ValueError as e:
                    self.logger.warning(
                        f"[Rate Limit] Failed to parse Genesys retryAfter format: {retry_after_duration}. "
                        f"Error: {str(e)}"
                    )

            if retry_after is None and hasattr(self.ws, 'response_headers'):
                http_retry_after = (
                    self.ws.response_headers.get('Retry-After') or 
                    self.ws.response_headers.get('retry-after')
                )
                if http_retry_after:
                    try:
                        retry_after = float(http_retry_after)
                        self.logger.info(
                            f"[Rate Limit] Using HTTP header Retry-After duration: {retry_after}s"
                        )
                    except ValueError:
                        try:
                            retry_after = parse_iso8601_duration(http_retry_after)
                            self.logger.info(
                                f"[Rate Limit] Using HTTP header Retry-After duration: {retry_after}s "
                                f"(parsed from ISO8601)"
                            )
                        except ValueError:
                            self.logger.warning(
                                f"[Rate Limit] Failed to parse HTTP Retry-After format: {http_retry_after}"
                            )

            self.logger.warning(
                f"[Rate Limit] Received 429 error. "
                f"Session: {self.session_id}, "
                f"Current duration: {time.time() - self.start_time:.2f}s, "
                f"Retry count: {self.rate_limit_state['retry_count']}, "
                f"RetryAfter: {retry_after}s"
            )

            self.rate_limit_state["in_backoff"] = True
            self.rate_limit_state["retry_count"] += 1

            if self.rate_limit_state["retry_count"] > RATE_LIMIT_MAX_RETRIES:
                self.logger.error(
                    f"[Rate Limit] Max retries ({RATE_LIMIT_MAX_RETRIES}) exceeded. "
                    f"Session: {self.session_id}, "
                    f"Total retries: {self.rate_limit_state['retry_count']}, "
                    f"Duration: {time.time() - self.start_time:.2f}s"
                )
                await self.disconnect_session(reason="error", info="Rate limit max retries exceeded")
                return False

            self.logger.warning(
                f"[Rate Limit] Rate limited, attempt {self.rate_limit_state['retry_count']}/{RATE_LIMIT_MAX_RETRIES}. "
                f"Backing off for {retry_after if retry_after is not None else 'default delay'}s. "
                f"Session: {self.session_id}, "
                f"Duration: {time.time() - self.start_time:.2f}s"
            )

            await asyncio.sleep(retry_after if retry_after is not None else 3)
            self.rate_limit_state["in_backoff"] = False
            self.logger.info(
                f"[Rate Limit] Backoff complete, resuming operations. "
                f"Session: {self.session_id}"
            )

            return True
        return False

    async def handle_message(self, msg: dict):
        msg_type = msg.get("type")
        seq = msg.get("seq", 0)
        self.client_seq = seq

        if self.rate_limit_state.get("in_backoff") and msg_type != "error":
            self.logger.debug(f"Skipping message type {msg_type} during rate limit backoff")
            return

        if msg_type == "error":
            handled = await self.handle_error(msg)
            if handled:
                return

        if msg_type == "open":
            await self.handle_open(msg)
        elif msg_type == "ping":
            await self.handle_ping(msg)
        elif msg_type == "close":
            await self.handle_close(msg)
        elif msg_type == "discarded":
            await self.handle_discarded(msg)
        elif msg_type == "paused":
            await self.handle_paused(msg)
        elif msg_type == "resumed":
            await self.handle_resumed(msg)
        elif msg_type in ["update"]:
            self.logger.debug(f"Ignoring message type {msg_type}")
        else:
            self.logger.debug(f"Ignoring unknown message type: {msg_type}")

    async def handle_discarded(self, msg: dict):
        discarded_duration_str = msg["parameters"].get("discarded")
        if discarded_duration_str:
            try:
                gap = parse_iso8601_duration(discarded_duration_str)
                gap_samples = int(gap * 8000)  # assuming 8kHz sample rate
                self.offset_adjustment += gap_samples
                self.logger.info(f"Handled 'discarded' message: gap duration {gap}s, adding {gap_samples} samples to offset adjustment.")
            except ValueError as e:
                self.logger.warning(f"Failed to parse discarded duration '{discarded_duration_str}': {e}")
        else:
            self.logger.warning("Received 'discarded' message without 'discarded' parameter.")

    async def handle_paused(self, msg: dict):
        if self.pause_start_time is None:
            self.pause_start_time = time.time()
            self.logger.info("Handled 'paused' message: pause started.")
        else:
            self.logger.warning("Received 'paused' message while already paused.")

    async def handle_resumed(self, msg: dict):
        if self.pause_start_time is not None:
            pause_duration = time.time() - self.pause_start_time
            gap_samples = int(pause_duration * 8000)  # assuming 8kHz sample rate
            self.offset_adjustment += gap_samples
            self.logger.info(f"Handled 'resumed' message: pause duration {pause_duration:.2f}s, adding {gap_samples} samples to offset adjustment.")
            self.pause_start_time = None
        else:
            self.logger.warning("Received 'resumed' message without a preceding 'paused' event.")

    async def handle_open(self, msg: dict):
        self.session_id = msg["id"]

        custom_config = msg["parameters"].get("customConfig", {})

        self.conversation_id = msg["parameters"].get("conversationId")

        self.input_language = normalize_language_code(custom_config.get("inputLanguage", "en-US"))

        self.enable_translation = custom_config.get("enableTranslation", False)

        self.destination_language = normalize_language_code(msg["parameters"].get("language", "en-US"))
        
        self.speech_provider = custom_config.get("transcriptionVendor", DEFAULT_SPEECH_PROVIDER)

        # Optional channel speaker mapping: ["customer","agent"] or {"0":"customer","1":"agent"}
        self.channel_speakers = {}
        ch_map = custom_config.get("channelSpeakers")
        if isinstance(ch_map, list):
            for idx, spk in enumerate(ch_map):
                self.channel_speakers[idx] = str(spk)
        elif isinstance(ch_map, dict):
            for k, v in ch_map.items():
                try:
                    self.channel_speakers[int(k)] = str(v)
                except Exception:
                    continue
        
        self._load_transcription_provider(self.speech_provider)

        is_probe = (
            msg["parameters"].get("conversationId") == "00000000-0000-0000-0000-000000000000" and
            msg["parameters"].get("participant", {}).get("id") == "00000000-0000-0000-0000-000000000000"
        )

        if is_probe:
            self.logger.info("Detected probe connection")
            supported_langs = [lang.strip() for lang in SUPPORTED_LANGUAGES.split(",")]
            opened_msg = {
                "version": "2",
                "type": "opened",
                "seq": self.server_seq + 1,
                "clientseq": self.client_seq,
                "id": self.session_id,
                "parameters": {
                    "startPaused": False,
                    "media": [],
                    "supportedLanguages": supported_langs
                }
            }
            if await self._send_json(opened_msg):
                self.server_seq += 1
            else:
                await self.disconnect_session(reason="error", info="Failed to send opened message")
                return
            return

        offered_media = msg["parameters"].get("media", [])
        chosen = None
        for m in offered_media:
            if (m.get("format") == "PCMU" and m.get("rate") == 8000):
                chosen = m
                break

        if not chosen:
            resp = {
                "version": "2",
                "type": "disconnect",
                "seq": self.server_seq + 1,
                "clientseq": self.client_seq,
                "id": self.session_id,
                "parameters": {
                    "reason": "error",
                    "info": "No supported format found"
                }
            }
            if await self._send_json(resp):
                self.server_seq += 1
            else:
                await self.disconnect_session(reason="error", info="Failed to send disconnect message")
                return
            self.running = False
            return

        self.negotiated_media = chosen

        # Default speaker mapping if not provided.
        channels = len(self.negotiated_media.get("channels", [])) if self.negotiated_media and "channels" in self.negotiated_media else 1
        if channels == 0:
            channels = 1
        if not self.channel_speakers:
            if channels >= 2:
                self.channel_speakers = {0: "customer", 1: "agent"}
            else:
                self.channel_speakers = {0: "customer"}

        opened_msg = {
            "version": "2",
            "type": "opened",
            "seq": self.server_seq + 1,
            "clientseq": self.client_seq,
            "id": self.session_id,
            "parameters": {
                "startPaused": False,
                "media": [chosen]
            }
        }
        if await self._send_json(opened_msg):
            self.server_seq += 1
        else:
            await self.disconnect_session(reason="error", info="Failed to send opened message")
            return
        self.logger.info(f"Session opened. Negotiated media format: {chosen}")
        if self.debug_hub:
            asyncio.create_task(
                self.debug_hub.publish(
                    "opened",
                    {
                        "session_id": self.session_id,
                        "media": chosen,
                        "conversation_id": self.conversation_id,
                        "input_language": self.input_language,
                        "destination_language": self.destination_language,
                        "enable_translation": self.enable_translation,
                        "speech_provider": self.speech_provider,
                    },
                )
            )

        # Optional: save per-channel audio to WAV for debugging.
        if DEBUG_SAVE_AUDIO:
            try:
                os.makedirs(DEBUG_AUDIO_DIR, exist_ok=True)
                self._wav_writers = []
                self._wav_paths = []
                for ch in range(channels):
                    path = os.path.join(DEBUG_AUDIO_DIR, f"{self.session_id}_ch{ch}.wav")
                    wf = wave.open(path, "wb")
                    wf.setnchannels(1)
                    wf.setsampwidth(2)  # 16-bit PCM
                    wf.setframerate(8000)
                    self._wav_writers.append(wf)
                    self._wav_paths.append(path)

                if self.debug_hub:
                    asyncio.create_task(
                        self.debug_hub.publish(
                            "audio_recording",
                            {
                                "session_id": self.session_id,
                                "paths": list(self._wav_paths),
                                "sample_rate": 8000,
                                "format": "pcm_s16le_wav",
                            },
                        )
                    )
            except Exception as exc:
                self.logger.warning(f"Failed to set up WAV recording: {exc}")

        self.streaming_transcriptions = [self.StreamingTranscription(self.input_language, 1, self.logger) for _ in range(channels)]
        for transcription in self.streaming_transcriptions:
            transcription.start_streaming()
        self.process_responses_tasks = [asyncio.create_task(self.process_transcription_responses(channel)) for channel in range(channels)]

    async def handle_ping(self, msg: dict):
        pong_msg = {
            "version": "2",
            "type": "pong",
            "seq": self.server_seq + 1,
            "clientseq": self.client_seq,
            "id": self.session_id,
            "parameters": {}
        }
        if await self._send_json(pong_msg):
            self.server_seq += 1
        else:
            self.logger.error("Failed to send pong response")
            await self.disconnect_session(reason="error", info="Failed to send pong message")

    async def handle_close(self, msg: dict):
        self.logger.info(f"Received 'close' from Genesys. Reason: {msg['parameters'].get('reason')}")

        closed_msg = {
            "version": "2",
            "type": "closed",
            "seq": self.server_seq + 1,
            "clientseq": self.client_seq,
            "id": self.session_id,
            "parameters": {
                "summary": ""
            }
        }
        if await self._send_json(closed_msg):
            self.server_seq += 1
        else:
            self.logger.error("Failed to send closed response")
            await self.disconnect_session(reason="error", info="Failed to send closed message")

        duration = time.time() - self.start_time
        self.logger.info(
            f"Session stats - Duration: {duration:.2f}s, "
            f"Frames sent: {self.audio_frames_sent}, "
            f"Frames received: {self.audio_frames_received}"
        )

        self.running = False
        for transcription in self.streaming_transcriptions:
            transcription.stop_streaming()
        for task in self.process_responses_tasks:
            task.cancel()
        await self.daisy.close()

    async def disconnect_session(self, reason="completed", info=""):
        try:
            if not self.session_id:
                return

            disconnect_msg = {
                "version": "2",
                "type": "disconnect",
                "seq": self.server_seq + 1,
                "clientseq": self.client_seq,
                "id": self.session_id,
                "parameters": {
                    "reason": reason,
                    "info": info,
                    "outputVariables": {}
                }
            }
            if await self._send_json(disconnect_msg):
                self.server_seq += 1
            else:
                self.logger.error("Failed to send disconnect message")
            try:
                await asyncio.wait_for(self.ws.wait_closed(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning(f"Client did not acknowledge disconnect for session {self.session_id}")
        except Exception as e:
            self.logger.error(f"Error in disconnect_session: {e}")
        finally:
            self.running = False
            for wf in self._wav_writers:
                try:
                    wf.close()
                except Exception:
                    pass
            if self.debug_hub:
                asyncio.create_task(
                    self.debug_hub.publish(
                        "session_end",
                        {
                            "session_id": self.session_id,
                            "reason": reason,
                            "info": info,
                            "audio_paths": list(self._wav_paths),
                        },
                    )
                )
            for transcription in self.streaming_transcriptions:
                transcription.stop_streaming()
            for task in self.process_responses_tasks:
                task.cancel()
            await self.daisy.close()

    async def handle_audio_frame(self, frame_bytes: bytes):
        self.audio_frames_received += 1
        self.logger.debug(f"Received audio frame from Genesys: {len(frame_bytes)} bytes (frame #{self.audio_frames_received})")

        channels = len(self.negotiated_media.get("channels", [])) if self.negotiated_media and "channels" in self.negotiated_media else 1
        if channels == 0:
            channels = 1

        sample_times = len(frame_bytes) // channels
        self.total_samples += sample_times

        per_channel_pcmu = deinterleave_pcmu_frames(frame_bytes, channels)
        for idx in range(min(channels, len(self.streaming_transcriptions))):
            pcm16 = pcmu_to_pcm16(per_channel_pcmu[idx])
            self.streaming_transcriptions[idx].feed_audio(pcm16, 0)
            if idx < len(self._wav_writers):
                try:
                    self._wav_writers[idx].writeframes(pcm16)
                except Exception:
                    pass

        self.audio_buffer.append(frame_bytes)

        # Throttle debug UI updates (once per second max).
        if self.debug_hub:
            now = time.time()
            if now - self._last_debug_audio_ts >= 1.0:
                self._last_debug_audio_ts = now
                asyncio.create_task(
                    self.debug_hub.publish(
                        "audio_stats",
                        {
                            "session_id": self.session_id,
                            "frames_received": self.audio_frames_received,
                            "bytes_last_frame": len(frame_bytes),
                            "total_samples": self.total_samples,
                        },
                    )
                )

    async def process_transcription_responses(self, channel):
        while self.running:
            response = self.streaming_transcriptions[channel].get_response(0)  # Each instance handles 1 channel
            if response:
                self.logger.info(f"Processing transcription response on channel {channel}: {response}")
                if isinstance(response, Exception):
                    self.logger.error(f"Streaming recognition error on channel {channel}: {response}")
                    if self.debug_hub:
                        asyncio.create_task(
                            self.debug_hub.publish(
                                "stt_error",
                                {"session_id": self.session_id, "channel": channel, "error": str(response)},
                            )
                        )
                    await self.disconnect_session(reason="error", info="Streaming recognition failed")
                    break
                for result in response.results:
                    if not result.alternatives:
                        continue
                    alt = result.alternatives[0]
                    transcript_text = alt.transcript
                    source_lang = self.input_language
                    if self.enable_translation:
                        dest_lang = self.destination_language
                        translated_text = await translate_with_gemini(transcript_text, source_lang, dest_lang, self.logger)
                        if translated_text is None:
                            self.logger.warning(f"Translation failed for text: '{transcript_text}'. Skipping transcription event.")
                            continue  # Skip sending the event if translation failed
                    else:
                        dest_lang = source_lang
                        translated_text = transcript_text

                    speaker = self.channel_speakers.get(channel, f"channel_{channel}")
                    conversation_id = self.conversation_id or self.session_id
                    timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

                    if self.debug_hub and translated_text:
                        asyncio.create_task(
                            self.debug_hub.publish(
                                "transcript",
                                {
                                    "session_id": self.session_id,
                                    "channel": channel,
                                    "speaker": speaker,
                                    "is_final": bool(getattr(result, "is_final", False)),
                                    "text": translated_text,
                                    "timestamp": timestamp,
                                },
                            )
                        )

                    # Push transcript to DAISY asynchronously (non-blocking).
                    if self.daisy.enabled() and translated_text:
                        asyncio.create_task(
                            self.daisy.send_transcript(
                                conversation_id=conversation_id,
                                speaker=speaker,
                                text=translated_text,
                                timestamp=timestamp,
                                is_final=getattr(result, "is_final", None),
                            )
                        )

                    # Agent assist (rules-based) on customer final transcripts by default.
                    if speaker == "customer" and getattr(result, "is_final", False):
                        last = self._last_assist_text_by_speaker.get(speaker)
                        if translated_text and translated_text != last:
                            self._last_assist_text_by_speaker[speaker] = translated_text
                            assist = self.assist_engine.analyze(translated_text)
                            if assist and self.daisy.enabled():
                                asyncio.create_task(
                                    self.daisy.send_suggestion(
                                        conversation_id=conversation_id,
                                        intent=assist.intent,
                                        suggested_reply=assist.suggested_reply,
                                    )
                                )
    
                    adjustment_seconds = self.offset_adjustment / 8000.0
                    
                    default_confidence = 1.0
                    
                    use_word_timings = hasattr(alt, "words") and alt.words and len(alt.words) > 0 and all(
                        hasattr(w, "start_offset") and w.start_offset is not None for w in alt.words
                    )
                    
                    if use_word_timings:
                        overall_start = alt.words[0].start_offset.total_seconds()
                        overall_end = alt.words[-1].end_offset.total_seconds()
                        overall_duration = overall_end - overall_start
                    else:
                        self.logger.warning("No word-level timings found, using fallback")
                        overall_start = (self.total_samples - self.offset_adjustment) / 8000.0
                        overall_duration = 1.0  # Default duration
    
                    overall_start -= adjustment_seconds
                    
                    if overall_start < 0:
                        overall_start = 0
                    
                    offset_str = f"PT{overall_start:.2f}S"
                    duration_str = f"PT{overall_duration:.2f}S"
    
                    overall_confidence = default_confidence
                    
                    if hasattr(alt, "confidence") and alt.confidence is not None and alt.confidence > 0.0:
                        overall_confidence = alt.confidence
                    
                    if self.enable_translation:
                        words_list = translated_text.split()
                        if words_list and overall_duration > 0:
                            per_word_duration = overall_duration / len(words_list)
                            tokens = []
                            for i, word in enumerate(words_list):
                                token_offset = overall_start + i * per_word_duration
                                confidence = overall_confidence
                                tokens.append({
                                    "type": "word",
                                    "value": word,
                                    "confidence": confidence,
                                    "offset": f"PT{token_offset:.2f}S",
                                    "duration": f"PT{per_word_duration:.2f}S",
                                    "language": dest_lang
                                })
                        else:
                            tokens = [{
                                "type": "word",
                                "value": translated_text,
                                "confidence": overall_confidence,
                                "offset": offset_str,
                                "duration": duration_str,
                                "language": dest_lang
                            }]
                    else:
                        if use_word_timings:
                            tokens = []
                            for w in alt.words:
                                token_offset = w.start_offset.total_seconds() - adjustment_seconds
                                token_duration = w.end_offset.total_seconds() - w.start_offset.total_seconds()
                                
                                if token_offset < 0:
                                    token_offset = 0
                                
                                word_confidence = default_confidence
                                if hasattr(w, "confidence") and w.confidence is not None and w.confidence > 0.0:
                                    word_confidence = w.confidence
                                    
                                tokens.append({
                                    "type": "word",
                                    "value": w.word,
                                    "confidence": word_confidence,
                                    "offset": f"PT{token_offset:.2f}S",
                                    "duration": f"PT{token_duration:.2f}S",
                                    "language": dest_lang
                                })
                        else:
                            tokens = [{
                                "type": "word",
                                "value": transcript_text,
                                "confidence": overall_confidence,
                                "offset": offset_str,
                                "duration": duration_str,
                                "language": dest_lang
                            }]
    
                    alternative = {
                        "confidence": overall_confidence,
                        **({"languages": [dest_lang]} if self.enable_translation else {}),
                        "interpretations": [
                            {
                                "type": "display",
                                "transcript": translated_text,
                                "tokens": tokens
                            }
                        ]
                    }
    
                    channel_id = channel  # Integer channel index
    
                    transcript_event = {
                        "version": "2",
                        "type": "event",
                        "seq": self.server_seq + 1,
                        "clientseq": self.client_seq,
                        "id": self.session_id,
                        "parameters": {
                            "entities": [
                                {
                                    "type": "transcript",
                                    "data": {
                                        "id": str(uuid.uuid4()),
                                        "channelId": channel_id,
                                        "isFinal": result.is_final,
                                        "offset": offset_str,
                                        "duration": duration_str,
                                        "alternatives": [
                                            alternative
                                        ]
                                    }
                                }
                            ]
                        }
                    }
                    self.logger.info(f"Sending transcription event to Genesys: {json.dumps(transcript_event)}")
                    if await self._send_json(transcript_event):
                        self.server_seq += 1
                    else:
                        self.logger.debug("Transcript event dropped due to rate limiting")
                else:
                    self.logger.debug("Skipping transcript event because events are disabled for this session")
            else:
                await asyncio.sleep(0.01)    

    async def _send_json(self, msg: dict):
        try:
            # If Genesys is in a mode that doesn't accept server->client events (transcripts),
            # avoid sending any "event" messages once we know they're rejected.
            if msg.get("type") == "event" and not self.events_allowed:
                self.logger.debug("Dropping outgoing event message because events are disabled for this session")
                return False

            if not await self.message_limiter.acquire():
                current_rate = self.message_limiter.get_current_rate()
                self.logger.warning(
                    f"Message rate limit exceeded (current rate: {current_rate:.2f}/s). "
                    f"Message type: {msg.get('type')}. Dropping to maintain compliance."
                )
                return False  # Message not sent

            self.logger.debug(f"Sending message to Genesys:\n{format_json(msg)}")
            await self.ws.send(json.dumps(msg))
            return True  # Message sent
        except ConnectionClosed:
            self.logger.warning("Genesys WebSocket closed while sending JSON message.")
            self.running = False
            return False
        except Exception as e:
            self.logger.error(f"Error sending message: {e}")
            return False
