import os
from dotenv import load_dotenv

load_dotenv()  # load variables from .env

DEBUG = os.getenv('DEBUG', 'false').lower()
DEBUG_UI_TOKEN = os.getenv("DEBUG_UI_TOKEN")  # optional; if set, enables /debug UI protected by ?token=...
DEBUG_SAVE_AUDIO = os.getenv("DEBUG_SAVE_AUDIO", "false").lower() == "true"
DEBUG_AUDIO_DIR = os.getenv("DEBUG_AUDIO_DIR", "debug_audio")

# Audio buffering settings
MAX_AUDIO_BUFFER_SIZE = 50

# Server settings
GENESYS_LISTEN_HOST = os.getenv("GENESYS_LISTEN_HOST", "0.0.0.0")
GENESYS_LISTEN_PORT = os.getenv("GENESYS_LISTEN_PORT", "443")
GENESYS_PATH = os.getenv("GENESYS_PATH", "/audiohook")

# Google Cloud Speech-to-Text API settings
GOOGLE_CLOUD_PROJECT = os.getenv('GOOGLE_CLOUD_PROJECT')

# Centralized service account credentials JSON key.
GOOGLE_APPLICATION_CREDENTIALS = os.getenv('GOOGLE_APPLICATION_CREDENTIALS')

# New environment variable to set the speech recognition model.
GOOGLE_SPEECH_MODEL = os.getenv('GOOGLE_SPEECH_MODEL', 'chirp_2')

# New environment variables for Gemini API
GOOGLE_TRANSLATION_MODEL = os.getenv('GOOGLE_TRANSLATION_MODEL')

GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')

# OpenAI Speech-to-Text API settings
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
OPENAI_SPEECH_MODEL = os.getenv('OPENAI_SPEECH_MODEL', 'gpt-4o-mini-transcribe')

# Default speech recognition provider (used if not specified in customConfig)
DEFAULT_SPEECH_PROVIDER = os.getenv('DEFAULT_SPEECH_PROVIDER', 'elevenlabs').lower()  # 'elevenlabs', 'google', or 'openai'

# ElevenLabs Scribe v2 (Streaming STT) settings
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
ELEVENLABS_SCRIBE_WS_URL = os.getenv(
    "ELEVENLABS_SCRIBE_WS_URL",
    "wss://api.elevenlabs.io/v1/speech-to-text/realtime?model_id=scribe_v2_realtime&commit_strategy=vad&audio_format=pcm_16000",
)  # Optional override; defaults to ElevenLabs Realtime STT endpoint.
ELEVENLABS_SCRIBE_START_MESSAGE_JSON = os.getenv("ELEVENLABS_SCRIBE_START_MESSAGE_JSON")  # optional JSON string
ELEVENLABS_SCRIBE_STREAM_MODE = os.getenv("ELEVENLABS_SCRIBE_STREAM_MODE", "binary").lower()  # binary | json_base64
ELEVENLABS_SCRIBE_LANG = os.getenv("ELEVENLABS_SCRIBE_LANG")  # optional override (e.g. en)
ELEVENLABS_SCRIBE_TARGET_SAMPLE_RATE = int(os.getenv("ELEVENLABS_SCRIBE_TARGET_SAMPLE_RATE", "16000"))  # 16000 recommended

# Language policy
# - ALLOWED_STT_LANGUAGES limits which input languages are accepted from Genesys open messages.
# - This prevents accidental/unsupported languages being selected at runtime.
ALLOWED_STT_LANGUAGES = os.getenv("ALLOWED_STT_LANGUAGES", "en-US,ms-MY,zh-CN")

# Genesys API key and Organization ID
GENESYS_API_KEY = os.getenv('GENESYS_API_KEY')
if not GENESYS_API_KEY:
    raise ValueError("GENESYS_API_KEY not found in environment variables.")

GENESYS_ORG_ID = os.getenv('GENESYS_ORG_ID')
if not GENESYS_ORG_ID:
    raise ValueError("GENESYS_ORG_ID not found in environment variables.")

# If false, do not send "type=event" transcript messages back to Genesys.
# This is useful for AudioHook configurations that reject event messages (e.g., Monitor mode).
GENESYS_SEND_TRANSCRIPT_EVENTS = os.getenv("GENESYS_SEND_TRANSCRIPT_EVENTS", "true").lower() == "true"

# DAISY integration settings
DAISY_BASE_URL = os.getenv("DAISY_BASE_URL")  # e.g. https://daisy.example.com
DAISY_UPDATE_PATH = os.getenv("DAISY_UPDATE_PATH", "/live_transcription/update")
DAISY_API_KEY = os.getenv("DAISY_API_KEY")  # optional; if set sent as Authorization: Bearer
DAISY_TIMEOUT_SECS = float(os.getenv("DAISY_TIMEOUT_SECS", "2.0"))
DAISY_MAX_RETRIES = int(os.getenv("DAISY_MAX_RETRIES", "2"))
DAISY_RETRY_BASE_DELAY_SECS = float(os.getenv("DAISY_RETRY_BASE_DELAY_SECS", "0.25"))
DAISY_INCLUDE_IS_FINAL = os.getenv("DAISY_INCLUDE_IS_FINAL", "false").lower() == "true"

# Genesys rate limiting constants
GENESYS_MSG_RATE_LIMIT = 5
GENESYS_BINARY_RATE_LIMIT = 5
GENESYS_MSG_BURST_LIMIT = 25
GENESYS_BINARY_BURST_LIMIT = 25

# Rate limiting constants 
RATE_LIMIT_MAX_RETRIES = 3

# Transcription Connector language support
SUPPORTED_LANGUAGES = os.getenv("SUPPORTED_LANGUAGES", "en-US,ms-MY,zh-CN")
