"""Personal CUDA speech configuration; edit constants here, not in .env."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


PACKAGE_DIR = Path(__file__).resolve().parent
ASSETS_DIR = PACKAGE_DIR / "assets"
VOICES_DIR = ASSETS_DIR / "voices"

TTS_ENABLED = True
TTS_URL = "http://127.0.0.1:3030/generate"
TTS_VOICE = "jarvis"
TTS_AUTOSTART = True
TTS_SERVER_BIN = "s2"
TTS_MODEL = Path("fish-speech/s2-pro-q4_k_m.gguf")
TTS_TOKENIZER = Path("fish-speech/tokenizer.json")
TTS_SERVER_ARGS = ["--cuda", "0", "-ngl", "-1"]
TTS_REFERENCE = VOICES_DIR / "jarvis_reference.mp3"
TTS_REFERENCE_TEXT = VOICES_DIR / "jarvis_reference.txt"
TTS_START_BUFFER_SECONDS = 0.35
TTS_GAIN = 2.0

STT_ENABLED = True
STT_MODEL = "large-v3-turbo"
STT_DEVICE = "cuda"
STT_COMPUTE_TYPE = "float16"
STT_LANGUAGE: str | None = None
STT_LANGUAGES = ("ru", "en")
STT_THRESHOLD = 0.5
STT_PRE_ROLL = 0.5
STT_CHUNK_SILENCE = 2.0
STT_KEEP_AUDIO = False
STT_INPUT_DEVICE: str | None = None


@dataclass(frozen=True, slots=True)
class Config:
    """Полная конфигурация TTS для конкретного запуска Jarvis."""

    tts_url: str
    tts_voice: str
    tts_voice_dir: Path
    tts_autostart: bool
    tts_server_bin: str
    tts_model: Path
    tts_tokenizer: Path
    tts_server_args: list[str]
    tts_reference: Path
    tts_reference_text: Path
    tts_start_buffer_seconds: float
    runtime_dir: Path


def build_config(jarvis_dir: Path) -> Config:
    runtime_dir = Path(jarvis_dir) / "runtime"
    return Config(
        tts_url=TTS_URL,
        tts_voice=TTS_VOICE,
        tts_voice_dir=runtime_dir / "voices",
        tts_autostart=TTS_AUTOSTART,
        tts_server_bin=TTS_SERVER_BIN,
        tts_model=runtime_dir / "models" / TTS_MODEL,
        tts_tokenizer=runtime_dir / "models" / TTS_TOKENIZER,
        tts_server_args=list(TTS_SERVER_ARGS),
        tts_reference=TTS_REFERENCE,
        tts_reference_text=TTS_REFERENCE_TEXT,
        tts_start_buffer_seconds=TTS_START_BUFFER_SECONDS,
        runtime_dir=runtime_dir,
    )
