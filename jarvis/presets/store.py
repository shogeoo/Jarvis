"""Файловые пресеты агентов в ``.jarvis/presets``."""

from __future__ import annotations

import json
import ctypes
import os
import re
import shutil
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path


_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
# Точка в корневом действии/handler запрещена: она означает единицу модуля.
_CAPABILITY = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


@dataclass(frozen=True, slots=True)
class AgentPreset:
    name: str
    person_prompt: str
    modules: tuple[str, ...]
    actions: tuple[str, ...]
    handlers: tuple[str, ...]
    protected: bool = False
    disabled_modules: tuple[str, ...] = ()
    disabled_actions: tuple[str, ...] = ()
    disabled_handlers: tuple[str, ...] = ()


class PresetStore:
    """Читает пресеты с диска при каждом обращении.

    Каждый пресет: person.txt плюс capabilities.json с перечнями
    modules, actions и handlers.
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        self._lock = threading.RLock()

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
        with self._lock:
            return self._load_locked(name)

    def _load_locked(self, name: str) -> AgentPreset:
        path = self.path(name)
        try:
            person_prompt = (path / "person.txt").read_text(
                encoding="utf-8"
            ).strip()
            capabilities = self._read_capabilities(path, name)
        except FileNotFoundError as exc:
            raise ValueError(f"Пресет не найден или неполон: {name}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Не удалось прочитать пресет {name}: {exc}") from exc
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
            tuple(capabilities["modules"]),
            tuple(capabilities["actions"]),
            tuple(capabilities["handlers"]),
            metadata.get("protected", False),
            tuple(self._read_disabled(path, "modules", _NAME)),
            tuple(self._read_disabled(path, "actions", _CAPABILITY)),
            tuple(self._read_disabled(path, "handlers", _CAPABILITY)),
        )

    @staticmethod
    def _read_disabled(path: Path, name: str, pattern) -> list[str]:
        unified = path / "disabled_capabilities.json"
        if unified.is_file():
            state = json.loads(unified.read_text(encoding="utf-8"))
            values = state.get(name, []) if isinstance(state, dict) else []
        else:
            legacy = path / f"disabled_{name}.json"
            values = json.loads(legacy.read_text(encoding="utf-8")) if legacy.is_file() else []
        if not isinstance(values, list) or not all(isinstance(item, str) and pattern.fullmatch(item) for item in values):
            raise ValueError(f"Некорректный список disabled {name} в preset {path.name}")
        return values

    def set_disabled(self, name: str, kind: str, capability_id: str, disabled: bool) -> None:
        self.validate_capability(kind, capability_id)
        preset = self.load(name)
        key = {"module": "modules", "action": "actions", "handler": "handlers"}[kind]
        values = set(getattr(preset, {"modules": "disabled_modules", "actions": "disabled_actions", "handlers": "disabled_handlers"}[key]))
        if disabled:
            values.add(capability_id)
        else:
            values.discard(capability_id)
        state = {
            "modules": sorted(set(preset.disabled_modules)),
            "actions": sorted(set(preset.disabled_actions)),
            "handlers": sorted(set(preset.disabled_handlers)),
        }
        state[key] = sorted(values)
        destination = self.path(name) / "disabled_capabilities.json"
        temporary = destination.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, destination)
        for legacy_key in ("modules", "actions", "handlers"):
            (destination.parent / f"disabled_{legacy_key}.json").unlink(missing_ok=True)

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
        if not person_prompt.strip():
            raise ValueError("person.txt не должен быть пустым")
        normalized = self._normalize(capabilities or {})
        with self._lock:
            if path.exists():
                raise ValueError(f"Пресет уже существует: {name}")
            self.root.mkdir(parents=True, exist_ok=True)
            staging = Path(tempfile.mkdtemp(prefix=f".{name}.", dir=self.root))
            try:
                (staging / "person.txt").write_text(
                    person_prompt.strip() + "\n", encoding="utf-8"
                )
                self._write_capabilities(staging, normalized)
                (staging / "preset.json").write_text(
                    json.dumps({"protected": False}, indent=2) + "\n",
                    encoding="utf-8",
                )
                os.rename(staging, path)
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)
                raise
        return self.load(name)

    def delete(self, name: str) -> None:
        with self._lock:
            self._delete_locked(name)

    def _delete_locked(self, name: str) -> None:
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

    def edit(self, name: str, person_prompt: str, capabilities: dict[str, list[str]]) -> AgentPreset:
        if not isinstance(person_prompt, str) or not person_prompt.strip():
            raise ValueError("person_prompt must not be empty")
        normalized = self._normalize(capabilities)
        with self._lock:
            self.load(name)
            path = self.path(name)
            staging = Path(tempfile.mkdtemp(prefix=".edit-", dir=self.root))
            replacement = staging / "replacement"
            try:
                shutil.copytree(path, replacement)
                (replacement / "person.txt").write_text(person_prompt.strip() + "\n", encoding="utf-8")
                self._write_capabilities(replacement, normalized)
                (replacement / "disabled_capabilities.json").write_text(json.dumps({key: [] for key in normalized}, indent=2) + "\n", encoding="utf-8")
                # Linux atomically exchanges the two complete directories:
                # a crash never leaves the preset absent or half-written.
                libc = ctypes.CDLL(None, use_errno=True)
                if libc.renameat2(-100, os.fsencode(path), -100, os.fsencode(replacement), 2) != 0:
                    error = ctypes.get_errno()
                    raise OSError(error, os.strerror(error))
            finally:
                shutil.rmtree(staging)
            return self.load(name)

    @staticmethod
    def _read_capabilities(path: Path, name: str) -> dict[str, list[str]]:
        try:
            values = json.loads(
                (path / "capabilities.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"Не удалось прочитать capabilities.json пресета {name}: {exc}"
            ) from exc
        if not isinstance(values, dict) or set(values) != {
            "modules",
            "actions",
            "handlers",
        }:
            raise ValueError(
                f"capabilities.json пресета {name} должен содержать ровно "
                "ключи modules, actions и handlers"
            )
        for key, pattern in (
            ("modules", _NAME),
            ("actions", _CAPABILITY),
            ("handlers", _CAPABILITY),
        ):
            items = values[key]
            if not isinstance(items, list) or not all(
                isinstance(item, str) and pattern.fullmatch(item) for item in items
            ):
                raise ValueError(
                    f"capabilities.json пресета {name}: "
                    f"{key} должен быть массивом строк"
                )
            if len(items) != len(set(items)):
                raise ValueError(f"Пресет {name} содержит повторяющиеся {key}")
        return values

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
