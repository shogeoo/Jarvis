"""Файловые пресеты агентов в ``.jarvis/presets``."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path


_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
_CAPABILITY = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*(\.[A-Za-z][A-Za-z0-9_-]*)?$")


@dataclass(frozen=True, slots=True)
class AgentPreset:
    name: str
    person_prompt: str
    modules: tuple[str, ...]
    actions: tuple[str, ...]
    handlers: tuple[str, ...]
    protected: bool = False


class PresetStore:
    """Читает пресеты с диска при каждом обращении.

    Благодаря этому изменение ``capabilities.json`` применяется к уже работающим
    агентам на следующей безопасной границе их цикла.
    """

    def __init__(self, root: Path):
        self.root = Path(root)

    @staticmethod
    def validate_name(name: str) -> None:
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise ValueError(f"Некорректное имя пресета: {name!r}")

    @staticmethod
    def validate_capability(kind: str, capability_id: str) -> None:
        if kind == "module":
            pattern = _NAME
        elif kind in {"action", "handler"}:
            pattern = _CAPABILITY
        else:
            raise ValueError(f"Неизвестный вид capability: {kind!r}")
        if not isinstance(capability_id, str) or not pattern.fullmatch(capability_id):
            raise ValueError(f"Некорректный {kind}: {capability_id!r}")

    def path(self, name: str) -> Path:
        self.validate_name(name)
        return self.root / name

    def load(self, name: str) -> AgentPreset:
        path = self.path(name)
        try:
            person_prompt = (path / "personprompt.txt").read_text(
                encoding="utf-8"
            ).strip()
            raw = json.loads(
                (path / "capabilities.json").read_text(encoding="utf-8")
            )
        except FileNotFoundError as exc:
            raise ValueError(f"Пресет не найден или неполон: {name}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Не удалось прочитать пресет {name}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ValueError(
                f"capabilities.json пресета {name} должен быть объектом"
            )
        capabilities = {}
        for kind in ("modules", "actions", "handlers"):
            values = raw.get(kind, [])
            singular = kind.rstrip("s") if kind != "modules" else "module"
            if not isinstance(values, list) or not all(
                isinstance(item, str) for item in values
            ):
                raise ValueError(
                    f"capabilities.json пресета {name}: {kind} должен быть массивом строк"
                )
            for item in values:
                self.validate_capability(singular, item)
            if len(values) != len(set(values)):
                raise ValueError(f"Пресet {name} содержит повторяющиеся {kind}")
            capabilities[kind] = tuple(values)
        metadata_path = path / "preset.json"
        try:
            metadata = (
                json.loads(metadata_path.read_text(encoding="utf-8"))
                if metadata_path.exists()
                else {}
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Не удалось прочитать metadata пресета {name}: {exc}") from exc
        if not isinstance(metadata, dict) or not isinstance(
            metadata.get("protected", False), bool
        ):
            raise ValueError(f"preset.json пресета {name} имеет неверный формат")
        return AgentPreset(
            name,
            person_prompt,
            capabilities["modules"],
            capabilities["actions"],
            capabilities["handlers"],
            metadata.get("protected", False),
        )

    def list(self) -> list[AgentPreset]:
        if not self.root.exists():
            return []
        presets = []
        for path in sorted(self.root.iterdir()):
            if path.is_dir() and not path.name.startswith("."):
                presets.append(self.load(path.name))
        return presets

    def create(
        self,
        name: str,
        person_prompt: str,
        capabilities: dict[str, list[str]] | None = None,
    ) -> AgentPreset:
        self.validate_name(name)
        path = self.path(name)
        if path.exists():
            try:
                existing = self.load(name)
            except ValueError:
                existing = None
            if existing is not None and existing.protected:
                raise ValueError(f"Защищённый пресет {name} нельзя заменить")
        if not person_prompt.strip():
            raise ValueError("personprompt не должен быть пустым")
        normalized = self._normalize(capabilities or {})
        path.mkdir(parents=True, exist_ok=True)
        (path / "personprompt.txt").write_text(
            person_prompt.strip() + "\n", encoding="utf-8"
        )
        self._write_capabilities(path, normalized)
        return self.load(name)

    def delete(self, name: str) -> None:
        self.validate_name(name)
        path = self.path(name)
        if not path.is_dir():
            raise ValueError(f"Пресет не найден: {name}")
        if self.load(name).protected:
            raise ValueError(f"Защищённый пресет {name} нельзя удалить")
        for child in path.iterdir():
            if child.is_file():
                child.unlink()
            else:
                raise ValueError(f"В пресете есть неизвестный каталог: {child}")
        path.rmdir()

    def add_capability(
        self, name: str, kind: str, capability_id: str
    ) -> AgentPreset:
        self.validate_capability(kind, capability_id)
        preset = self.load(name)
        key = {"module": "modules", "action": "actions", "handler": "handlers"}[kind]
        current = getattr(preset, key)
        if capability_id in current:
            return preset
        normalized = {
            "modules": list(preset.modules),
            "actions": list(preset.actions),
            "handlers": list(preset.handlers),
        }
        normalized[key].append(capability_id)
        self._write_capabilities(self.path(name), self._normalize(normalized))
        return self.load(name)

    @classmethod
    def _normalize(
        cls, capabilities: dict[str, list[str]]
    ) -> dict[str, list[str]]:
        normalized = {
            "modules": list(capabilities.get("modules", [])),
            "actions": list(capabilities.get("actions", [])),
            "handlers": list(capabilities.get("handlers", [])),
        }
        for kind, values in (
            ("module", normalized["modules"]),
            ("action", normalized["actions"]),
            ("handler", normalized["handlers"]),
        ):
            for item in values:
                cls.validate_capability(kind, item)
            if len(values) != len(set(values)):
                raise ValueError("Список capabilities содержит повторы")
        return normalized

    @staticmethod
    def _write_capabilities(
        path: Path, capabilities: dict[str, list[str]]
    ) -> None:
        (path / "capabilities.json").write_text(
            json.dumps(capabilities, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
