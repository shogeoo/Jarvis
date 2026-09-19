"""Загрузка, диспетчеризация и полная выгрузка модулей."""

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
    ActionContext,
    ActionSpec,
    EventDefinition,
    HandlerSpec,
    Module,
    ModuleContext,
)


DEFAULT_MODULES_DIR = DEFAULT_JARVIS_DIR / "modules"
_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


@dataclass(slots=True)
class _ActionJob:
    spec: ActionSpec
    data: dict[str, Any]
    context: ActionContext


@dataclass(slots=True)
class LoadedModule:
    definition: Module
    path: Path
    python_module: ModuleType | None
    execution: str = "in_process"
    contexts: list[ModuleContext] = field(default_factory=list)
    handler_threads: list[threading.Thread] = field(default_factory=list)
    action_jobs: queue.Queue[_ActionJob | None] = field(default_factory=queue.Queue)
    action_thread: threading.Thread | None = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    started: bool = False
    process: subprocess.Popen | None = None
    reader_thread: threading.Thread | None = None
    writer_lock: threading.Lock = field(default_factory=threading.Lock)
    stderr_stream: Any = None


class ModuleManager:
    """Код на диске существует независимо от runtime в оперативной памяти."""

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
        self._disabled_targets: dict[str, list[str]] = {}
        self._closing = threading.Event()

    @staticmethod
    def validate_name(module_id: str) -> None:
        if not isinstance(module_id, str) or not _NAME.fullmatch(module_id):
            raise ValueError(f"Некорректный module_id: {module_id!r}")

    def module_path(self, module_id: str) -> Path:
        self.validate_name(module_id)
        return self.modules_dir / module_id

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

    def existing_names(self) -> set[str]:
        return {path.name for path in self.discover()}

    def loaded_names(self) -> set[str]:
        with self._lock:
            return set(self._loaded)

    def list_existing(self) -> list[dict[str, Any]]:
        result = []
        loaded = self.loaded_names()
        for path in self.discover():
            try:
                manifest = self._manifest(path)
                result.append(
                    {
                        "module_id": manifest["module_id"],
                        "description": manifest["description"],
                        "loaded": path.name in loaded,
                    }
                )
            except Exception as exc:  # noqa: BLE001
                result.append(
                    {
                        "module_id": path.name,
                        "description": "",
                        "loaded": False,
                        "error": str(exc),
                    }
                )
        return result

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
        execution = manifest.get("execution", "in_process")
        requirements = manifest.get("requirements")
        if not isinstance(description, str):
            raise ValueError(f"Описание модуля {module_id} должно быть строкой")
        if (
            not isinstance(entrypoint, str)
            or not entrypoint
            or Path(entrypoint).is_absolute()
            or ".." in Path(entrypoint).parts
        ):
            raise ValueError(f"Некорректный entrypoint модуля {module_id}")
        if not isinstance(factory, str) or not factory.isidentifier():
            raise ValueError(f"Некорректная factory-функция модуля {module_id}")
        if execution not in {"in_process", "isolated"}:
            raise ValueError(f"Неизвестный execution модуля {module_id}: {execution}")
        if requirements is not None and (
            not isinstance(requirements, str)
            or Path(requirements).is_absolute()
            or ".." in Path(requirements).parts
        ):
            raise ValueError(f"Некорректный requirements модуля {module_id}")
        if not (path / "actions").is_dir() or not (path / "handlers").is_dir():
            raise ValueError(
                f"Модуль {module_id} обязан содержать actions/ и handlers/"
            )
        return {
            "module_id": module_id,
            "description": description,
            "entrypoint": entrypoint,
            "factory": factory,
            "execution": execution,
            "requirements": requirements,
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

    def _worker_command(self, path: Path, manifest: dict[str, Any]) -> list[str]:
        python = path / ".venv" / "bin" / "python"
        if not python.is_file():
            raise ValueError(
                f"У изолированного модуля {path.name} нет .venv; "
                "сначала выполни module_manager.prepare_environment"
            )
        return [
            str(python),
            "-m",
            "jarvis.modules.worker",
            "--module",
            str(path.resolve()),
            "--entrypoint",
            manifest["entrypoint"],
            "--factory",
            manifest["factory"],
        ]

    def _worker_env(self) -> dict[str, str]:
        environment = dict(os.environ)
        project_root = str(self.config.project_root if self.config else Path.cwd())
        previous = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = (
            project_root if not previous else project_root + os.pathsep + previous
        )
        return environment

    def _describe_isolated(self, path: Path, manifest: dict[str, Any]) -> Module:
        command = [*self._worker_command(path, manifest), "--describe"]
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
                f"Worker {path.name} не прошёл импорт: {exc.stderr.strip()}"
            ) from exc
        lines = completed.stdout.strip().splitlines()
        if not lines:
            raise RuntimeError(f"Worker {path.name} не вернул описание")
        message = json.loads(lines[-1])
        definition = self._proxy_definition(message["catalog"])
        if definition.module_id != manifest["module_id"]:
            raise ValueError("module_id worker не совпадает с module.json")
        if definition.description != manifest["description"]:
            raise ValueError("Описание worker не совпадает с module.json")
        return definition

    def _proxy_definition(self, catalog: dict[str, Any]) -> Module:
        module_id = catalog["module_id"]

        def proxy(data, context):
            self._send_isolated(
                module_id,
                {
                    "kind": "action",
                    "agent_id": context.agent_id,
                    "action_id": context.action_id,
                    "type": context.action_type,
                    "data": data,
                },
            )

        actions = tuple(
            ActionSpec(item["type"], item["description"], item["data_schema"], proxy)
            for item in catalog["actions"]
        )
        handlers = tuple(
            HandlerSpec(
                item["name"],
                item["description"],
                tuple(
                    EventDefinition(
                        event["type"], event["description"], event["data_schema"]
                    )
                    for event in item["events"]
                ),
                lambda context: None,
            )
            for item in catalog["handlers"]
        )
        return Module(
            module_id=module_id,
            description=catalog["description"],
            actions=actions,
            handlers=handlers,
        )

    @staticmethod
    def _events(definition: Module):
        return [event for handler in definition.handlers for event in handler.events]

    @classmethod
    def _check_definition(cls, definition: Module) -> None:
        prefix = f"{definition.module_id}."
        if definition.prepare is not None and not callable(definition.prepare):
            raise ValueError(
                f"prepare модуля {definition.module_id} должен быть функцией"
            )
        action_names = [spec.type for spec in definition.actions]
        event_names = [event.type for event in cls._events(definition)]
        handler_names = [handler.name for handler in definition.handlers]
        if len(action_names) != len(set(action_names)):
            raise ValueError(f"Модуль {definition.module_id} повторяет actions")
        if len(event_names) != len(set(event_names)):
            raise ValueError(f"Модуль {definition.module_id} повторяет events")
        if len(handler_names) != len(set(handler_names)):
            raise ValueError(f"Модуль {definition.module_id} повторяет handlers")
        for spec in definition.actions:
            if not spec.type.startswith(prefix) or not callable(spec.handler):
                raise ValueError(f"Некорректное действие {spec.type!r}")
            validate_strict_schema(spec.data_schema, where=f"схема действия {spec.type}")
        for handler in definition.handlers:
            if not handler.name or not callable(handler.start):
                raise ValueError(f"Некорректный handler {handler.name!r}")
            for event in handler.events:
                if not event.type.startswith(prefix):
                    raise ValueError(f"Событие {event.type} должно начинаться с {prefix}")
                validate_strict_schema(event.data_schema, where=f"схема события {event.type}")

    def validate(self, module_id: str) -> dict[str, Any]:
        path = self.module_path(module_id)
        manifest = self._manifest(path)
        if manifest["execution"] == "isolated":
            definition = self._describe_isolated(path, manifest)
            self._check_definition(definition)
            return self._summary(definition, path)
        definition, python_module = self._import_module(path, manifest)
        try:
            self._check_definition(definition)
            return self._summary(definition, path)
        finally:
            self._forget_import(python_module.__name__)

    def load_many(self, module_ids: set[str], *, start_handlers: bool) -> None:
        loaded_now = []
        try:
            for module_id in sorted(module_ids):
                if module_id not in self.loaded_names():
                    self.load(module_id, start_handlers=False)
                    loaded_now.append(module_id)
            if start_handlers:
                self.start_many(module_ids)
        except Exception:
            for module_id in reversed(loaded_now):
                self.unload(module_id)
            raise

    def load(self, module_id: str, *, start_handlers: bool = True) -> dict[str, Any]:
        if self._closing.is_set():
            raise RuntimeError("runtime_stopping")
        with self._lock:
            existing = self._loaded.get(module_id)
            if existing is not None:
                if start_handlers:
                    self._start_runtime_locked(existing)
                return self._summary(existing.definition, existing.path)
        path = self.module_path(module_id)
        manifest = self._manifest(path)
        if manifest["execution"] == "isolated":
            definition = self._describe_isolated(path, manifest)
            python_module = None
        else:
            definition, python_module = self._import_module(path, manifest)
        runtime = None
        try:
            self._check_definition(definition)
            if definition.prepare is not None and self.config is not None:
                definition.prepare(self.config)
            owner = self._owner(module_id)
            self.actions.replace_owner(
                [replace(spec, owner=owner) for spec in definition.actions],
                owner=owner,
            )
            try:
                self.events.replace_owner(self._events(definition), owner=owner)
            except Exception:
                self.actions.unregister_owner(owner)
                raise
            runtime = LoadedModule(
                definition,
                path,
                python_module,
                execution=manifest["execution"],
            )
            with self._lock:
                self._loaded[module_id] = runtime
                if start_handlers:
                    self._start_runtime_locked(runtime)
        except Exception:
            if runtime is not None and module_id in self.loaded_names():
                self.unload(module_id)
            else:
                owner = self._owner(module_id)
                self.actions.unregister_owner(owner)
                self.events.unregister_owner(owner)
            if python_module is not None:
                self._forget_import(python_module.__name__)
            raise
        return self._summary(definition, path)

    def start_many(self, module_ids: set[str]) -> None:
        with self._lock:
            for module_id in module_ids:
                runtime = self._loaded.get(module_id)
                if runtime is not None:
                    self._start_runtime_locked(runtime)

    def _start_runtime_locked(self, runtime: LoadedModule) -> None:
        if runtime.started:
            return
        runtime.started = True
        if runtime.execution == "isolated":
            self._start_isolated_locked(runtime)
        runtime.action_thread = threading.Thread(
            target=self._run_actions,
            args=(runtime,),
            name=f"jarvis-actions-{runtime.definition.module_id}",
            daemon=True,
        )
        runtime.action_thread.start()
        if runtime.execution == "isolated":
            return
        for handler in runtime.definition.handlers:
            context = ModuleContext(
                module_id=runtime.definition.module_id,
                module_path=runtime.path,
                emit_event=self.event_bus.publish,
                agent_manager=getattr(self.event_bus, "manager", None),
                modules=self,
                config=self.config,
                services=self.services,
                stop_event=runtime.stop_event,
            )
            runtime.contexts.append(context)
            thread = threading.Thread(
                target=self._run_handler,
                args=(runtime, handler, context),
                name=f"jarvis-handler-{runtime.definition.module_id}-{handler.name}",
                daemon=True,
            )
            runtime.handler_threads.append(thread)
            thread.start()

    def _start_isolated_locked(self, runtime: LoadedModule) -> None:
        manifest = self._manifest(runtime.path)
        log_dir = (self.config.jarvis_dir if self.config else DEFAULT_JARVIS_DIR) / "runtime"
        log_dir.mkdir(parents=True, exist_ok=True)
        runtime.stderr_stream = open(log_dir / f"{runtime.definition.module_id}.log", "ab")
        runtime.process = subprocess.Popen(
            self._worker_command(runtime.path, manifest),
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
                f"Worker {runtime.definition.module_id} не запустился за 30 секунд"
            )
        first = runtime.process.stdout.readline()
        if not first:
            raise RuntimeError(f"Worker {runtime.definition.module_id} не запустился")
        message = json.loads(first)
        if message.get("kind") != "ready":
            raise RuntimeError(f"Worker вернул неожиданный ответ: {message}")
        runtime.reader_thread = threading.Thread(
            target=self._read_isolated,
            args=(runtime,),
            name=f"jarvis-ipc-{runtime.definition.module_id}",
            daemon=True,
        )
        runtime.reader_thread.start()

    def _send_isolated(self, module_id: str, message: dict[str, Any]) -> None:
        with self._lock:
            runtime = self._loaded.get(module_id)
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
                    self._report_error(runtime.definition.module_id, message["error"])
                elif message.get("kind") == "event":
                    raw = message["event"]
                    self.event_bus.publish(
                        Event(
                            type=raw["type"],
                            data=raw["data"],
                            source=f"module:{runtime.definition.module_id}",
                            target=raw.get("target"),
                            reply_to=raw.get("reply_to"),
                            parts=tuple(
                                InputPart(item["type"], item["mime_type"], item["data"])
                                for item in raw.get("parts", [])
                            ),
                            module_id=runtime.definition.module_id,
                        )
                    )
            except Exception as exc:  # noqa: BLE001
                self._report_error(runtime.definition.module_id, exc)
        if not runtime.stop_event.is_set():
            self._report_error(runtime.definition.module_id, "Worker неожиданно завершился")

    def dispatch(self, *, action: ActionRequest, spec: ActionSpec, agent: Any) -> None:
        validate_json(action.data, spec.data_schema, where=f"аргументы {action.type}")
        module_id = spec.owner.removeprefix("module:")
        with self._lock:
            runtime = self._loaded.get(module_id)
            if runtime is None or not runtime.started:
                raise RuntimeError(f"Модуль {module_id} выключен")
            context = ActionContext(
                agent_id=agent.agent_id,
                action_id=action.id,
                action_type=action.type,
                module_id=module_id,
                config=self.config,
                services=self.services,
                metadata={"preset": agent.preset, "agent_name": agent.name},
                stop_event=runtime.stop_event,
            )
            runtime.action_jobs.put(_ActionJob(spec, dict(action.data), context))

    def _run_actions(self, runtime: LoadedModule) -> None:
        while not runtime.stop_event.is_set():
            job = runtime.action_jobs.get()
            if job is None:
                return
            try:
                job.spec.handler(job.data, job.context)
            except Exception as exc:  # noqa: BLE001
                self._report_error(runtime.definition.module_id, exc)

    def _run_handler(
        self, runtime: LoadedModule, handler: HandlerSpec, context: ModuleContext
    ) -> None:
        try:
            handler.start(context)
        except Exception as exc:  # noqa: BLE001
            if not runtime.stop_event.is_set():
                self._report_error(runtime.definition.module_id, exc)

    def _report_error(self, module_id: str, exc: Exception | str) -> None:
        manager = getattr(self.event_bus, "manager", None)
        if manager is not None:
            manager.report_module_error(module_id, exc)
        else:
            self.debug.log("module_error", module_id=module_id, error=str(exc))

    def unload(self, module_id: str) -> None:
        with self._lock:
            runtime = self._loaded.pop(module_id, None)
        if runtime is None:
            return
        runtime.stop_event.set()
        while True:
            try:
                runtime.action_jobs.get_nowait()
            except queue.Empty:
                break
        runtime.action_jobs.put(None)
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
        for context, handler in zip(runtime.contexts, runtime.definition.handlers):
            if handler.stop is not None:
                try:
                    handler.stop(context)
                except Exception as exc:  # noqa: BLE001
                    self._report_error(module_id, exc)
        current = threading.current_thread()
        if runtime.action_thread is not None and runtime.action_thread is not current:
            runtime.action_thread.join(timeout=5)
        for thread in runtime.handler_threads:
            if thread is not current:
                thread.join(timeout=5)
        if runtime.reader_thread is not None and runtime.reader_thread is not current:
            runtime.reader_thread.join(timeout=2)
        if runtime.process is not None:
            for stream in (runtime.process.stdin, runtime.process.stdout):
                if stream is not None:
                    stream.close()
        owner = self._owner(module_id)
        self.actions.unregister_owner(owner)
        self.events.unregister_owner(owner)
        if runtime.python_module is not None:
            self._forget_import(runtime.python_module.__name__)
        if runtime.stderr_stream is not None:
            runtime.stderr_stream.close()

    def disable_for_edit(self, module_id: str) -> dict[str, Any]:
        if module_id in self._disabled_targets:
            return {
                "module_id": module_id,
                "disabled_for": list(self._disabled_targets[module_id]),
            }
        manager = getattr(self.event_bus, "manager", None)
        if manager is None:
            raise RuntimeError("Менеджер агентов не запущен")
        targets = manager.disable_module_everywhere(module_id)
        self._disabled_targets[module_id] = targets
        return {"module_id": module_id, "disabled_for": targets}

    def enable_after_edit(self, module_id: str) -> dict[str, Any]:
        self.validate(module_id)
        targets = self._disabled_targets.get(module_id, [])
        restored = []
        if targets:
            manager = getattr(self.event_bus, "manager", None)
            restored = manager.restore_module(module_id, targets)
        self._disabled_targets.pop(module_id, None)
        return {"module_id": module_id, "restored_for": restored}

    def create_environment(self, module_id: str) -> dict[str, Any]:
        path = self.module_path(module_id)
        manifest = self._manifest(path)
        environment = path / ".venv"
        requirements_name = manifest.get("requirements")
        requirements = None
        if requirements_name:
            requirements = path / requirements_name
            if not requirements.is_file():
                raise ValueError(f"Не найден requirements: {requirements}")
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
        if not environment.exists():
            venv.EnvBuilder(with_pip=True).create(environment)
        if requirements is not None:
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

    def catalog(self, module_ids: set[str]) -> list[dict[str, Any]]:
        with self._lock:
            result = []
            for module_id in sorted(module_ids):
                runtime = self._loaded.get(module_id)
                if runtime is None:
                    continue
                summary = self._summary(runtime.definition, runtime.path)
                result.append(
                    {
                        "module_id": module_id,
                        "description": summary["description"],
                        "actions": summary["actions"],
                        "events": summary["events"],
                    }
                )
            return result

    def shutdown(self) -> None:
        self._closing.set()
        for module_id in list(self.loaded_names()):
            self.unload(module_id)

    @staticmethod
    def _owner(module_id: str) -> str:
        return f"module:{module_id}"

    @classmethod
    def _summary(cls, definition: Module, path: Path) -> dict[str, Any]:
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
                for event in cls._events(definition)
            ],
            "handlers": [
                {"name": handler.name, "description": handler.description}
                for handler in definition.handlers
            ],
        }
