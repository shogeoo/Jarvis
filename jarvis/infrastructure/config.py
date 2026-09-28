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
    subscription_enabled: bool = False
    subscription_model: str | None = None

    @property
    def llm_enabled(self) -> bool:
        return bool(self.model or self.subscription_enabled)


def _enabled(value: str | None) -> bool:
    if value is None:
        return False
    normalized = value.strip().lower()
    if normalized not in {"true", "false"}:
        raise ValueError("CHATGPT_SUBSCRIPTION_ENABLED must be true or false")
    return normalized == "true"


def load_config(env_path: str | os.PathLike | None = None) -> Config:
    environment = (
        Path(env_path).expanduser().resolve() if env_path else DEFAULT_ENV_PATH
    )
    project_root = ROOT
    load_dotenv(environment)
    jarvis_dir = DEFAULT_JARVIS_DIR
    return Config(
        model=os.environ.get("LLM_API_MODEL") or os.environ.get("LLM_MODEL"),
        base_url=os.environ.get("LLM_BASE_URL"),
        api_key=os.environ.get("LLM_API_KEY"),
        project_root=project_root,
        jarvis_dir=jarvis_dir,
        reasoning_effort=os.getenv("LLM_REASONING_EFFORT", "").strip() or None,
        subscription_enabled=_enabled(os.environ.get("CHATGPT_SUBSCRIPTION_ENABLED")),
        subscription_model=os.environ.get("LLM_SUBSCRIPTION_MODEL", "").strip() or None,
    )
