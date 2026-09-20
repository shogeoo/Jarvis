"""Загрузка, диспетчеризация и полная выгрузка capabilities."""

from __future__ import annotations

import importlib.util
import json
import os
import queue
import re
import select
import subprocess
import sys
import threading
import uuid
import venv
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import ModuleType
from typing import Any

from ..core.lifecycle import terminate_process
from ..core.protocol import (
    ActionRequest,
    Event,
    InputPart,
    validate_json,
    validate_strict_schema,
)
from ..core.registry import ActionRegistry, EventRegistry
from ..infrastructure.config import DEFAULT_JARVIS_DIR
from ..infrastructure.debug import Debugger
from .api import (
    PENDING,
    ActionContext,
    ActionDefinition,
    EventDefinition,
    HandlerContext,
    HandlerDefinition,
    ModuleDefinition,
)


DEFAULT_ROOT = DEFAULT_JARVIS_DIR
_SIMPLE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
_UNIT = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*(\.[A-Za-z][A-Za-z0-9_-]*)*$")


@dataclass(slots=True)
class _ActionJob:
    data: dict[str, Any]
    context: ActionContext


@dataclass(slots=True)
class LoadedAction:
    id: str
    definition: ActionDefinition
    path: Path
    python_import: str | None
    module_id: str | None
    execution: str = "in_process"
    jobs: queue.Queue[_ActionJob | None] = field(default_factory=queue.Queue)
    thread: threading.Thread | None = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    started: bool = False


@dataclass(slots=True)
class LoadedHandler:
    id: str
    definition: HandlerDefinition
    path: Path
    python_import: str | None
    module_id: str | None
    execution: str = "in_process"
    contexts: list[HandlerContext] = field(default_factory=list)
    threads: list[threading.Thread] = field(default_factory=list)
    stop_event: threading.Event = field(default_factory=threading.Event)
    started: bool = False


@dataclass(slots=True)
class LoadedModule:
    module_id: str
    definition: ModuleDefinition
    path: Path
    package_import: str | None = None
    execution: str = "in_process"
    action_ids: list[str] = field(default_factory=list)
    handler_ids: list[str] = field(default_factory=list)
    stop_event: threading.Event = field(default_factory=threading.Event)
    started: bool = False
    process: subprocess.Popen | None = None
    reader_thread: threading.Thread | None = None
    writer_lock: threading.Lock = field(default_factory=threading.Lock)
    stderr_stream: Any = None


class CapabilityManager:
    """Код на диске существует независимо от runtime в оперативной памяти."""

    def __init__(
        self,
        event_bus: Any,
        actions: ActionRegistry,
        events: EventRegistry,
        *,
        root: Path = DEFAULT_ROOT,
        config: Any = None,
        debug: Debugger | None = None,
        services: dict[str, Any] | None = None,
    ):
        self.event_bus = event_bus
        self.actions = actions
        self.events = events
        self.root = Path(root)
        self.actions_dir = self.root / "actions"
        self.handlers_dir = self.root / "handlers"
        self.modules_dir = self.root / "modules"
        self.config = config
        self.debug = debug or Debugger(enabled=False)
        self.services = services if services is not None else {}
        self._lock = threading.RLock()
        self._actions: dict[str, LoadedAction] = {}
        self._handlers: dict[str, LoadedHandler] = {}
        self._modules: dict[str, LoadedModule] = {}
        self._disabled_targets: dict[tuple[str, str], list[str]] = {}
        self._closing = threading.Event()

    @staticmethod
    def validate_module_id(module_id: str) -> None:
        if not isinstance(module_id, str) or not _SIMPLE.fullmatch(module_id):
            raise ValueError(f"Некорректный module_id: {module_id!r}")

    @staticmethod
    def validate_unit_id(unit_id: str, *, kind: str) -> None:
        if not isinstance(unit_id, str) or not _UNIT.fullmatch(unit_id):
            raise ValueError(f"Некорректный {kind}: {unit_id!r}")

    def action_path(self, action_id: str) -> Path:
        self.validate_unit_id(action_id, kind="action")
        return self.actions_dir / f"{action_id}.py"

    def handler_path(self, handler_id: str) -> Path:
        self.validate_unit_id(handler_id, kind="handler")
        return self.handlers_dir / f"{handler_id}.py"

    def module_path(self, module_id: str) -> Path:
        self.validate_module_id(module_id)
        return self.modules_dir / module_id

    def discover_modules(self) -> list[Path]:
        if not self.modules_dir.exists():
            return []
        return sorted(
            path
            for path in self.modules_dir.iterdir()
            if path.is_dir()
            and not path.name.startswith(".")
            and (path / "module.py").is_file()
        )

    def discover_actions(self) -> list[Path]:
        return self._discover_units(self.actions_dir)

    def discover_handlers(self) -> list[Path]:
        return self._discover_units(self.handlers_dir)

    @staticmethod
    def _discover_units(directory: Path) -> list[Path]:
        if not directory.exists():
            return []
        return sorted(
            path
            for path in directory.iterdir()
            if path.is_file()
            and path.suffix == ".py"
            and not path.name.startswith(".")
            and path.name != "__init__.py"
            and _UNIT.fullmatch(path.stem)
        )

    def existing_modules(self) -> set[str]:
        return {path.name for path in self.discover_modules()}

    def existing_actions(self) -> set[str]:
        return {path.stem for path in self.discover_actions()}

    def existing_handlers(self) -> set[str]:
        return {path.stem for path in self.discover_handlers()}

    def loaded_modules(self) -> set[str]:
        with self._lock:
            return set(self._modules)

    def loaded_actions(self) -> set[str]:
        with self._lock:
            return set(self._actions)

    def loaded_handlers(self) -> set[str]:
        with self._lock:
            return set(self._handlers)

    def loaded_snapshot(self) -> dict[str, set[str]]:
        return {
            "modules": self.loaded_modules(),
            "actions": self.loaded_actions(),
            "handlers": self.loaded_handlers(),
        }

    def missing(self, snapshot: dict[str, set[str]]) -> set[str]:
        loaded = self.loaded_snapshot()
        missing: set[str] = set()
        for module_id in snapshot.get("modules", ()):
            if module_id not in loaded["modules"]:
                missing.add(f"module:{module_id}")
        for action_id in snapshot.get("actions", ()):
            if action_id not in loaded["actions"]:
                missing.add(f"action:{action_id}")
        for handler_id in snapshot.get("handlers", ()):
            if handler_id not in loaded["handlers"]:
                missing.add(f"handler:{handler_id}")
        return missing

    def list_existing(self) -> dict[str, Any]:
        loaded = self.loaded_snapshot()
        modules = []
        for path in self.discover_modules():
            package = None
            try:
                module_id, definition, package = self._inspect_module(path)
                modules.append(
                    {
                        "module_id": module_id,
                        "description": definition.description,
                        "loaded": path.name in loaded["modules"],
                    }
                )
            except Exception as exc:  # noqa: BLE001
                modules.append(
                    {
                        "module_id": path.name,
                        "description": "",
                        "loaded": False,
                        "error": str(exc),
                    }
                )
            finally:
                if package is not None:
                    self._forget_import(package)
        actions = []
        for path in self.discover_actions():
            with self._lock:
                loaded_definition = (
                    self._actions[path.stem].definition
                    if path.stem in self._actions
                    else None
                )
            actions.append(
                {
                    "action_id": path.stem,
                    "description": (
                        loaded_definition.description
                        if loaded_definition is not None
                        else ""
                    ),
                    "loaded": path.stem in loaded["actions"],
                }
            )
        handlers = []
        for path in self.discover_handlers():
            with self._lock:
                loaded_definition = (
                    self._handlers[path.stem].definition
                    if path.stem in self._handlers
                    else None
                )
            handlers.append(
                {
                    "handler_id": path.stem,
                    "description": (
                        loaded_definition.description
                        if loaded_definition is not None
                        else ""
                    ),
                    "loaded": path.stem in loaded["handlers"],
                }
            )
        return {"modules": modules, "actions": actions, "handlers": handlers}

    @staticmethod
    def _forget_import(name: str) -> None:
        for key in tuple(sys.modules):
            if key == name or key.startswith(name + "."):
                sys.modules.pop(key, None)

    @classmethod
    def _import_file(
        cls, path: Path, package: str | None
    ) -> tuple[ModuleType, str]:
        if not path.is_file():
            raise ValueError(f"Нет файла capability: {path}")
        safe = re.sub(r"[^A-Za-z0-9_]", "_", path.stem)
        if package is not None:
            import_name = f"{package}.{safe}_{uuid.uuid4().hex}"
        else:
            import_name = f"jarvis_unit_{safe}_{uuid.uuid4().hex}"
        # Файлы capability не являются пакетами: относительные импорты
        # разрешаются по dotted-имени.
        spec = importlib.util.spec_from_file_location(import_name, path)
        if spec is None or spec.loader is None:
            raise ValueError(f"Не удалось импортировать файл: {path}")
        python_module = importlib.util.module_from_spec(spec)
        sys.modules[import_name] = python_module
        try:
            spec.loader.exec_module(python_module)
        except Exception:
            cls._forget_import(import_name)
            raise
        return python_module, import_name

    @classmethod
    def _ensure_package(cls, module_id: str, path: Path) -> str:
        package = f"jarvis_container_{module_id}_{uuid.uuid4().hex}"
        package_module = ModuleType(package)
        package_module.__path__ = [str(path.resolve())]
        sys.modules[package] = package_module
        return package

    @staticmethod
    def _check_action(definition: ActionDefinition) -> None:
        if not callable(definition.run):
            raise ValueError("Некорректное действие: run должен быть функцией")
        for name, schema in (
            ("аргументов", definition.args_schema),
            ("результата", definition.result_schema),
        ):
            if not isinstance(schema, dict) or schema.get("type") != "object":
                raise ValueError(f"Схема {name} действия должна быть объектом")
            validate_strict_schema(schema, where=f"схема {name} действия")

    @staticmethod
    def _check_handler(definition: HandlerDefinition) -> None:
        if not callable(definition.start):
            raise ValueError("Некорректный handler: start должен быть функцией")
        if definition.stop is not None and not callable(definition.stop):
            raise ValueError("Некорректный stop handler")
        event = definition.event
        if not isinstance(event, EventDefinition):
            raise ValueError("Handler обязан объявлять ровно одно событие")
        if not isinstance(event.data_schema, dict):
            raise ValueError(f"Схема события {event.type} должна быть объектом")
        validate_strict_schema(event.data_schema, where=f"схема события {event.type}")

    def _load_standalone_action(
        self, path: Path
    ) -> tuple[ActionDefinition, str]:
        python_module, import_name = self._import_file(path, None)
        try:
            factory = getattr(python_module, "create_action", None)
            if not callable(factory):
                raise ValueError(f"В файле {path} нет create_action()")
            definition = factory()
        except Exception:
            self._forget_import(import_name)
            raise
        if not isinstance(definition, ActionDefinition):
            self._forget_import(import_name)
            raise ValueError(f"create_action в {path} должен вернуть ActionDefinition")
        if definition.id:
            self._forget_import(import_name)
            raise ValueError(
                f"ID действия захардкожен в {path}: назначается из имени файла"
            )
        self._check_action(definition)
        return definition, import_name

    def _load_standalone_handler(
        self, path: Path
    ) -> tuple[HandlerDefinition, str]:
        python_module, import_name = self._import_file(path, None)
        try:
            factory = getattr(python_module, "create_handler", None)
            if not callable(factory):
                raise ValueError(f"В файле {path} нет create_handler()")
            definition = factory()
        except Exception:
            self._forget_import(import_name)
            raise
        if not isinstance(definition, HandlerDefinition):
            self._forget_import(import_name)
            raise ValueError(
                f"create_handler в {path} должен вернуть HandlerDefinition"
            )
        if definition.id:
            self._forget_import(import_name)
            raise ValueError(
                f"ID handler захардкожен в {path}: назначается из имени файла"
            )
        self._check_handler(definition)
        return definition, import_name

    def _inspect_module(
        self, path: Path
    ) -> tuple[str, ModuleDefinition, str]:
        """Импортировать module.py и вернуть метаданные. Пакет не удаляется."""

        module_id = path.name
        self.validate_module_id(module_id)
        source = path / "module.py"
        if not source.is_file():
            raise ValueError(f"В модуле {path} нет обязательного module.py")
        package = self._ensure_package(module_id, path)
        try:
            python_module, _ = self._import_file(source, package)
            factory = getattr(python_module, "create_module", None)
            if not callable(factory):
                raise ValueError(f"В файле {source} нет create_module()")
            definition = factory()
        except Exception:
            self._forget_import(package)
            raise
        if not isinstance(definition, ModuleDefinition):
            self._forget_import(package)
            raise ValueError(f"create_module в {source} должен вернуть ModuleDefinition")
        if definition.execution not in {"in_process", "isolated"}:
            self._forget_import(package)
            raise ValueError(f"Неизвестный execution модуля {module_id}")
        return module_id, definition, package

    def _derive_module_units(
        self, path: Path, module_id: str, definition: ModuleDefinition
    ) -> tuple[list[tuple[str, ActionDefinition]], list[tuple[str, HandlerDefinition]]]:
        """Сопоставить определения с файлами и назначить ID. Без захардкоженных ID."""

        by_source: dict[str, tuple[str, Any]] = {}
        for item in definition.actions:
            if not isinstance(item, ActionDefinition):
                raise ValueError(f"Модуль {module_id} вернул не действие")
            if item.id:
                raise ValueError("ID действия захардкожен: назначается из имени файла")
            if not item.source:
                raise ValueError("Действие без файла-источника")
            key = str(Path(item.source).resolve())
            if key in by_source:
                raise ValueError(
                    f"Один файл — одно действие: {item.source}"
                )
            by_source[key] = ("action", item)
        for item in definition.handlers:
            if not isinstance(item, HandlerDefinition):
                raise ValueError(f"Модуль {module_id} вернул не handler")
            if item.id:
                raise ValueError("ID handler захардкожен: назначается из имени файла")
            if not item.source:
                raise ValueError("Handler без файла-источника")
            key = str(Path(item.source).resolve())
            if key in by_source:
                raise ValueError(f"Один файл — одна единица: {item.source}")
            by_source[key] = ("handler", item)
        actions_dir = path / "actions"
        handlers_dir = path / "handlers"
        if not actions_dir.is_dir() or not handlers_dir.is_dir():
            raise ValueError(
                f"Модуль {module_id} обязан содержать actions/ и handlers/"
            )
        actions: list[tuple[str, ActionDefinition]] = []
        for unit in self._discover_units(actions_dir):
            key = str(unit.resolve())
            entry = by_source.get(key)
            if entry is None or entry[0] != "action":
                raise ValueError(
                    f"Файл {unit.name} не подключён в module.py модуля {module_id}"
                )
            if not _SIMPLE.fullmatch(unit.stem):
                raise ValueError(
                    f"Некорректное имя действия модуля {module_id}: {unit.name!r}"
                )
            definition_item = entry[1]
            self._check_action(definition_item)
            actions.append((f"{module_id}.{unit.stem}", definition_item))
        handlers: list[tuple[str, HandlerDefinition]] = []
        for unit in self._discover_units(handlers_dir):
            key = str(unit.resolve())
            entry = by_source.get(key)
            if entry is None or entry[0] != "handler":
                raise ValueError(
                    f"Файл {unit.name} не подключён в module.py модуля {module_id}"
                )
            if not _SIMPLE.fullmatch(unit.stem):
                raise ValueError(
                    f"Некорректное имя handler модуля {module_id}: {unit.name!r}"
                )
            definition_item = entry[1]
            self._check_handler(definition_item)
            handlers.append((f"{module_id}.{unit.stem}", definition_item))
        if not actions and not handlers:
            raise ValueError(f"Модуль {module_id} не содержит действий или handlers")
        return actions, handlers

    def validate_action(self, action_id: str) -> dict[str, Any]:
        path = self.action_path(action_id)
        definition, import_name = self._load_standalone_action(path)
        try:
            assigned = replace(definition, id=action_id)
            return self._action_summary(assigned, path)
        finally:
            self._forget_import(import_name)

    def validate_handler(self, handler_id: str) -> dict[str, Any]:
        path = self.handler_path(handler_id)
        definition, import_name = self._load_standalone_handler(path)
        try:
            assigned = replace(definition, id=handler_id)
            return self._handler_summary(assigned, path)
        finally:
            self._forget_import(import_name)

    def validate_module(self, module_id: str) -> dict[str, Any]:
        path = self.module_path(module_id)
        module_id, definition, package = self._inspect_module(path)
        try:
            actions, handlers = self._derive_module_units(path, module_id, definition)
            return {
                "module_id": module_id,
                "description": definition.description,
                "actions": [
                    self._action_summary(
                        replace(item, id=action_id),
                        path / "actions" / f"{action_id.split('.', 1)[1]}.py",
                    )
                    for action_id, item in actions
                ],
                "handlers": [
                    self._handler_summary(
                        replace(item, id=handler_id),
                        path / "handlers" / f"{handler_id.split('.', 1)[1]}.py",
                    )
                    for handler_id, item in handlers
                ],
            }
        finally:
            self._forget_import(package)

    def validate(self, *, kind: str, capability_id: str) -> dict[str, Any]:
        if kind == "module":
            return self.validate_module(capability_id)
        if kind == "action":
            path = self.actions_dir / f"{capability_id}.py"
            if path.is_file():
                return self.validate_action(capability_id)
            if "." in capability_id:
                module_id, _ = capability_id.split(".", 1)
                if module_id in self.existing_modules():
                    raise ValueError(
                        f"Часть модуля нельзя проверить отдельно: {capability_id!r}"
                    )
            raise ValueError(f"Действие не найдено: {capability_id}")
        if kind == "handler":
            path = self.handlers_dir / f"{capability_id}.py"
            if path.is_file():
                return self.validate_handler(capability_id)
            if "." in capability_id:
                module_id, _ = capability_id.split(".", 1)
                if module_id in self.existing_modules():
                    raise ValueError(
                        f"Часть модуля нельзя проверить отдельно: {capability_id!r}"
                    )
            raise ValueError(f"Handler не найден: {capability_id}")
        raise ValueError(f"Неизвестный вид capability: {kind!r}")

    def load_snapshot(self, snapshot: dict[str, set[str]], *, start_handlers: bool) -> None:
        loaded: list[tuple[str, str]] = []
        try:
            for module_id in sorted(snapshot.get("modules", ())):
                if module_id not in self.loaded_modules():
                    self.load_module(module_id, start_handlers=False)
                    loaded.append(("module", module_id))
            for action_id in sorted(snapshot.get("actions", ())):
                if action_id not in self.loaded_actions():
                    self.load_action(action_id, start_handlers=False)
                    loaded.append(("action", action_id))
            for handler_id in sorted(snapshot.get("handlers", ())):
                if handler_id not in self.loaded_handlers():
                    self.load_handler(handler_id, start_handlers=False)
                    loaded.append(("handler", handler_id))
            if start_handlers:
                self.start_snapshot(snapshot)
        except Exception:
            for kind, capability_id in reversed(loaded):
                if kind == "module":
                    self.unload_module(capability_id)
                elif kind == "action":
                    self.unload_action(capability_id)
                else:
                    self.unload_handler(capability_id)
            raise

    def start_snapshot(self, snapshot: dict[str, set[str]]) -> None:
        for module_id in sorted(snapshot.get("modules", ())):
            with self._lock:
                runtime = self._modules.get(module_id)
            if runtime is not None:
                self.start_module(module_id)
        for action_id in sorted(snapshot.get("actions", ())):
            with self._lock:
                runtime = self._actions.get(action_id)
            if runtime is not None and runtime.module_id is None:
                self.start_action(action_id)
        for handler_id in sorted(snapshot.get("handlers", ())):
            with self._lock:
                runtime = self._handlers.get(handler_id)
            if runtime is not None and runtime.module_id is None:
                self.start_handler(handler_id)

    def release_snapshot(
        self, snapshot: dict[str, set[str]], *, agents: list[Any]
    ) -> None:
        for module_id in sorted(snapshot.get("modules", ())):
            if not any(module_id in agent.modules() for agent in agents):
                self.unload_module(module_id)
        for action_id in sorted(snapshot.get("actions", ())):
            if not any(action_id in agent.standalone_actions() for agent in agents):
                self.unload_action(action_id)
        for handler_id in sorted(snapshot.get("handlers", ())):
            if not any(handler_id in agent.standalone_handlers() for agent in agents):
                self.unload_handler(handler_id)

    def load_module(self, module_id: str, *, start_handlers: bool = True) -> dict[str, Any]:
        if self._closing.is_set():
            raise RuntimeError("runtime_stopping")
        with self._lock:
            existing = self._modules.get(module_id)
        if existing is not None:
            if start_handlers:
                self.start_module(module_id)
            return self._module_summary(existing)
        path = self.module_path(module_id)
        module_id, definition, package = self._inspect_module(path)
        try:
            actions, handlers = self._derive_module_units(path, module_id, definition)
            if definition.execution == "isolated":
                catalog = self._describe_isolated(module_id)
                runtime = self._register_isolated_module(
                    path, module_id, definition, catalog
                )
            else:
                if self.config is not None and definition.prepare is not None:
                    definition.prepare(self.config)
                runtime = self._register_in_process_module(
                    path, module_id, definition, package, actions, handlers
                )
        except Exception:
            self.actions.unregister_owner(f"module:{module_id}")
            self.events.unregister_owner(f"module:{module_id}")
            self._forget_import(package)
            raise
        with self._lock:
            self._modules[module_id] = runtime
        if start_handlers:
            try:
                self._start_module(runtime)
            except Exception:
                self.unload_module(module_id)
                raise
        return self._module_summary(runtime)

    def load_action(self, action_id: str, *, start_handlers: bool = True) -> dict[str, Any]:
        if self._closing.is_set():
            raise RuntimeError("runtime_stopping")
        with self._lock:
            existing = self._actions.get(action_id)
        if existing is not None:
            if start_handlers:
                self.start_action(action_id)
            return self._action_summary(existing.definition, existing.path)
        path = self.actions_dir / f"{action_id}.py"
        if not path.is_file():
            if "." in action_id:
                module_id, _ = action_id.split(".", 1)
                if module_id in self.existing_modules():
                    raise ValueError(
                        f"Часть модуля нельзя загрузить отдельно: {action_id!r}"
                    )
            raise ValueError(f"Действие не найдено: {action_id}")
        definition, import_name = self._load_standalone_action(path)
        owner = f"action:{action_id}"
        try:
            self.actions.replace_owner(
                [replace(definition, id=action_id, owner=owner)], owner=owner
            )
        except Exception:
            self._forget_import(import_name)
            raise
        runtime = LoadedAction(
            id=action_id,
            definition=replace(definition, id=action_id, owner=owner),
            path=path,
            python_import=import_name,
            module_id=None,
            execution="in_process",
        )
        with self._lock:
            self._actions[action_id] = runtime
        if start_handlers:
            try:
                self._start_action(runtime)
            except Exception:
                self.unload_action(action_id)
                raise
        return self._action_summary(runtime.definition, path)

    def load_handler(self, handler_id: str, *, start_handlers: bool = True) -> dict[str, Any]:
        if self._closing.is_set():
            raise RuntimeError("runtime_stopping")
        with self._lock:
            existing = self._handlers.get(handler_id)
        if existing is not None:
            if start_handlers:
                self.start_handler(handler_id)
            return self._handler_summary(existing.definition, existing.path)
        path = self.handlers_dir / f"{handler_id}.py"
        if not path.is_file():
            if "." in handler_id:
                module_id, _ = handler_id.split(".", 1)
                if module_id in self.existing_modules():
                    raise ValueError(
                        f"Часть модуля нельзя загрузить отдельно: {handler_id!r}"
                    )
            raise ValueError(f"Handler не найден: {handler_id}")
        definition, import_name = self._load_standalone_handler(path)
        owner = f"handler:{handler_id}"
        try:
            self.events.replace_owner([definition.event], owner=owner)
        except Exception:
            self._forget_import(import_name)
            raise
        runtime = LoadedHandler(
            id=handler_id,
            definition=replace(definition, id=handler_id, owner=owner),
            path=path,
            python_import=import_name,
            module_id=None,
            execution="in_process",
        )
        with self._lock:
            self._handlers[handler_id] = runtime
        if start_handlers:
            try:
                self._start_handler(runtime)
            except Exception:
                self.unload_handler(handler_id)
                raise
        return self._handler_summary(runtime.definition, path)

    def start_module(self, module_id: str) -> None:
        with self._lock:
            runtime = self._modules.get(module_id)
        if runtime is None:
            raise RuntimeError(f"Модуль {module_id} не загружен")
        self._start_module(runtime)

    def start_action(self, action_id: str) -> None:
        with self._lock:
            runtime = self._actions.get(action_id)
        if runtime is None:
            raise RuntimeError(f"Действие {action_id} не загружено")
        self._start_action(runtime)

    def start_handler(self, handler_id: str) -> None:
        with self._lock:
            runtime = self._handlers.get(handler_id)
        if runtime is None:
            raise RuntimeError(f"Handler {handler_id} не загружен")
        self._start_handler(runtime)

    def _register_in_process_module(
        self,
        path: Path,
        module_id: str,
        definition: ModuleDefinition,
        package: str,
        actions: list[tuple[str, ActionDefinition]],
        handlers: list[tuple[str, HandlerDefinition]],
    ) -> LoadedModule:
        owner = f"module:{module_id}"
        assigned_actions = [
            replace(item, id=action_id, owner=owner)
            for action_id, item in actions
        ]
        assigned_handlers = [
            replace(item, id=handler_id, owner=owner)
            for handler_id, item in handlers
        ]
        self.actions.replace_owner(assigned_actions, owner=owner)
        try:
            self.events.replace_owner(
                [handler.event for handler in assigned_handlers],
                owner=owner,
            )
        except Exception:
            self.actions.unregister_owner(owner)
            raise
        runtime = LoadedModule(
            module_id=module_id,
            definition=definition,
            path=path,
            package_import=package,
            execution="in_process",
            action_ids=[item.id for item in assigned_actions],
            handler_ids=[item.id for item in assigned_handlers],
        )
        with self._lock:
            for item in assigned_actions:
                stem = item.id.split(".", 1)[1]
                self._actions[item.id] = LoadedAction(
                    id=item.id,
                    definition=item,
                    path=path / "actions" / f"{stem}.py",
                    python_import=None,
                    module_id=module_id,
                    execution="in_process",
                    stop_event=runtime.stop_event,
                )
            for item in assigned_handlers:
                stem = item.id.split(".", 1)[1]
                self._handlers[item.id] = LoadedHandler(
                    id=item.id,
                    definition=item,
                    path=path / "handlers" / f"{stem}.py",
                    python_import=None,
                    module_id=module_id,
                    execution="in_process",
                    stop_event=runtime.stop_event,
                )
        return runtime

    def _register_isolated_module(
        self,
        path: Path,
        module_id: str,
        definition: ModuleDefinition,
        catalog: dict[str, Any],
    ) -> LoadedModule:
        def proxy(action_id: str):
            def run(data, context):
                self._send_isolated(
                    module_id,
                    {
                        "kind": "action",
                        "agent_id": context.agent_id,
                        "action_id": context.action_id,
                        "type": action_id,
                        "data": data,
                    },
                )
                return PENDING

            return run

        actions = tuple(
            ActionDefinition(
                id=item["id"],
                description=item["description"],
                args_schema=item["args_schema"],
                result_schema=item["result_schema"],
                run=proxy(item["id"]),
                source="",
                owner=f"module:{module_id}",
            )
            for item in catalog["actions"]
        )
        handlers = tuple(
            HandlerDefinition(
                id=item["id"],
                description=item["description"],
                event=EventDefinition(
                    item["event"]["type"],
                    item["event"]["description"],
                    item["event"]["data_schema"],
                ),
                start=lambda context: None,
                source="",
                owner=f"module:{module_id}",
            )
            for item in catalog["handlers"]
        )
        owner = f"module:{module_id}"
        self.actions.replace_owner(list(actions), owner=owner)
        try:
            self.events.replace_owner(
                [handler.event for handler in handlers],
                owner=owner,
            )
        except Exception:
            self.actions.unregister_owner(owner)
            raise
        runtime = LoadedModule(
            module_id=module_id,
            definition=definition,
            path=path,
            package_import=None,
            execution="isolated",
            action_ids=[item.id for item in actions],
            handler_ids=[item.id for item in handlers],
        )
        with self._lock:
            for item in actions:
                self._actions[item.id] = LoadedAction(
                    id=item.id,
                    definition=item,
                    path=path,
                    python_import=None,
                    module_id=module_id,
                    execution="isolated",
                    stop_event=runtime.stop_event,
                )
            for item in handlers:
                self._handlers[item.id] = LoadedHandler(
                    id=item.id,
                    definition=item,
                    path=path,
                    python_import=None,
                    module_id=module_id,
                    execution="isolated",
                    stop_event=runtime.stop_event,
                )
        return runtime

    def _start_module(self, runtime: LoadedModule) -> None:
        with self._lock:
            if runtime.started:
                return
            runtime.started = True
        if runtime.execution == "isolated":
            self._start_isolated(runtime)
        for action_id in runtime.action_ids:
            with self._lock:
                loaded = self._actions.get(action_id)
            if loaded is not None and loaded.module_id == runtime.module_id:
                self._start_action(loaded)
        for handler_id in runtime.handler_ids:
            with self._lock:
                loaded = self._handlers.get(handler_id)
            if loaded is not None and loaded.module_id == runtime.module_id:
                self._start_handler(loaded)

    def _start_action(self, runtime: LoadedAction) -> None:
        with self._lock:
            if runtime.started:
                return
            runtime.started = True
            if runtime.execution == "isolated":
                return
        runtime.thread = threading.Thread(
            target=self._run_action,
            args=(runtime,),
            name=f"jarvis-action-{runtime.id}",
            daemon=True,
        )
        runtime.thread.start()

    def _start_handler(self, runtime: LoadedHandler) -> None:
        with self._lock:
            if runtime.started:
                return
            runtime.started = True
            if runtime.execution == "isolated":
                return
        context = HandlerContext(
            handler_id=runtime.id,
            unit_path=runtime.path,
            emit_event=self.event_bus.publish,
            module_id=runtime.module_id,
            agent_manager=getattr(self.event_bus, "manager", None),
            capabilities=self,
            config=self.config,
            services=self.services,
            stop_event=runtime.stop_event,
        )
        with self._lock:
            runtime.contexts.append(context)
        thread = threading.Thread(
            target=self._run_handler,
            args=(runtime, runtime.definition, context),
            name=f"jarvis-handler-{runtime.id}",
            daemon=True,
        )
        with self._lock:
            runtime.threads.append(thread)
        thread.start()

    def _worker_command(self, module_id: str) -> list[str]:
        path = self.module_path(module_id)
        python = path / ".venv" / "bin" / "python"
        if not python.is_file():
            raise ValueError(
                f"У изолированного модуля {path.name} нет .venv; "
                "сначала подготовь окружение модуля"
            )
        return [
            str(python),
            "-m",
            "jarvis.capabilities.worker",
            "--jarvis-dir",
            str(self.root.resolve()),
            "--module-id",
            module_id,
        ]

    def _worker_env(self) -> dict[str, str]:
        environment = dict(os.environ)
        project_root = str(self.config.project_root if self.config else Path.cwd())
        previous = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = (
            project_root if not previous else project_root + os.pathsep + previous
        )
        return environment

    def _describe_isolated(self, module_id: str) -> dict[str, Any]:
        command = [*self._worker_command(module_id), "--describe"]
        try:
            completed = subprocess.run(
                command,
                cwd=self.config.project_root if self.config else Path.cwd(),
                env=self._worker_env(),
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                f"Worker {module_id} не прошёл импорт: {exc.stderr.strip()}"
            ) from exc
        lines = completed.stdout.strip().splitlines()
        if not lines:
            raise RuntimeError(f"Worker {module_id} не вернул описание")
        message = json.loads(lines[-1])
        if message.get("kind") != "description":
            raise RuntimeError(f"Worker вернул неожиданный ответ: {message}")
        catalog = message["catalog"]
        if catalog.get("module_id") != module_id:
            raise ValueError("module_id worker не совпадает с каталогом модуля")
        return catalog

    def _start_isolated(self, runtime: LoadedModule) -> None:
        module_id = runtime.module_id
        log_dir = (self.config.jarvis_dir if self.config else DEFAULT_ROOT) / "runtime"
        log_dir.mkdir(parents=True, exist_ok=True)
        runtime.stderr_stream = open(log_dir / f"{module_id}.log", "ab")
        runtime.process = subprocess.Popen(
            self._worker_command(module_id),
            cwd=self.config.project_root if self.config else Path.cwd(),
            env=self._worker_env(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=runtime.stderr_stream,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        ready, _, _ = select.select([runtime.process.stdout], [], [], 30)
        if not ready:
            raise TimeoutError(
                f"Worker {module_id} не запустился за 30 секунд"
            )
        first = runtime.process.stdout.readline()
        if not first:
            raise RuntimeError(f"Worker {module_id} не запустился")
        message = json.loads(first)
        if message.get("kind") != "ready":
            raise RuntimeError(f"Worker вернул неожиданный ответ: {message}")
        runtime.reader_thread = threading.Thread(
            target=self._read_isolated,
            args=(runtime,),
            name=f"jarvis-ipc-{module_id}",
            daemon=True,
        )
        runtime.reader_thread.start()

    def _send_isolated(self, module_id: str, message: dict[str, Any]) -> None:
        with self._lock:
            runtime = self._modules.get(module_id)
        if runtime is None or runtime.process is None or runtime.process.stdin is None:
            raise RuntimeError(f"Worker модуля {module_id} не запущен")
        with runtime.writer_lock:
            runtime.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            runtime.process.stdin.flush()

    def _read_isolated(self, runtime: LoadedModule) -> None:
        process = runtime.process
        if process is None or process.stdout is None:
            return
        for line in process.stdout:
            try:
                message = json.loads(line)
                if message.get("kind") == "module_error":
                    self._report_error(
                        f"module:{runtime.module_id}", message["error"]
                    )
                    if message.get("action_id"):
                        self._discard_pending(
                            message.get("agent_id"), message.get("action_id")
                        )
                elif message.get("kind") == "action_result":
                    self._complete_action(
                        message.get("agent_id"),
                        message.get("action_id"),
                        message.get("data"),
                        tuple(
                            InputPart(item["type"], item["mime_type"], item["data"])
                            for item in message.get("parts", [])
                        ),
                    )
                elif message.get("kind") == "event":
                    raw = message["event"]
                    self.event_bus.publish(
                        Event(
                            type=raw["type"],
                            data=raw["data"],
                            source=f"module:{runtime.module_id}",
                            target=raw.get("target"),
                            reply_to=raw.get("reply_to"),
                            parts=tuple(
                                InputPart(item["type"], item["mime_type"], item["data"])
                                for item in raw.get("parts", [])
                            ),
                            module_id=runtime.module_id,
                            handler_id=raw.get("handler_id"),
                        )
                    )
            except Exception as exc:  # noqa: BLE001
                self._report_error(f"module:{runtime.module_id}", exc)
        if not runtime.stop_event.is_set():
            self._report_error(
                f"module:{runtime.module_id}",
                "Worker неожиданно завершился",
            )

    def _manager(self) -> Any:
        return getattr(self.event_bus, "manager", None)

    def _complete_action(
        self,
        agent_id: str | None,
        action_id: str | None,
        data: Any,
        parts: Any = (),
    ) -> bool:
        manager = self._manager()
        if manager is None or agent_id is None or action_id is None:
            self.debug.log(
                "action_result_dropped",
                agent_id=agent_id,
                action_id=action_id,
                reason="no_manager",
            )
            return False
        if not isinstance(data, dict):
            self.debug.log(
                "action_result_rejected",
                agent_id=agent_id,
                action_id=action_id,
                reason="invalid_data",
            )
            manager.results.discard(agent_id, action_id)
            return False
        return manager.results.complete(
            manager,
            agent_id=agent_id,
            action_id=action_id,
            data=data,
            parts=tuple(parts),
        )

    def _discard_pending(self, agent_id: str | None, action_id: str | None) -> None:
        manager = self._manager()
        if manager is None or agent_id is None or action_id is None:
            return
        manager.results.discard(agent_id, action_id)

    def dispatch(self, *, action: ActionRequest, spec: ActionDefinition, agent: Any) -> None:
        validate_json(action.data, spec.args_schema, where=f"аргументы {action.type}")
        with self._lock:
            runtime = self._actions.get(action.type)
            if runtime is None or not runtime.started:
                raise RuntimeError(f"Действие {action.type} выключено")
            manager = self._manager()
            if manager is None:
                raise RuntimeError("Менеджер агентов не запущен")
            manager.results.begin(
                agent_id=agent.agent_id,
                action_id=action.action_id,
                action_type=action.type,
                result_schema=spec.result_schema,
            )
            context = ActionContext(
                agent_id=agent.agent_id,
                action_id=action.action_id,
                action_type=action.type,
                capability_id=action.type,
                module_id=runtime.module_id,
                complete=lambda data, parts=(): self._complete_action(
                    agent.agent_id, action.action_id, data, parts
                ),
                config=self.config,
                agent_manager=manager,
                capabilities=self,
                services=self.services,
                metadata={"preset": agent.preset, "agent_name": agent.name},
                stop_event=runtime.stop_event,
            )
            if runtime.execution == "isolated":
                message = {
                    "kind": "action",
                    "agent_id": context.agent_id,
                    "action_id": context.action_id,
                    "type": context.action_type,
                    "data": dict(action.data),
                }
            else:
                runtime.jobs.put(_ActionJob(dict(action.data), context))
        if runtime.execution == "isolated":
            self._send_isolated(runtime.module_id, message)

    def _run_action(self, runtime: LoadedAction) -> None:
        while not runtime.stop_event.is_set():
            job = runtime.jobs.get()
            if job is None:
                return
            try:
                result = runtime.definition.run(job.data, job.context)
            except Exception as exc:  # noqa: BLE001
                owner = (
                    f"module:{runtime.module_id}"
                    if runtime.module_id is not None
                    else f"action:{runtime.id}"
                )
                self._report_error(owner, exc)
                self._discard_pending(job.context.agent_id, job.context.action_id)
                continue
            if result is PENDING:
                continue
            self._complete_action(
                job.context.agent_id, job.context.action_id, result
            )

    def _run_handler(
        self,
        runtime: LoadedHandler,
        definition: HandlerDefinition,
        context: HandlerContext,
    ) -> None:
        try:
            definition.start(context)
        except Exception as exc:  # noqa: BLE001
            if not runtime.stop_event.is_set():
                owner = definition.owner or (
                    f"module:{runtime.module_id}"
                    if runtime.module_id is not None
                    else f"handler:{runtime.id}"
                )
                self._report_error(owner, exc)

    def _report_error(self, capability: str, exc: Exception | str) -> None:
        manager = self._manager()
        if manager is not None:
            manager.report_capability_error(capability, exc)
        else:
            self.debug.log("capability_error", capability=capability, error=str(exc))

    def unload_action(self, action_id: str) -> None:
        with self._lock:
            runtime = self._actions.pop(action_id, None)
        if runtime is None or runtime.module_id is not None:
            return
        runtime.stop_event.set()
        while True:
            try:
                runtime.jobs.get_nowait()
            except queue.Empty:
                break
        runtime.jobs.put(None)
        current = threading.current_thread()
        if runtime.thread is not None and runtime.thread is not current:
            runtime.thread.join(timeout=5)
        self.actions.unregister_owner(f"action:{action_id}")
        if runtime.python_import is not None:
            self._forget_import(runtime.python_import)

    def unload_handler(self, handler_id: str) -> None:
        with self._lock:
            runtime = self._handlers.pop(handler_id, None)
        if runtime is None or runtime.module_id is not None:
            return
        runtime.stop_event.set()
        if runtime.definition.stop is not None:
            for context in runtime.contexts:
                try:
                    runtime.definition.stop(context)
                except Exception as exc:  # noqa: BLE001
                    self._report_error(f"handler:{handler_id}", exc)
        current = threading.current_thread()
        for thread in runtime.threads:
            if thread is not current:
                thread.join(timeout=5)
        self.events.unregister_owner(f"handler:{handler_id}")
        if runtime.python_import is not None:
            self._forget_import(runtime.python_import)

    def unload_module(self, module_id: str) -> None:
        with self._lock:
            runtime = self._modules.pop(module_id, None)
            actions = [
                self._actions.pop(action_id, None) for action_id in (
                    runtime.action_ids if runtime is not None else []
                )
            ]
            handlers = [
                self._handlers.pop(handler_id, None) for handler_id in (
                    runtime.handler_ids if runtime is not None else []
                )
            ]
        if runtime is None:
            return
        runtime.stop_event.set()
        for action in actions:
            if action is not None and action.execution == "in_process":
                while True:
                    try:
                        action.jobs.get_nowait()
                    except queue.Empty:
                        break
                action.jobs.put(None)
        if runtime.process is not None:
            try:
                if runtime.process.stdin is not None:
                    with runtime.writer_lock:
                        runtime.process.stdin.write('{"kind":"shutdown"}\n')
                        runtime.process.stdin.flush()
            except Exception:
                pass
            try:
                runtime.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                terminate_process(runtime.process, group=True)
        for handler in handlers:
            if handler is not None and handler.definition.stop is not None:
                for context in handler.contexts:
                    try:
                        handler.definition.stop(context)
                    except Exception as exc:  # noqa: BLE001
                        self._report_error(f"module:{module_id}", exc)
        current = threading.current_thread()
        for action in actions:
            if (
                action is not None
                and action.thread is not None
                and action.thread is not current
            ):
                action.thread.join(timeout=5)
        for handler in handlers:
            if handler is not None:
                for thread in handler.threads:
                    if thread is not current:
                        thread.join(timeout=5)
        if runtime.reader_thread is not None and runtime.reader_thread is not current:
            runtime.reader_thread.join(timeout=2)
        if runtime.process is not None:
            for stream in (runtime.process.stdin, runtime.process.stdout):
                if stream is not None:
                    stream.close()
        owner = f"module:{module_id}"
        self.actions.unregister_owner(owner)
        self.events.unregister_owner(owner)
        if runtime.definition.teardown is not None:
            try:
                runtime.definition.teardown(self.config)
            except Exception as exc:  # noqa: BLE001
                self._report_error(f"module:{module_id}", exc)
        if runtime.package_import is not None:
            self._forget_import(runtime.package_import)
        if runtime.stderr_stream is not None:
            runtime.stderr_stream.close()

    def disable_for_edit(self, *, kind: str, capability_id: str) -> dict[str, Any]:
        key = (kind, capability_id)
        if key in self._disabled_targets:
            return {
                "kind": kind,
                "capability_id": capability_id,
                "disabled_for": list(self._disabled_targets[key]),
            }
        manager = self._manager()
        if manager is None:
            raise RuntimeError("Менеджер агентов не запущен")
        targets = manager.disable_capability_everywhere(
            kind=kind, capability_id=capability_id
        )
        self._disabled_targets[key] = targets
        return {
            "kind": kind,
            "capability_id": capability_id,
            "disabled_for": targets,
        }

    def enable_after_edit(self, *, kind: str, capability_id: str) -> dict[str, Any]:
        self.validate(kind=kind, capability_id=capability_id)
        targets = self._disabled_targets.get((kind, capability_id), [])
        restored = []
        if targets:
            manager = self._manager()
            restored = manager.restore_capability(
                kind=kind, capability_id=capability_id, agent_ids=targets
            )
        self._disabled_targets.pop((kind, capability_id), None)
        return {
            "kind": kind,
            "capability_id": capability_id,
            "restored_for": restored,
        }

    def create_environment(self, module_id: str) -> dict[str, Any]:
        path = self.module_path(module_id)
        requirements = path / "requirements.txt"
        requirements_name = "requirements.txt" if requirements.is_file() else None
        if requirements_name:
            unpinned = [
                line.strip()
                for line in requirements.read_text(encoding="utf-8").splitlines()
                if line.strip()
                and not line.lstrip().startswith(("#", "-"))
                and "==" not in line
            ]
            if unpinned:
                raise ValueError(
                    "Зависимости должны быть закреплены через ==: "
                    + ", ".join(unpinned)
                )
        environment = path / ".venv"
        if not environment.exists():
            venv.EnvBuilder(with_pip=True).create(environment)
        if requirements_name is not None:
            python = environment / "bin" / "python"
            subprocess.run(
                [str(python), "-m", "pip", "install", "-r", str(requirements)],
                cwd=path,
                check=True,
            )
        return {
            "module_id": module_id,
            "environment": str(environment),
            "requirements": requirements_name,
        }

    def catalog(self, snapshot: dict[str, set[str]]) -> dict[str, Any]:
        with self._lock:
            actions = []
            for action_id in sorted(snapshot.get("actions", ())):
                runtime = self._actions.get(action_id)
                if runtime is None or runtime.module_id is not None:
                    continue
                actions.append(self._action_summary(runtime.definition, runtime.path))
            handlers = []
            events = []
            for handler_id in sorted(snapshot.get("handlers", ())):
                runtime = self._handlers.get(handler_id)
                if runtime is None or runtime.module_id is not None:
                    continue
                handlers.append(self._handler_summary(runtime.definition, runtime.path))
                event = runtime.definition.event
                events.append(
                    {
                        "type": event.type,
                        "description": event.description,
                        "data_schema": event.data_schema,
                    }
                )
            modules = []
            for module_id in sorted(snapshot.get("modules", ())):
                runtime = self._modules.get(module_id)
                if runtime is None:
                    continue
                modules.append(self._module_summary(runtime))
            return {"actions": actions, "handlers": handlers, "events": events, "modules": modules}

    def shutdown(self) -> None:
        self._closing.set()
        for action_id in list(self.loaded_actions()):
            if self._actions.get(action_id) is not None and (
                self._actions[action_id].module_id is None
            ):
                self.unload_action(action_id)
        for handler_id in list(self.loaded_handlers()):
            if self._handlers.get(handler_id) is not None and (
                self._handlers[handler_id].module_id is None
            ):
                self.unload_handler(handler_id)
        for module_id in list(self.loaded_modules()):
            self.unload_module(module_id)

    @staticmethod
    def _action_summary(definition: ActionDefinition, path: Path) -> dict[str, Any]:
        return {
            "id": definition.id,
            "type": definition.id,
            "description": definition.description,
            "args_schema": definition.args_schema,
            "result_schema": definition.result_schema,
            "path": str(path),
        }

    @staticmethod
    def _handler_summary(definition: HandlerDefinition, path: Path) -> dict[str, Any]:
        return {
            "id": definition.id,
            "description": definition.description,
            "path": str(path),
            "events": [
                {
                    "type": definition.event.type,
                    "description": definition.event.description,
                    "data_schema": definition.event.data_schema,
                }
            ],
        }

    def _module_summary(self, runtime: LoadedModule) -> dict[str, Any]:
        with self._lock:
            actions = [
                self._action_summary(
                    self._actions[action_id].definition,
                    self._actions[action_id].path,
                )
                for action_id in runtime.action_ids
                if action_id in self._actions
            ]
            events = []
            handlers = []
            for handler_id in runtime.handler_ids:
                loaded = self._handlers.get(handler_id)
                if loaded is None:
                    continue
                handlers.append(
                    {
                        "id": handler_id,
                        "description": loaded.definition.description,
                    }
                )
                event = loaded.definition.event
                events.append(
                    {
                        "type": event.type,
                        "description": event.description,
                        "data_schema": event.data_schema,
                    }
                )
            return {
                "module_id": runtime.module_id,
                "description": runtime.definition.description,
                "path": str(runtime.path),
                "actions": actions,
                "handlers": handlers,
                "events": events,
            }
