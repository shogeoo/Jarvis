"""Долговременная память экземпляров агентов в ``.jarvis/memory``.

Раскладка::

    memory/<preset_id>/agent.json
    memory/<preset_id>/context.json
    memory/<preset_id>/<agent_id>/agent.json
    memory/<preset_id>/<agent_id>/context.json

Корневой агент preset (``main``) хранится без подпапки ``agent_id``.
``agent.json`` описывает экземпляр, ``context.json`` содержит только историю
сообщений без system message: он пересобирается при запуске.
Немодальный текст сохраняется; image/audio/video-части между перезагрузками
не переживают и вырезаются при записи.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_CAPABILITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*(\.[A-Za-z0-9][A-Za-z0-9_-]*)?$")


def valid_name(value: Any) -> bool:
    return isinstance(value, str) and bool(_NAME.fullmatch(value))


def valid_capability(value: Any) -> bool:
    return isinstance(value, str) and bool(_CAPABILITY.fullmatch(value))


def _valid_message(message: Any) -> bool:
    if not isinstance(message, dict) or message.get("role") not in {
        "user",
        "assistant",
    }:
        return False
    content = message.get("content")
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        return all(isinstance(part, dict) for part in content)
    return False


def _strip_modalities(messages: list[Any]) -> list[dict[str, Any]]:
    """Оставить в истории только текст; модальности не переживают рестарт."""

    stripped = []
    for message in messages:
        if not _valid_message(message):
            continue
        content = message["content"]
        if isinstance(content, list):
            text_parts = [
                part for part in content if part.get("type") == "text"
            ]
            if not text_parts:
                continue
            message = {**message, "content": text_parts}
        stripped.append(message)
    return stripped


def _clean_capabilities(values: Any, pattern) -> list[str]:
    if not isinstance(values, list) or not all(
        isinstance(item, str) and pattern.fullmatch(item) for item in values
    ):
        return []
    return sorted(set(values))


class MemoryStore:
    """Читает и атомарно пишет память экземпляров по пресетам."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def agent_dir(self, preset: str, agent_id: str) -> Path:
        if not valid_name(preset):
            raise ValueError(f"Некорректный preset для памяти: {preset!r}")
        if not valid_name(agent_id):
            raise ValueError(f"Некорректный agent_id для памяти: {agent_id!r}")
        if agent_id == "main":
            return self.root / preset
        return self.root / preset / agent_id

    @staticmethod
    def _write_json(path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        try:
            temporary.write_text(
                json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass

    @staticmethod
    def _read_json(path: Path) -> Any | None:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError, ValueError):
            return None

    def save(self, record: dict[str, Any]) -> None:
        agent_id = record.get("agent_id")
        preset = record.get("preset") or "main"
        directory = self.agent_dir(preset, agent_id)
        self._write_json(
            directory / "agent.json",
            {
                "agent_id": agent_id,
                "name": record.get("name"),
                "preset": preset,
                "parent_id": record.get("parent_id"),
                "modules": record.get("modules", []),
                "actions": record.get("actions", []),
                "handlers": record.get("handlers", []),
            },
        )
        messages = record.get("messages", [])
        if not isinstance(messages, list):
            messages = []
        self._write_json(
            directory / "context.json", _strip_modalities(messages)
        )

    def load(self, preset: str, agent_id: str) -> dict[str, Any] | None:
        directory = self.agent_dir(preset, agent_id)
        metadata = self._read_json(directory / "agent.json")
        messages = self._read_json(directory / "context.json")
        if not isinstance(metadata, dict) or not isinstance(messages, list):
            return None
        return self._clean(preset, agent_id, metadata, messages)

    def load_all(self) -> list[dict[str, Any]]:
        if not self.root.exists():
            return []
        records = []
        for preset_dir in sorted(self.root.iterdir()):
            if not preset_dir.is_dir() or preset_dir.name.startswith("."):
                continue
            preset = preset_dir.name
            if not valid_name(preset):
                continue
            main = self._load_agent_dir(preset_dir, preset, "main")
            if main is not None:
                records.append(main)
            for child in sorted(preset_dir.iterdir()):
                if not child.is_dir() or child.name.startswith("."):
                    continue
                if not valid_name(child.name) or child.name == "main":
                    continue
                record = self._load_agent_dir(child, preset, child.name)
                if record is not None:
                    records.append(record)
        return records

    def _load_agent_dir(
        self, directory: Path, preset: str, agent_id: str
    ) -> dict[str, Any] | None:
        metadata = self._read_json(directory / "agent.json")
        messages = self._read_json(directory / "context.json")
        if not isinstance(metadata, dict) or not isinstance(messages, list):
            return None
        return self._clean(preset, agent_id, metadata, messages)

    def delete(self, preset: str, agent_id: str) -> None:
        try:
            directory = self.agent_dir(preset, agent_id)
        except ValueError:
            return
        try:
            if agent_id == "main":
                for name in ("agent.json", "context.json"):
                    try:
                        (directory / name).unlink()
                    except FileNotFoundError:
                        pass
            else:
                shutil.rmtree(directory, ignore_errors=True)
        except OSError:
            return

    @staticmethod
    def _clean(
        preset: str, agent_id: str, metadata: Any, messages: Any
    ) -> dict[str, Any] | None:
        if not isinstance(metadata, dict) or not isinstance(messages, list):
            return None
        parent_id = metadata.get("parent_id")
        if parent_id is not None and not valid_name(parent_id):
            return None
        name = metadata.get("name")
        return {
            "agent_id": agent_id,
            "name": name if isinstance(name, str) and name.strip() else agent_id,
            "preset": preset,
            "parent_id": parent_id,
            "modules": _clean_capabilities(metadata.get("modules", []), _NAME),
            "actions": _clean_capabilities(metadata.get("actions", []), _CAPABILITY),
            "handlers": _clean_capabilities(metadata.get("handlers", []), _CAPABILITY),
            "messages": _strip_modalities(messages),
        }
