"""Долговременная память экземпляров агентов в ``.jarvis/memory``.

На каждый экземпляр хранится отдельный JSON-файл ``<agent_id>.json`` с
метаданными и историей сообщений. System message не сохраняется: он
пересобирается ядром при каждом запуске из актуальных модулей.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_CAPABILITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*(\.[A-Za-z0-9][A-Za-z0-9_-]*)?$")
_VERSION = 1


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


class MemoryStore:
    """Читает и атомарно пишет по одному файлу на экземпляр агента."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def path(self, agent_id: str) -> Path:
        if not valid_name(agent_id):
            raise ValueError(f"Некорректный agent_id для памяти: {agent_id!r}")
        return self.root / f"{agent_id}.json"

    def save(self, record: dict[str, Any]) -> None:
        path = self.path(record.get("agent_id"))
        payload = {"version": _VERSION, **record}
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
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

    def load(self, agent_id: str) -> dict[str, Any] | None:
        try:
            raw = json.loads(self.path(agent_id).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError, ValueError):
            return None
        return self._clean(raw)

    def load_all(self) -> list[dict[str, Any]]:
        if not self.root.exists():
            return []
        records = []
        for path in sorted(self.root.glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            record = self._clean(raw)
            if record is not None:
                records.append(record)
        return records

    def delete(self, agent_id: str) -> None:
        try:
            self.path(agent_id).unlink()
        except (FileNotFoundError, ValueError):
            return
        except OSError:
            return

    @staticmethod
    def _clean(raw: Any) -> dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None
        agent_id = raw.get("agent_id")
        if not valid_name(agent_id):
            return None
        parent_id = raw.get("parent_id")
        if parent_id is not None and not valid_name(parent_id):
            return None
        messages = raw.get("messages", [])
        if not isinstance(messages, list):
            return None
        modules = raw.get("modules", [])
        if not isinstance(modules, list) or not all(
            valid_name(item) for item in modules
        ):
            modules = []
        actions = raw.get("actions", [])
        if not isinstance(actions, list) or not all(
            valid_capability(item) for item in actions
        ):
            actions = []
        handlers = raw.get("handlers", [])
        if not isinstance(handlers, list) or not all(
            valid_capability(item) for item in handlers
        ):
            handlers = []
        name = raw.get("name")
        preset = raw.get("preset")
        return {
            "agent_id": agent_id,
            "name": name if isinstance(name, str) and name.strip() else agent_id,
            "preset": preset if valid_name(preset) else "main",
            "parent_id": parent_id,
            "modules": sorted(set(modules)),
            "actions": sorted(set(actions)),
            "handlers": sorted(set(handlers)),
            "messages": [
                message for message in messages if _valid_message(message)
            ],
        }
