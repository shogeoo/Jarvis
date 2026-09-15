"""Загрузка конфигурации Jarvis.

Параметры OpenAI-совместимого API берутся из ``.env``, мастер-промпт —
из текстового файла. Оба пути можно переопределить в CLI.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_PATH = ROOT / ".env"
DEFAULT_PROMPT_PATH = ROOT / "system_prompt.txt"
DEFAULT_SYSTEM = "Ты — Jarvis, голосовой ассистент. Отвечай кратко и по делу."


@dataclass
class Config:
    model: str | None
    base_url: str | None
    api_key: str | None
    system: str

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
        model=os.environ.get("OPENAI_MODEL"),
        base_url=os.environ.get("OPENAI_BASE_URL"),
        api_key=os.environ.get("OPENAI_API_KEY"),
        system=read_system_prompt(prompt_path),
    )
