"""Конфигурация неизменяемого ядра Jarvis."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_PATH = ROOT / ".env"
DEFAULT_JARVIS_DIR = ROOT / ".jarvis"


@dataclass(frozen=True, slots=True)
class Config:
    model: str | None
    base_url: str | None
    api_key: str | None
    project_root: Path
    jarvis_dir: Path

    @property
    def llm_enabled(self) -> bool:
        return bool(self.model)


def load_config(env_path: str | os.PathLike | None = None) -> Config:
    load_dotenv(Path(env_path) if env_path else DEFAULT_ENV_PATH)
    jarvis_value = os.environ.get("JARVIS_DIR")
    jarvis_dir = Path(jarvis_value).expanduser() if jarvis_value else DEFAULT_JARVIS_DIR
    if not jarvis_dir.is_absolute():
        jarvis_dir = ROOT / jarvis_dir
    return Config(
        model=os.environ.get("LLM_MODEL"),
        base_url=os.environ.get("LLM_BASE_URL"),
        api_key=os.environ.get("LLM_API_KEY"),
        project_root=ROOT,
        jarvis_dir=jarvis_dir,
    )
