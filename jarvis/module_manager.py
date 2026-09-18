"""Загрузка и жизненный цикл пользовательских модулей."""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import ModuleType
from typing import Any

from .config import ROOT
from .debug import Debugger
from .module_api import HandlerSpec, Module, ModuleContext
from .protocol import Event, validate_strict_schema
from .registry import ActionRegistry, EventRegistry


DEFAULT_MODULES_DIR = ROOT / ".assistant" / "modules"
_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


@dataclass(slots=True)
class LoadedModule:
    definition: Module
    path: Path
    python_module: ModuleType
    contexts: list[ModuleContext] = field(default_factory=list)
    threads: list[threading.Thread] = field(default_factory=list)


class ModuleManager:
    """Сканирует .assistant/modules и регистрирует модули глобально."""

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
        self._closing = threading.Event()

    @staticmethod
    def validate_name(name: str) -> None:
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise ValueError(f"Некорректное имя: {name!r}")

    def module_path(self, module_name: str) -> Path:
        self.validate_name(module_name)
        return self.modules_dir / module_name

    def discover(self) -> list[Path]:
        if not self.modules_dir.exists():
            return []
        return sorted(
            path
            for path in self.modules_dir.iterdir()
            if path.is_dir()
            and not path.name.startswith(".")
            and (path / "module.json").is_file()
        )

    def load_all(self, *, start_handlers: bool = False) -> list[dict[str, Any]]:
        loaded = []
        for path in self.discover():
            if path.name in self._loaded:
                continue
            try:
                loaded.append(self.load(path.name, start_handlers=start_handlers))
            except Exception as exc:  # noqa: BLE001
                self.debug.log(
                    "module_error",
                    module=path.name,
                    operation="load",
                    error=str(exc),
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
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise ValueError(f"Некорректное имя модуля: {name!r}")
        if name != path.name:
            raise ValueError(f"Имя модуля {name!r} не совпадает с каталогом {path.name!r}")
        description = manifest.get("description", "")
        entrypoint = manifest.get("entrypoint")
        factory = manifest.get("factory")
        if not isinstance(description, str):
            raise ValueError(f"Описание модуля {name} должно быть строкой")
        if not isinstance(entrypoint, str) or not entrypoint or Path(entrypoint).is_absolute():
            raise ValueError(f"Некорректный entrypoint модуля {name}")
        if not isinstance(factory, str) or not factory.isidentifier():
            raise ValueError(f"Некорректная factory-функция модуля {name}")
        if not (path / "actions").is_dir() or not (path / "handlers").is_dir():
            raise ValueError(f"Модуль {name} обязан содержать actions/ и handlers/")
        return {
            "name": name,
            "description": description,
            "entrypoint": entrypoint,
            "factory": factory,
        }

    @staticmethod
    def _forget_import(name: str) -> None:
        for key in tuple(sys.modules):
            if key == name or key.startswith(name + "."):
                sys.modules.pop(key, None)

    @classmethod
    def _import_module(cls, path: Path, manifest: dict[str, Any]) -> tuple[Module, ModuleType]:
        source = path / manifest["entrypoint"]
        if not source.is_file():
            raise ValueError(f"В модуле {manifest['name']} нет файла {source.name}")
        safe_name = re.sub(r"[^A-Za-z0-9_]", "_", manifest["name"])
        import_name = f"jarvis_module_{safe_name}_{uuid.uuid4().hex}"
        spec = importlib.util.spec_from_file_location(
            import_name,
            source,
            submodule_search_locations=[str(path.resolve())],
        )
        if spec is None or spec.loader is None:
            raise ValueError(f"Не удалось импортировать модуль: {source}")
        python_module = importlib.util.module_from_spec(spec)
        sys.modules[import_name] = python_module
        try:
            spec.loader.exec_module(python_module)
            factory = getattr(python_module, manifest["factory"], None)
            if not callable(factory):
                raise ValueError(f"Функция {manifest['factory']} не найдена в {source}")
            definition = factory()
        except Exception:
            cls._forget_import(import_name)
            raise
        if not isinstance(definition, Module):
            cls._forget_import(import_name)
            raise ValueError("factory модуля должна вернуть Module")
        if definition.name != manifest["name"]:
            cls._forget_import(import_name)
            raise ValueError("Имя Module не совпадает с module.json")
        if definition.description != manifest["description"]:
            cls._forget_import(import_name)
            raise ValueError("Описание Module не совпадает с module.json")
        return definition, python_module

    @staticmethod
    def _check_definition(definition: Module) -> None:
        prefix = f"{definition.name}."
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
            if not spec.type.startswith(prefix):
                raise ValueError(
                    f"Действие {spec.type} должно начинаться с {prefix}"
                )
            validate_strict_schema(spec.data_schema, where=f"схема действия {spec.type}")
        for handler in definition.handlers:
            if not handler.name or not callable(handler.start):
                raise ValueError(f"Некорректный обработчик {handler.name!r}")
            for event in handler.events:
                if not event.type:
                    raise ValueError(f"Некорректное событие в обработчике {handler.name}")
                if not event.type.startswith(prefix):
                    raise ValueError(
                        f"Событие {event.type} должно начинаться с {prefix}"
                    )
                validate_strict_schema(event.data_schema, where=f"схема события {event.type}")

    def validate(self, module_name: str, *, path: Path | None = None) -> dict[str, Any]:
        module_path = Path(path) if path is not None else self.module_path(module_name)
        manifest = self._manifest(module_path)
        definition, python_module = self._import_module(module_path, manifest)
        try:
            self._check_definition(definition)
        finally:
            self._forget_import(python_module.__name__)
        return self._summary(definition, module_path)

    def load(self, module_name: str, *, start_handlers: bool = True) -> dict[str, Any]:
        if self._closing.is_set():
            raise RuntimeError("runtime_stopping")
        module_path = self.module_path(module_name)
        manifest = self._manifest(module_path)
        definition, python_module = self._import_module(module_path, manifest)
        try:
            self._check_definition(definition)
            with self._lock:
                if self._closing.is_set():
                    raise RuntimeError("runtime_stopping")
                if module_name in self._loaded:
                    self._unload_locked(module_name)
                owner = self._owner(module_name)
                self.actions.replace_owner(
                    [replace(spec, owner=owner) for spec in definition.actions],
                    owner=owner,
                )
                try:
                    self.events.replace_owner(
                        [event for handler in definition.handlers for event in handler.events],
                        owner=owner,
                    )
                except Exception:
                    self.actions.unregister_owner(owner)
                    raise
                runtime = LoadedModule(definition, module_path, python_module)
                self._loaded[module_name] = runtime
                if start_handlers:
                    self._start_handlers_locked(runtime)
        except Exception:
            self._forget_import(python_module.__name__)
            raise
        summary = self._summary(definition, module_path)
        self.debug.log("module_loaded", **summary)
        return summary

    def start_all(self) -> None:
        with self._lock:
            if self._closing.is_set():
                return
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
        try:
            handler.start(context)
        except Exception as exc:  # noqa: BLE001
            if not context.stop_event.is_set():
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
        owner = self._owner(module_name)
        self.actions.unregister_owner(owner)
        self.events.unregister_owner(owner)
        self._forget_import(runtime.python_module.__name__)

    def apply(self, operation: str, module_name: str) -> dict[str, Any]:
        self.validate_name(module_name)
        if operation == "delete":
            self.unload(module_name)
            path = self.module_path(module_name)
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
                for runtime in sorted(
                    self._loaded.values(), key=lambda item: item.definition.name
                )
            ]

    def describe(self, module_name: str) -> dict[str, Any]:
        with self._lock:
            runtime = self._loaded.get(module_name)
            if runtime is None:
                raise KeyError(f"Модуль не подключён: {module_name}")
            return self._summary(runtime.definition, runtime.path)

    def shutdown(self, timeout: float = 3.0) -> None:
        self._closing.set()
        with self._lock:
            names = list(self._loaded)
            for name in names:
                self._unload_locked(name)
        deadline = time.monotonic() + timeout
        for runtime in tuple(self._loaded.values()):
            for thread in runtime.threads:
                thread.join(max(0, deadline - time.monotonic()))

    @staticmethod
    def _owner(module_name: str) -> str:
        return f"module:{module_name}"

    @staticmethod
    def _summary(definition: Module, path: Path) -> dict[str, Any]:
        return {
            "module": definition.name,
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
