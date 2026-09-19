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

from ..core.protocol import validate_strict_schema
from ..core.registry import ActionRegistry, EventRegistry
from ..infrastructure.config import DEFAULT_JARVIS_DIR
from ..infrastructure.debug import Debugger
from .api import HandlerSpec, Module, ModuleContext


DEFAULT_MODULES_DIR = DEFAULT_JARVIS_DIR / "modules"
_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


@dataclass(slots=True)
class LoadedModule:
    definition: Module
    path: Path
    python_module: ModuleType
    contexts: list[ModuleContext] = field(default_factory=list)
    threads: list[threading.Thread] = field(default_factory=list)


class ModuleManager:
    """Сканирует .jarvis/modules и регистрирует модули глобально."""

    def __init__(
        self,
        event_bus: Any,
        actions: ActionRegistry,
        events: EventRegistry,
        *,
        modules_dir: Path = DEFAULT_MODULES_DIR,
        config: Any = None,
        debug: Debugger | None = None,
        services: dict[str, Any] | None = None,
    ):
        self.event_bus = event_bus
        self.actions = actions
        self.events = events
        self.modules_dir = Path(modules_dir)
        self.config = config
        self.debug = debug or Debugger(enabled=False)
        self.services = services if services is not None else {}
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

    def loaded_names(self) -> set[str]:
        with self._lock:
            return set(self._loaded)

    def load_all(
        self,
        *,
        start_handlers: bool = False,
        strict: bool = True,
    ) -> list[dict[str, Any]]:
        loaded = []
        for path in self.discover():
            if path.name in self._loaded:
                continue
            try:
                loaded.append(self.load(path.name, start_handlers=start_handlers))
            except Exception as exc:  # noqa: BLE001
                if strict:
                    raise RuntimeError(
                        f"Не удалось загрузить модуль {path.name}: {exc}"
                    ) from exc
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
        module_id = manifest.get("module_id")
        if not isinstance(module_id, str) or not _NAME.fullmatch(module_id):
            raise ValueError(f"Некорректный module_id: {module_id!r}")
        if module_id != path.name:
            raise ValueError(
                f"module_id {module_id!r} не совпадает с каталогом {path.name!r}"
            )
        description = manifest.get("description", "")
        entrypoint = manifest.get("entrypoint")
        factory = manifest.get("factory")
        if not isinstance(description, str):
            raise ValueError(f"Описание модуля {module_id} должно быть строкой")
        if not isinstance(entrypoint, str) or not entrypoint or Path(entrypoint).is_absolute():
            raise ValueError(f"Некорректный entrypoint модуля {module_id}")
        if not isinstance(factory, str) or not factory.isidentifier():
            raise ValueError(f"Некорректная factory-функция модуля {module_id}")
        if not (path / "actions").is_dir() or not (path / "handlers").is_dir():
            raise ValueError(
                f"Модуль {module_id} обязан содержать actions/ и handlers/"
            )
        return {
            "module_id": module_id,
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
            raise ValueError(
                f"В модуле {manifest['module_id']} нет файла {source.name}"
            )
        safe_name = re.sub(r"[^A-Za-z0-9_]", "_", manifest["module_id"])
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
        if definition.module_id != manifest["module_id"]:
            cls._forget_import(import_name)
            raise ValueError("module_id объекта Module не совпадает с module.json")
        if definition.description != manifest["description"]:
            cls._forget_import(import_name)
            raise ValueError("Описание Module не совпадает с module.json")
        return definition, python_module

    @staticmethod
    def _check_definition(definition: Module) -> None:
        prefix = f"{definition.module_id}."
        if definition.prepare is not None and not callable(definition.prepare):
            raise ValueError(
                f"prepare модуля {definition.module_id} должен быть функцией"
            )
        action_names = [spec.type for spec in definition.actions]
        if len(action_names) != len(set(action_names)):
            raise ValueError(
                f"Модуль {definition.module_id} объявляет повторные действия"
            )
        declared_events = [
            *definition.events,
            *(event for handler in definition.handlers for event in handler.events),
        ]
        event_names = [event.type for event in declared_events]
        if len(event_names) != len(set(event_names)):
            raise ValueError(
                f"Модуль {definition.module_id} объявляет повторные события"
            )
        handler_names = [handler.name for handler in definition.handlers]
        if len(handler_names) != len(set(handler_names)):
            raise ValueError(
                f"Модуль {definition.module_id} объявляет повторные обработчики"
            )
        for spec in definition.actions:
            if not spec.type or not callable(spec.handler):
                raise ValueError(f"Некорректное действие {spec.type!r}")
            if not spec.type.startswith(prefix):
                raise ValueError(
                    f"Действие {spec.type} должно начинаться с {prefix}"
                )
            validate_strict_schema(spec.data_schema, where=f"схема действия {spec.type}")
        for event_definition in definition.events:
            if not event_definition.type.startswith(prefix):
                raise ValueError(
                    f"Событие {event_definition.type} должно начинаться с {prefix}"
                )
            validate_strict_schema(
                event_definition.data_schema,
                where=f"схема события {event_definition.type}",
            )
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
            if definition.prepare is not None and self.config is not None:
                definition.prepare(self.config)
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
                        [
                            *definition.events,
                            *(event for handler in definition.handlers for event in handler.events),
                        ],
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
                module_id=runtime.definition.module_id,
                module_path=runtime.path,
                emit_event=self.event_bus.publish,
                config=self.config,
                services=self.services,
            )
            runtime.contexts.append(context)
            thread = threading.Thread(
                target=self._run_handler,
                args=(runtime, handler, context),
                name=f"jarvis-module-{runtime.definition.module_id}-{handler.name}",
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
                self.debug.log(
                    "handler_error",
                    module=runtime.definition.module_id,
                    handler=handler.name,
                    error=str(exc),
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

    def apply(
        self,
        operation: str,
        module_name: str,
        *,
        requested_by: str | None = None,
        requester_agent_id: str | None = None,
    ) -> dict[str, Any]:
        self.validate_name(module_name)
        manager = getattr(self.event_bus, "manager", None)
        if requested_by == module_name:
            raise ValueError("Модуль не может горячо заменить сам себя")
        if manager is not None:
            if (
                requester_agent_id is not None
                and manager.agent_turn_uses(requester_agent_id, module_name)
            ):
                raise ValueError(
                    "Нельзя обновить модуль, используемый текущим циклом "
                    "агента управления модулями"
                )
            manager.wait_module_idle(module_name)
        if operation == "delete":
            if manager is not None:
                used_by = [
                    preset.name
                    for preset in manager.presets.list()
                    if module_name in preset.modules
                ]
                if used_by:
                    raise ValueError(
                        f"Модуль {module_name} используется пресетами: {used_by}"
                    )
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
                    self._loaded.values(), key=lambda item: item.definition.module_id
                )
            ]

    def describe(self, module_name: str) -> dict[str, Any]:
        with self._lock:
            runtime = self._loaded.get(module_name)
            if runtime is None:
                raise KeyError(f"Модуль не подключён: {module_name}")
            return self._summary(runtime.definition, runtime.path)

    def catalog(self, module_ids: set[str]) -> list[dict[str, Any]]:
        """Вернуть model-visible каталог модулей без внутренних путей."""

        with self._lock:
            catalog = []
            for module_id in sorted(module_ids):
                runtime = self._loaded.get(module_id)
                if runtime is None:
                    continue
                summary = self._summary(runtime.definition, runtime.path)
                catalog.append(
                    {
                        "module_id": module_id,
                        "description": summary["description"],
                        "actions": summary["actions"],
                        "events": summary["events"],
                    }
                )
            return catalog

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
            "module": definition.module_id,
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
                for event in (
                    *definition.events,
                    *(event for handler in definition.handlers for event in handler.events),
                )
            ],
            "handlers": [
                {"name": handler.name, "description": handler.description}
                for handler in definition.handlers
            ],
        }
