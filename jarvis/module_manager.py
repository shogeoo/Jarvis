"""Загрузка и жизненный цикл пользовательских модулей."""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import sys
import threading
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

from .config import ROOT
from .debug import Debugger
from .module_api import HandlerSpec, Module, ModuleContext
from .protocol import Event, validate_strict_schema
from .registry import ActionRegistry, EventRegistry


DEFAULT_MODULES_DIR = ROOT / ".jarvis" / "modules"
_MODULE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


@dataclass(slots=True)
class LoadedModule:
    definition: Module
    path: Path
    python_module: ModuleType
    contexts: list[ModuleContext] = field(default_factory=list)
    threads: list[threading.Thread] = field(default_factory=list)


class ModuleManager:
    """Сканирует ``.jarvis/modules`` и подключает пакеты расширений."""

    def __init__(
        self,
        event_bus: Any,
        actions: ActionRegistry,
        events: EventRegistry,
        *,
        modules_dir: Path = DEFAULT_MODULES_DIR,
        config: Any = None,
        debug: Debugger | None = None,
    ):
        self.event_bus = event_bus
        self.actions = actions
        self.events = events
        self.modules_dir = Path(modules_dir)
        self.config = config
        self.debug = debug or Debugger(enabled=False)
        self._lock = threading.RLock()
        self._loaded: dict[str, LoadedModule] = {}

    def discover(self) -> list[Path]:
        if not self.modules_dir.exists():
            return []
        return sorted(
            path
            for path in self.modules_dir.iterdir()
            if path.is_dir() and not path.name.startswith(".")
        )

    @staticmethod
    def validate_name(module_name: str) -> None:
        if not isinstance(module_name, str) or not _MODULE_NAME.fullmatch(module_name):
            raise ValueError(f"Некорректное имя модуля: {module_name!r}")

    def load_all(self) -> list[dict[str, Any]]:
        loaded = []
        for path in self.discover():
            try:
                loaded.append(self.load(path.name, start_handlers=False))
            except Exception as exc:  # noqa: BLE001
                self.debug.log(
                    "module_error",
                    module=path.name,
                    operation="load",
                    error=str(exc),
                    traceback=traceback.format_exc(),
                )
        return loaded

    def _manifest(self, path: Path) -> dict[str, Any]:
        manifest_path = path / "module.json"
        if not manifest_path.is_file():
            raise ValueError(f"В модуле {path} нет module.json")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Некорректный module.json: {manifest_path}: {exc}") from exc
        if not isinstance(manifest, dict):
            raise ValueError(f"module.json должен быть JSON-объектом: {manifest_path}")
        name = manifest.get("name")
        version = manifest.get("version")
        if not isinstance(name, str) or not _MODULE_NAME.fullmatch(name):
            raise ValueError(f"Некорректное имя модуля: {name!r}")
        if name != path.name:
            raise ValueError(f"Имя модуля {name!r} не совпадает с каталогом {path.name!r}")
        if not isinstance(version, str) or not version:
            raise ValueError(f"У модуля {name} должна быть версия")
        entrypoint = manifest.get("entrypoint", "module.py")
        factory = manifest.get("factory", "create_module")
        if not isinstance(entrypoint, str) or Path(entrypoint).is_absolute():
            raise ValueError(f"Некорректный entrypoint модуля {name}")
        if not isinstance(factory, str) or not factory.isidentifier():
            raise ValueError(f"Некорректная factory-функция модуля {name}")
        return {
            "name": name,
            "version": version,
            "description": str(manifest.get("description", "")),
            "entrypoint": entrypoint,
            "factory": factory,
        }

    @staticmethod
    def _import_module(path: Path, manifest: dict[str, Any]) -> tuple[Module, ModuleType]:
        source = path / manifest["entrypoint"]
        if not source.is_file():
            raise ValueError(f"В модуле {manifest['name']} нет файла {source.name}")
        safe_name = re.sub(r"[^A-Za-z0-9_]", "_", manifest["name"])
        import_name = f"jarvis_user_module_{safe_name}_{id(source)}"
        spec = importlib.util.spec_from_file_location(import_name, source)
        if spec is None or spec.loader is None:
            raise ValueError(f"Не удалось импортировать модуль: {source}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[import_name] = module
        try:
            sys.path.insert(0, str(path))
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(import_name, None)
            raise
        finally:
            if sys.path and sys.path[0] == str(path):
                sys.path.pop(0)
        factory = getattr(module, manifest["factory"], None)
        if not callable(factory):
            raise ValueError(
                f"Функция {manifest['factory']} не найдена в {source}"
            )
        try:
            definition = factory()
        except Exception:
            sys.modules.pop(module.__name__, None)
            raise
        if not isinstance(definition, Module):
            sys.modules.pop(module.__name__, None)
            raise ValueError("factory модуля должна вернуть jarvis.module_api.Module")
        if definition.name != manifest["name"]:
            sys.modules.pop(module.__name__, None)
            raise ValueError(
                f"Имя Module ({definition.name!r}) не совпадает с module.json"
            )
        if definition.version != manifest["version"]:
            sys.modules.pop(module.__name__, None)
            raise ValueError(
                f"Версия Module ({definition.version!r}) не совпадает с module.json"
            )
        return definition, module

    @staticmethod
    def _check_definition(definition: Module) -> None:
        action_names = [spec.type for spec in definition.actions]
        if len(action_names) != len(set(action_names)):
            raise ValueError(f"Модуль {definition.name} объявляет повторные действия")
        event_names = [event.type for handler in definition.handlers for event in handler.events]
        if len(event_names) != len(set(event_names)):
            raise ValueError(f"Модуль {definition.name} объявляет повторные события")
        handler_names = [handler.name for handler in definition.handlers]
        if len(handler_names) != len(set(handler_names)):
            raise ValueError(f"Модуль {definition.name} объявляет повторные обработчики")
        for spec in definition.actions:
            if not spec.type or not callable(spec.handler):
                raise ValueError(f"Некорректное действие {spec.type!r}")
            if spec.data_schema.get("type") != "object":
                raise ValueError(f"Схема действия {spec.type} должна быть объектом")
            validate_strict_schema(spec.data_schema, where=f"схема действия {spec.type}")
        for handler in definition.handlers:
            if not handler.name or not callable(handler.start):
                raise ValueError(f"Некорректный обработчик {handler.name!r}")
            for event in handler.events:
                if not event.type:
                    raise ValueError(f"Некорректное событие в обработчике {handler.name}")
                if event.data_schema.get("type") != "object":
                    raise ValueError(f"Схема события {event.type} должна быть объектом")
                validate_strict_schema(
                    event.data_schema,
                    where=f"схема события {event.type}",
                )

    def validate(self, module_name: str, *, path: Path | None = None) -> dict[str, Any]:
        if path is None:
            self.validate_name(module_name)
        module_path = Path(path) if path is not None else self.modules_dir / module_name
        manifest = self._manifest(module_path)
        definition, python_module = self._import_module(module_path, manifest)
        self._check_definition(definition)
        sys.modules.pop(python_module.__name__, None)
        return self._summary(definition, module_path)

    def load(self, module_name: str, *, start_handlers: bool = True) -> dict[str, Any]:
        self.validate_name(module_name)
        module_path = self.modules_dir / module_name
        manifest = self._manifest(module_path)
        definition, python_module = self._import_module(module_path, manifest)
        self._check_definition(definition)
        with self._lock:
            if module_name in self._loaded:
                self._unload_locked(module_name)
            owner = f"module:{module_name}"
            self.actions.replace_owner(definition.actions, owner=owner)
            try:
                self.events.replace_owner(
                    [event for handler in definition.handlers for event in handler.events],
                    owner=owner,
                )
            except Exception:
                self.actions.unregister_owner(owner)
                sys.modules.pop(python_module.__name__, None)
                raise
            runtime = LoadedModule(definition, module_path, python_module)
            self._loaded[module_name] = runtime
            if start_handlers:
                self._start_handlers_locked(runtime)
        summary = self._summary(definition, module_path)
        self.debug.log("module_loaded", **summary)
        return summary

    def start_all(self) -> None:
        """Запустить обработчики уже загруженных модулей."""

        with self._lock:
            for runtime in self._loaded.values():
                if not runtime.threads:
                    self._start_handlers_locked(runtime)

    def _start_handlers_locked(self, runtime: LoadedModule) -> None:
        for handler in runtime.definition.handlers:
            context = ModuleContext(
                module_name=runtime.definition.name,
                module_path=runtime.path,
                emit_event=self.event_bus.publish,
                config=self.config,
            )
            runtime.contexts.append(context)
            thread = threading.Thread(
                target=self._run_handler,
                args=(runtime, handler, context),
                name=f"jarvis-module-{runtime.definition.name}-{handler.name}",
                daemon=True,
            )
            runtime.threads.append(thread)
            thread.start()

    def _run_handler(
        self, runtime: LoadedModule, handler: HandlerSpec, context: ModuleContext
    ) -> None:
        self.debug.log(
            "handler_started",
            module=runtime.definition.name,
            handler=handler.name,
            events=[event.type for event in handler.events],
        )
        try:
            handler.start(context)
        except Exception as exc:  # noqa: BLE001
            if not context.stop_event.is_set():
                self.debug.log(
                    "handler_error",
                    module=runtime.definition.name,
                    handler=handler.name,
                    error=str(exc),
                    traceback=traceback.format_exc(),
                )
                self.event_bus.publish(
                    Event(
                        type="handler_error",
                        data={
                            "module": runtime.definition.name,
                            "handler": handler.name,
                            "error": str(exc),
                        },
                        source=f"module:{runtime.definition.name}",
                        target="main",
                    )
                )
        finally:
            self.debug.log(
                "handler_stopped",
                module=runtime.definition.name,
                handler=handler.name,
            )

    def unload(self, module_name: str) -> None:
        with self._lock:
            self._unload_locked(module_name)

    def _unload_locked(self, module_name: str) -> None:
        runtime = self._loaded.pop(module_name, None)
        if runtime is None:
            return
        for context, handler in zip(runtime.contexts, runtime.definition.handlers):
            context.stop_event.set()
            if handler.stop is not None:
                try:
                    handler.stop(context)
                except Exception as exc:  # noqa: BLE001
                    self.debug.log(
                        "handler_stop_error",
                        module=module_name,
                        handler=handler.name,
                        error=str(exc),
                    )
        for thread in runtime.threads:
            thread.join(timeout=5)
        owner = f"module:{module_name}"
        self.actions.unregister_owner(owner)
        self.events.unregister_owner(owner)
        sys.modules.pop(runtime.python_module.__name__, None)
        self.debug.log("module_unloaded", module=module_name)

    def apply(self, operation: str, module_name: str) -> dict[str, Any]:
        """Применить результат работы разработчика модулей."""

        self.validate_name(module_name)
        if operation == "delete":
            self.unload(module_name)
            path = self.modules_dir / module_name
            if path.exists():
                shutil.rmtree(path)
            return {
                "module": module_name,
                "operation": operation,
                "actions": [],
                "events": [],
                "handlers": [],
            }
        if operation not in {"create", "update"}:
            raise ValueError(f"Неизвестная операция с модулем: {operation}")
        summary = self.load(module_name)
        summary["operation"] = operation
        return summary

    def list_modules(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                self._summary(runtime.definition, runtime.path)
                for runtime in sorted(self._loaded.values(), key=lambda item: item.definition.name)
            ]

    def describe(self, module_name: str) -> dict[str, Any]:
        with self._lock:
            runtime = self._loaded.get(module_name)
            if runtime is None:
                raise KeyError(f"Модуль не подключён: {module_name}")
            return self._summary(runtime.definition, runtime.path)

    @staticmethod
    def _summary(definition: Module, path: Path) -> dict[str, Any]:
        return {
            "module": definition.name,
            "version": definition.version,
            "description": definition.description,
            "path": str(path),
            "actions": [
                {
                    "type": spec.type,
                    "description": spec.description,
                    "data_schema": spec.data_schema,
                }
                for spec in definition.actions
            ],
            "events": [
                {
                    "type": event.type,
                    "description": event.description,
                    "data_schema": event.data_schema,
                }
                for handler in definition.handlers
                for event in handler.events
            ],
            "handlers": [
                {"name": handler.name, "description": handler.description}
                for handler in definition.handlers
            ],
        }
