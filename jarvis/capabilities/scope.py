"""Проверка путей рабочей области `.jarvis` для управляющих действий."""

from __future__ import annotations

from pathlib import Path


def scope_path(root: Path, value: str) -> Path:
    """Проверить путь относительно корня `.jarvis` и вернуть абсолютный."""

    root = Path(root).resolve()
    candidate = (root / value).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("Путь выходит за пределы .jarvis")
    parts = candidate.relative_to(root).parts
    if ".venv" in parts or "__pycache__" in parts or ".git" in parts:
        raise ValueError("Системные каталоги управляются только ядром")
    if parts[:1] in (("memory",), ("runtime",)):
        raise ValueError("Память и runtime управляются только ядром")
    return candidate
