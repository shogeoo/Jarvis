"""Файловые пресеты агентов в ``.jarvis/presets``."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path


_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


@dataclass(frozen=True, slots=True)
class AgentPreset:
    name: str
    person_prompt: str
    modules: tuple[str, ...]
    protected: bool = False


class PresetStore:
    """Читает пресеты с диска при каждом обращении.

    Благодаря этому изменение ``modules.json`` применяется к уже работающим
    агентам на следующей безопасной границе их цикла.
    """

    def __init__(self, root: Path):
        self.root = Path(root)

    @staticmethod
    def validate_name(name: str) -> None:
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise ValueError(f"Некорректное имя пресета: {name!r}")

    def path(self, name: str) -> Path:
        self.validate_name(name)
        return self.root / name

    def load(self, name: str) -> AgentPreset:
        path = self.path(name)
        try:
            person_prompt = (path / "personprompt.txt").read_text(
                encoding="utf-8"
            ).strip()
            raw_modules = json.loads(
                (path / "modules.json").read_text(encoding="utf-8")
            )
        except FileNotFoundError as exc:
            raise ValueError(f"Пресет не найден или неполон: {name}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Не удалось прочитать пресет {name}: {exc}") from exc
        if not isinstance(raw_modules, list) or not all(
            isinstance(item, str) and _NAME.fullmatch(item) for item in raw_modules
        ):
            raise ValueError(f"modules.json пресета {name} должен быть массивом строк")
        if len(raw_modules) != len(set(raw_modules)):
            raise ValueError(f"Пресет {name} содержит повторяющиеся модули")
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
            tuple(raw_modules),
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

    def create(self, name: str, person_prompt: str, modules: list[str]) -> AgentPreset:
        self.validate_name(name)
        path = self.path(name)
        if path.exists() and self.load(name).protected:
            raise ValueError(f"Защищённый пресет {name} нельзя заменить")
        if not person_prompt.strip():
            raise ValueError("personprompt не должен быть пустым")
        if not all(
            isinstance(module_id, str) and _NAME.fullmatch(module_id)
            for module_id in modules
        ):
            raise ValueError("Список модулей содержит некорректный module_id")
        if len(modules) != len(set(modules)):
            raise ValueError("Список модулей содержит повторы")
        path.mkdir(parents=True, exist_ok=True)
        (path / "personprompt.txt").write_text(
            person_prompt.strip() + "\n", encoding="utf-8"
        )
        self._write_modules(path, modules)
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

    def add_module(self, name: str, module_id: str) -> AgentPreset:
        if not isinstance(module_id, str) or not _NAME.fullmatch(module_id):
            raise ValueError(f"Некорректный module_id: {module_id!r}")
        preset = self.load(name)
        if module_id in preset.modules:
            return preset
        self._write_modules(self.path(name), [*preset.modules, module_id])
        return self.load(name)

    @staticmethod
    def _write_modules(path: Path, modules: list[str]) -> None:
        (path / "modules.json").write_text(
            json.dumps(modules, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
