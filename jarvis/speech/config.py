"""Speech defaults and environment configuration; assets are package resources."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path


PACKAGE_DIR = Path(__file__).resolve().parent
ASSETS_DIR = PACKAGE_DIR / "assets"
VOICES_DIR = ASSETS_DIR / "voices"

def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    if value.lower() not in {"true", "false", "1", "0"}:
        raise ValueError(f"{name} must be true or false")
    return value.lower() in {"true", "1"}


TTS_ENABLED = _bool("TTS_ENABLED", True)
TTS_URL = os.getenv("TTS_URL", "http://127.0.0.1:3030/generate")
TTS_VOICE = os.getenv("TTS_VOICE", "jarvis")
TTS_AUTOSTART = _bool("TTS_AUTOSTART", True)
TTS_SERVER_BIN = os.getenv("TTS_SERVER_BIN", "s2")
TTS_MODEL = Path(os.getenv("TTS_MODEL", "~/.models/s2-pro/s2-pro-q4_k_m.gguf"))
TTS_TOKENIZER = Path(os.getenv("TTS_TOKENIZER", "~/.models/s2-pro/tokenizer.json"))
TTS_SERVER_ARGS = json.loads(os.getenv("TTS_SERVER_ARGS", '["--cuda", "0", "-ngl", "-1"]'))
if not isinstance(TTS_SERVER_ARGS, list) or not all(isinstance(arg, str) for arg in TTS_SERVER_ARGS):
    raise ValueError("TTS_SERVER_ARGS must be a JSON array of strings")
TTS_REFERENCE = VOICES_DIR / "jarvis_reference.mp3"
TTS_REFERENCE_TEXT = VOICES_DIR / "jarvis_reference.txt"
TTS_START_BUFFER_SECONDS = 0.35

STT_ENABLED = _bool("STT_ENABLED", True)
STT_MODEL = os.getenv("STT_MODEL", "large-v3-turbo")
STT_DEVICE = os.getenv("STT_DEVICE", "cuda")
STT_COMPUTE_TYPE: str | None = os.getenv("STT_COMPUTE_TYPE") or None
STT_LANGUAGE: str | None = os.getenv("STT_LANGUAGE") or None
STT_LANGUAGES = tuple(item.strip() for item in os.getenv("STT_LANGUAGES", "ru,en").split(",") if item.strip())
if not STT_LANGUAGES:
    raise ValueError("STT_LANGUAGES must contain at least one language")
STT_THRESHOLD = 0.5
STT_PRE_ROLL = 0.5
STT_CHUNK_SILENCE = 2.0
STT_KEEP_AUDIO = False
STT_INPUT_DEVICE: str | None = os.getenv("STT_INPUT_DEVICE") or None


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
    project_root = Path(os.getenv("JARVIS_PROJECT_ROOT", str(Path.cwd())))
    def resolve(path):
        expanded = path.expanduser()
        return expanded if expanded.is_absolute() else project_root / expanded
    return Config(
        tts_url=TTS_URL,
        tts_voice=TTS_VOICE,
        tts_voice_dir=runtime_dir / "voices",
        tts_autostart=TTS_AUTOSTART,
        tts_server_bin=TTS_SERVER_BIN,
        tts_model=resolve(TTS_MODEL),
        tts_tokenizer=resolve(TTS_TOKENIZER),
        tts_server_args=list(TTS_SERVER_ARGS),
        tts_reference=TTS_REFERENCE,
        tts_reference_text=TTS_REFERENCE_TEXT,
        tts_start_buffer_seconds=TTS_START_BUFFER_SECONDS,
        runtime_dir=runtime_dir,
    )
