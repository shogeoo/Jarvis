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
    reasoning_effort: str | None = None

    @property
    def llm_enabled(self) -> bool:
        return bool(self.model)


def load_config(env_path: str | os.PathLike | None = None) -> Config:
    environment = (
        Path(env_path).expanduser().resolve() if env_path else DEFAULT_ENV_PATH
    )
    project_root = ROOT
    load_dotenv(environment)
    jarvis_dir = DEFAULT_JARVIS_DIR
    return Config(
        model=os.environ.get("LLM_MODEL"),
        base_url=os.environ.get("LLM_BASE_URL"),
        api_key=os.environ.get("LLM_API_KEY"),
        project_root=project_root,
        jarvis_dir=jarvis_dir,
        reasoning_effort=os.getenv("LLM_REASONING_EFFORT", "").strip() or None,
    )
