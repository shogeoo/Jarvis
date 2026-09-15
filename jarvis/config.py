"""Загрузка конфигурации Jarvis.

Параметры LLM и TTS берутся из ``.env``, мастер-промпт — из текстового файла.
Пути можно переопределить в CLI. Относительные пути считаются от корня проекта.
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_PATH = ROOT / ".env"
DEFAULT_PROMPT_PATH = ROOT / "system_prompt.txt"
DEFAULT_SYSTEM = (
    "Ты — Jarvis, роботизированный голосовой ассистент. "
    "Отвечай кратко и по делу."
)

_TRUE = {"1", "true", "yes", "on"}


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in _TRUE


def _path(value: str | None, default: str | Path) -> Path:
    raw = value if value else str(default)
    path = Path(raw).expanduser()
    return path if path.is_absolute() else ROOT / path


@dataclass
class Config:
    # LLM (OpenAI-совместимый API)
    model: str | None
    base_url: str | None
    api_key: str | None
    system: str

    # TTS (Fish Audio S2 Pro через s2.cpp)
    tts_enabled: bool
    tts_url: str | None
    tts_voice: str
    tts_voice_dir: Path
    tts_autostart: bool
    tts_server_bin: Path
    tts_model: Path
    tts_tokenizer: Path
    tts_server_args: list[str]
    tts_reference: Path
    tts_reference_text: Path

    @property
    def llm_enabled(self) -> bool:
        return bool(self.model)


def read_system_prompt(path: str | os.PathLike | None = None) -> str:
    """Прочитать мастер-промпт; при отсутствии файла — запасной текст."""
    prompt_path = Path(path) if path else DEFAULT_PROMPT_PATH
    try:
        text = prompt_path.read_text(encoding="utf-8").strip()
    except OSError:
        text = ""
    return text or DEFAULT_SYSTEM


def load_config(
    env_path: str | os.PathLike | None = None,
    prompt_path: str | os.PathLike | None = None,
) -> Config:
    load_dotenv(Path(env_path) if env_path else DEFAULT_ENV_PATH)
    return Config(
        model=os.environ.get("LLM_MODEL"),
        base_url=os.environ.get("LLM_BASE_URL"),
        api_key=os.environ.get("LLM_API_KEY"),
        system=read_system_prompt(prompt_path),
        tts_enabled=_env_bool("TTS_ENABLED", True),
        tts_url=os.environ.get("TTS_URL", "http://127.0.0.1:3030/generate"),
        tts_voice=os.environ.get("TTS_VOICE", "jarvis"),
        tts_voice_dir=_path(os.environ.get("TTS_VOICE_DIR"), "assets/voices"),
        tts_autostart=_env_bool("TTS_AUTOSTART", True),
        tts_server_bin=_path(os.environ.get("TTS_SERVER_BIN"), "~/s2.cpp/build/s2"),
        tts_model=_path(
            os.environ.get("TTS_MODEL"),
            "~/.models/s2-pro/s2-pro-q4_k_m.gguf",
        ),
        tts_tokenizer=_path(
            os.environ.get("TTS_TOKENIZER"),
            "~/.models/s2-pro/tokenizer.json",
        ),
        tts_server_args=shlex.split(
            os.environ.get("TTS_SERVER_ARGS", "--cuda 0 -ngl -1")
        ),
        tts_reference=_path(
            os.environ.get("TTS_REFERENCE"),
            "assets/voices/jarvis_reference.mp3",
        ),
        tts_reference_text=_path(
            os.environ.get("TTS_REFERENCE_TEXT"),
            "assets/voices/jarvis_reference.txt",
        ),
    )
