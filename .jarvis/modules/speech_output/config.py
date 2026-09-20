import os
import shlex
from dataclasses import dataclass
from pathlib import Path


JARVIS_DIR = Path(__file__).resolve().parents[2]
PROJECT_ROOT = JARVIS_DIR.parent


def _path(value, default):
    path = Path(value or default).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def _bool(name, default):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class Config:
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


def load_config():
    return Config(
        tts_url=os.environ.get("TTS_URL", "http://127.0.0.1:3030/generate"),
        tts_voice=os.environ.get("TTS_VOICE", "jarvis"),
        tts_voice_dir=_path(os.environ.get("TTS_VOICE_DIR"), "assets/voices"),
        tts_autostart=_bool("TTS_AUTOSTART", True),
        tts_server_bin=os.environ.get("TTS_SERVER_BIN", "s2").strip(),
        tts_model=_path(os.environ.get("TTS_MODEL"), "~/.models/s2-pro/s2-pro-q4_k_m.gguf"),
        tts_tokenizer=_path(os.environ.get("TTS_TOKENIZER"), "~/.models/s2-pro/tokenizer.json"),
        tts_server_args=shlex.split(os.environ.get("TTS_SERVER_ARGS", "--cuda 0 -ngl -1")),
        tts_reference=_path(os.environ.get("TTS_REFERENCE"), "assets/voices/jarvis_reference.mp3"),
        tts_reference_text=_path(os.environ.get("TTS_REFERENCE_TEXT"), "assets/voices/jarvis_reference.txt"),
    )
