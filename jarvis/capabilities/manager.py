"""Загрузка, диспетчеризация и полная выгрузка capabilities.

Каждая единица (``actions/<id>``, ``handlers/<id>``, ``modules/<id>``) —
самодостаточный каталог со своим ``.venv``. Ядро не импортирует код единиц:
оно запускает unit-host из окружения единицы, получает JSON-каталог и
общается с ним по JSON Lines. Управляющие вызовы к живому состоянию ядра
приходят от единиц отдельными RPC-сообщениями.
"""

from __future__ import annotations

import json
import os
import re
import select
import subprocess
import sys
import threading
import venv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.lifecycle import terminate_process
from ..core.protocol import (
    ActionRequest,
    CallResult,
    Event,
    InputPart,
    validate_json,
)
from ..core.registry import ActionRegistry, EventRegistry
from ..infrastructure.config import DEFAULT_JARVIS_DIR
from ..infrastructure.debug import Debugger
from .api import (
    PENDING,
    ActionContext,
    ActionDefinition,
    EventDefinition,
    HandlerDefinition,
    ModuleDefinition,
)


DEFAULT_ROOT = DEFAULT_JARVIS_DIR
_ENTRYPOINTS = {"action": "action.py", "handler": "handler.py", "module": "module.py"}
_DIRS = {"action": "actions", "handler": "handlers", "module": "modules"}
# Точка в ID корневой единицы запрещена: она означает единицу модуля.
_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


@dataclass(slots=True)
class UnitHost:
    """Живой процесс одной единицы (действие, handler или модуль)."""

    key: str
    kind: str
    unit_id: str
    module_id: str | None
    path: Path
    catalog: dict[str, Any]
    action_ids: list[str] = field(default_factory=list)
    handler_ids: list[str] = field(default_factory=list)
    process: subprocess.Popen | None = None
    reader_thread: threading.Thread | None = None
    writer_lock: threading.Lock = field(default_factory=threading.Lock)
    start_lock: threading.Lock = field(default_factory=threading.Lock)
    stderr_stream: Any = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    started: bool = False
    closing: bool = False


@dataclass(slots=True)
class LoadedAction:
    id: str
    definition: ActionDefinition
    path: Path
    host: UnitHost
    module_id: str | None


@dataclass(slots=True)
class LoadedHandler:
    id: str
    definition: HandlerDefinition
    path: Path
    host: UnitHost
    module_id: str | None


@dataclass(slots=True)
class LoadedModule:
    module_id: str
    definition: ModuleDefinition
    path: Path
    host: UnitHost
    action_ids: list[str] = field(default_factory=list)
    handler_ids: list[str] = field(default_factory=list)


class CapabilityManager:
    """Каталоги единиц на диске существуют независимо от runtime в памяти."""

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
        self._global_toggle_lock = threading.RLock()
        self._hosts: dict[str, UnitHost] = {}
        self._actions: dict[str, LoadedAction] = {}
        self._handlers: dict[str, LoadedHandler] = {}
        self._modules: dict[str, LoadedModule] = {}
        self._global_state_path = self.root / "capability_state.json"
        try:
            raw_state = json.loads(self._global_state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raw_state = {}
        old_paused = raw_state.get("paused", {})
        self._global_paused = {
            kind: set(old_paused.get(kind, raw_state.get(kind, [])))
            for kind in ("modules", "actions", "handlers")
        }
        self._save_global_state()
        self._agent_api = _AgentApi(self)
        self._closing = threading.Event()
        self._cancel_waiters: dict[tuple[str, str], threading.Event] = {}

    # --- пути и обнаружение ------------------------------------------
    @staticmethod
    def validate_module_id(module_id: str) -> None:
        if not isinstance(module_id, str) or not _NAME.fullmatch(module_id):
            raise ValueError(f"Некорректный module_id: {module_id!r}")

    @staticmethod
    def validate_unit_id(unit_id: str, *, kind: str) -> None:
        if not isinstance(unit_id, str) or not _NAME.fullmatch(unit_id):
            raise ValueError(f"Некорректный {kind}: {unit_id!r}")

    def _unit_path(self, kind: str, unit_id: str) -> Path:
        if kind == "module":
            self.validate_module_id(unit_id)
            return self.modules_dir / unit_id
        self.validate_unit_id(unit_id, kind=kind)
        return (self.actions_dir if kind == "action" else self.handlers_dir) / unit_id

    def action_path(self, action_id: str) -> Path:
        return self._unit_path("action", action_id)

    def handler_path(self, handler_id: str) -> Path:
        return self._unit_path("handler", handler_id)

    def module_path(self, module_id: str) -> Path:
        return self._unit_path("module", module_id)

    @staticmethod
    def _rel(kind: str, unit_id: str) -> str:
        return f"{_DIRS[kind]}/{unit_id}"

    def discover_modules(self) -> list[Path]:
        return self._discover_units(self.modules_dir, "module")

    def discover_actions(self) -> list[Path]:
        return self._discover_units(self.actions_dir, "action")

    def discover_handlers(self) -> list[Path]:
        return self._discover_units(self.handlers_dir, "handler")

    @staticmethod
    def _discover_units(directory: Path, kind: str) -> list[Path]:
        if not directory.exists():
            return []
        entry = _ENTRYPOINTS[kind]
        return sorted(
            path
            for path in directory.iterdir()
            if path.is_dir()
            and not path.name.startswith(".")
            and (path / entry).is_file()
        )

    def existing_modules(self) -> set[str]:
        return {path.name for path in self.discover_modules()}

    def existing_actions(self) -> set[str]:
        return {path.name for path in self.discover_actions()}

    def existing_handlers(self) -> set[str]:
        return {path.name for path in self.discover_handlers()}

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
        global_paused = self.global_paused()
        modules = [
            {
                "id": path.name,
                "description": self._describe("module", path.name)["description"],
                "loaded": path.name in loaded["modules"],
                "globally_running": path.name not in global_paused["modules"],
                "globally_paused": path.name in global_paused["modules"],
            }
            for path in self.discover_modules()
        ]
        actions = [
            {
                "id": path.name,
                "description": self._describe("action", path.name)["actions"][0]["description"],
                "loaded": path.name in loaded["actions"],
                "globally_running": path.name not in global_paused["actions"],
                "globally_paused": path.name in global_paused["actions"],
            }
            for path in self.discover_actions()
        ]
        handlers = [
            {
                "id": path.name,
                "description": self._describe("handler", path.name)["handlers"][0]["description"],
                "loaded": path.name in loaded["handlers"],
                "globally_running": path.name not in global_paused["handlers"],
                "globally_paused": path.name in global_paused["handlers"],
            }
            for path in self.discover_handlers()
        ]
        return {"modules": modules, "actions": actions, "handlers": handlers}

    def capability_info(self, *, kind: str, capability_id: str) -> dict[str, Any]:
        if kind == "module":
            return self._describe("module", capability_id)
        if kind == "action":
            return self._describe("action", capability_id)
        if kind == "handler":
            return self._describe("handler", capability_id)
        raise ValueError(f"Неизвестный вид capability: {kind!r}")

    def list_available(self, known: dict[str, set[str]]) -> dict[str, Any]:
        result = {"modules": [], "actions": [], "handlers": []}
        for kind, paths, key in (
            ("module", self.discover_modules(), "modules"),
            ("action", self.discover_actions(), "actions"),
            ("handler", self.discover_handlers(), "handlers"),
        ):
            for path in paths:
                if path.name in known[key]:
                    continue
                catalog = self._describe(kind, path.name)
                result[key].append({"id": path.name, "description": catalog["description"]})
        return result

    def module_action_ids(self, module_id: str) -> set[str]:
        with self._lock:
            runtime = self._modules.get(module_id)
        if runtime is not None:
            return set(runtime.action_ids)
        return {item["id"] for item in self._describe("module", module_id)["actions"]}

    def known_action_definitions(self, snapshot: dict[str, set[str]]) -> dict[str, ActionDefinition]:
        result: dict[str, ActionDefinition] = {}
        for action_id in snapshot["actions"]:
            spec = self.actions.get(action_id)
            if spec is None:
                item = self._describe("action", action_id)["actions"][0]
                spec = self._proxy_action(item, f"action:{action_id}", self.action_path(action_id))
            result[action_id] = spec
        for module_id in snapshot["modules"]:
            with self._lock:
                runtime = self._modules.get(module_id)
            catalog = runtime.host.catalog if runtime else self._describe("module", module_id)
            for item in catalog["actions"]:
                spec = self.actions.get(item["id"])
                if spec is None:
                    spec = self._proxy_action(item, f"module:{module_id}", self.module_path(module_id))
                result[item["id"]] = spec
        return result

    def _loaded_description(self, kind: str, unit_id: str) -> str:
        with self._lock:
            if kind == "module":
                runtime = self._modules.get(unit_id)
                return runtime.host.catalog.get("description", "") if runtime else ""
            table = self._actions if kind == "action" else self._handlers
            loaded = table.get(unit_id)
            return loaded.definition.description if loaded else ""

    # --- процесс единицы ---------------------------------------------
    def _python(self, path: Path) -> Path:
        """Окружение единицы; без собственного .venv используется ядро."""

        python = path / ".venv" / "bin" / "python"
        if python.is_file():
            return python
        return Path(sys.executable)

    def _worker_command(self, kind: str, unit_id: str) -> list[str]:
        path = self._unit_path(kind, unit_id)
        return [
            str(self._python(path)),
            "-m",
            "jarvis.capabilities.worker",
            "--jarvis-dir",
            str(self.root.resolve()),
            "--unit",
            self._rel(kind, unit_id),
        ]

    def _worker_env(self) -> dict[str, str]:
        environment = dict(os.environ)
        project_root = str(self.config.project_root if self.config else Path.cwd())
        package_root = str(Path(__file__).resolve().parents[2])
        paths = []
        for item in (project_root, package_root):
            if item not in paths:
                paths.append(item)
        previous = environment.get("PYTHONPATH")
        if previous:
            paths.append(previous)
        environment["PYTHONPATH"] = os.pathsep.join(paths)
        return environment

    def _cwd(self) -> Path:
        return self.config.project_root if self.config else Path.cwd()

    def _describe(self, kind: str, unit_id: str) -> dict[str, Any]:
        command = [*self._worker_command(kind, unit_id), "--describe"]
        completed = subprocess.run(
            command,
            cwd=self._cwd(),
            env=self._worker_env(),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        lines = completed.stdout.strip().splitlines()
        if lines:
            try:
                message = json.loads(lines[-1])
            except json.JSONDecodeError:
                message = {}
            if message.get("kind") == "unit_error":
                raise ValueError(
                    message.get("error") or f"Единица {unit_id} не загрузилась"
                )
            if message.get("kind") == "description":
                catalog = message["catalog"]
                if catalog.get("unit_id") != unit_id:
                    raise ValueError("unit_id не совпадает с каталогом единицы")
                return catalog
        raise RuntimeError(
            f"Единица {unit_id} не прошла импорт: "
            f"{(completed.stderr or completed.stdout or '').strip()}"
        )

    def _host_log(self, host: UnitHost):
        log_dir = (self.config.jarvis_dir if self.config else DEFAULT_ROOT) / "runtime" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        return open(log_dir / f"{host.kind}-{host.unit_id}.log", "ab")

    def _start_host(self, host: UnitHost) -> None:
        with host.start_lock:
            with self._lock:
                if host.started:
                    return
            host.stderr_stream = self._host_log(host)
            process = subprocess.Popen(
                self._worker_command(host.kind, host.unit_id),
                cwd=self._cwd(),
                env=self._worker_env(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=host.stderr_stream,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
            host.process = process
            try:
                ready, _, _ = select.select([process.stdout], [], [], 30)
                if not ready:
                    raise TimeoutError(
                        f"Единица {host.unit_id} не запустилась за 30 секунд"
                    )
                first = process.stdout.readline()
                message = json.loads(first) if first else {}
                if message.get("kind") != "ready":
                    raise RuntimeError(
                        f"Единица {host.unit_id} не сообщила о готовности"
                    )
            except Exception:
                host.closing = True
                host.stop_event.set()
                terminate_process(process, group=True)
                raise
            host.reader_thread = threading.Thread(
                target=self._read_host,
                args=(host,),
                name=f"jarvis-unit-{host.kind}-{host.unit_id}",
                daemon=True,
            )
            host.reader_thread.start()
            with self._lock:
                host.started = True

    def _send_host(self, host: UnitHost, message: dict[str, Any]) -> None:
        process = host.process
        if process is None or process.stdin is None:
            raise RuntimeError(f"Процесс единицы {host.unit_id} не запущен")
        with host.writer_lock:
            process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            process.stdin.flush()

    def _stop_host(self, host: UnitHost) -> None:
        host.closing = True
        host.stop_event.set()
        process = host.process
        if process is not None:
            try:
                if process.stdin is not None:
                    with host.writer_lock:
                        process.stdin.write('{"kind":"shutdown"}\n')
                        process.stdin.flush()
            except Exception:
                pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                terminate_process(process, group=True)
            for stream in (process.stdin, process.stdout):
                try:
                    if stream is not None:
                        stream.close()
                except Exception:
                    pass
        current = threading.current_thread()
        if host.reader_thread is not None and host.reader_thread is not current:
            host.reader_thread.join(timeout=2)
        if host.stderr_stream is not None:
            try:
                host.stderr_stream.close()
            except Exception:
                pass
            host.stderr_stream = None
        host.process = None
        host.reader_thread = None
        self.actions.unregister_owner(host.key)
        self.events.unregister_owner(host.key)
        with self._lock:
            host.started = False
            self._hosts.pop(host.key, None)

    def _read_host(self, host: UnitHost) -> None:
        process = host.process
        if process is None or process.stdout is None:
            return
        for line in process.stdout:
            try:
                message = json.loads(line)
                kind = message.get("kind")
                if kind == "rpc":
                    threading.Thread(
                        target=self._handle_rpc,
                        args=(host, message),
                        name=f"jarvis-rpc-{host.unit_id}",
                        daemon=True,
                    ).start()
                elif kind == "capability_error":
                    self._fail_call(host, message.get("agent_id"), message.get("call_id"), message.get("error", ""))
                elif kind == "call_result":
                    self._complete_action(
                        message.get("agent_id"),
                        message.get("call_id"),
                        message.get("data"),
                        tuple(
                            InputPart(item["type"], item["mime_type"], item["data"], item.get("name", ""))
                            for item in message.get("parts", [])
                        ),
                    )
                elif kind == "cancelled":
                    with self._lock:
                        waiter = self._cancel_waiters.get((message.get("agent_id"), message.get("call_id")))
                    if waiter is not None:
                        waiter.set()
                elif kind == "event":
                    raw = message["event"]
                    self.event_bus.publish(
                        Event(
                            type=raw["type"],
                            data=raw["data"],
                            source=host.key,
                            target=raw.get("target"),
                            reply_to=raw.get("reply_to"),
                            parts=tuple(
                                InputPart(item["type"], item["mime_type"], item["data"], item.get("name", ""))
                                for item in raw.get("parts", [])
                            ),
                            module_id=host.module_id,
                            handler_id=raw.get("handler_id"),
                        )
                    )
            except Exception as exc:  # noqa: BLE001
                self._host_failed(host, f"Нарушение протокола capability: {exc}")
                break
        with self._lock:
            host.started = False
        if not host.closing and not host.stop_event.is_set():
            self._host_failed(host, "Процесс единицы неожиданно завершился")

    def _fail_call(self, host: UnitHost, agent_id: str | None, call_id: str | None, error: str) -> None:
        manager = self._manager()
        if agent_id is not None and call_id is not None and manager is not None:
            manager.fail_call(agent_id, call_id, error)
        else:
            self._host_failed(host, error)

    def _host_failed(self, host: UnitHost, error: str) -> None:
        manager = self._manager()
        action_ids = set(host.action_ids) or ({host.unit_id} if host.kind == "action" else set())
        if manager is not None:
            for agent in manager.agents_snapshot():
                for pending in manager.results.for_capability(action_ids, {agent.agent_id}):
                    manager.fail_call(agent.agent_id, pending.call_id, error)
        if host.kind == "module":
            self.unload_module(host.unit_id)
        elif host.kind == "action":
            self.unload_action(host.unit_id)
        else:
            self.unload_handler(host.unit_id)
        self.debug.log("capability_error", capability=host.key, error=error)

    def cancel_call(self, agent_id: str, call_id: str) -> None:
        manager = self._manager()
        if manager is None:
            return
        pending = next((item for item in manager.results.for_agent(agent_id) if item.call_id == call_id), None)
        if pending is None:
            return
        self.cancel_execution(agent_id, call_id, pending.action_id)

    def cancel_execution(self, agent_id: str, call_id: str, action_id: str) -> None:
        with self._lock:
            runtime = self._actions.get(action_id)
        if runtime is not None and runtime.host.started:
            key = (agent_id, call_id)
            waiter = threading.Event()
            with self._lock:
                self._cancel_waiters[key] = waiter
            try:
                self._send_host(runtime.host, {"kind": "cancel_action", "agent_id": agent_id, "call_id": call_id})
                if not waiter.wait(3):
                    self._host_failed(runtime.host, "Не удалось остановить выполнение действия")
            finally:
                with self._lock:
                    self._cancel_waiters.pop(key, None)

    # --- RPC от единиц ------------------------------------------------
    def _handle_rpc(self, host: UnitHost, message: dict[str, Any]) -> None:
        rpc_id = message.get("rpc_id")
        try:
            target = message.get("target")
            if target == "capabilities":
                obj: Any = self
            elif target == "agent_manager":
                obj = self._agent_api
                method = str(message.get("method", ""))
                if method in {"enable", "disable", "delete", "interrupt"}:
                    kwargs = message.get("kwargs", {})
                    args = message.get("args", [])
                    target_id = kwargs.get("agent_id") or (args[0] if args else None)
                    if target_id is not None:
                        agent = self._manager().require_agent(target_id)
                        if self._manager().presets.load(agent.preset).protected and message.get("caller_agent_id") != target_id:
                            raise ValueError("Защищённый агент не может быть изменён извне")
            else:
                raise ValueError(f"Неизвестная цель RPC: {target!r}")
            func = obj
            for part in str(message.get("method", "")).split("."):
                func = getattr(func, part)
            value = func(
                *message.get("args", []),
                **message.get("kwargs", {}),
            )
            response = {
                "kind": "rpc_result",
                "rpc_id": rpc_id,
                "ok": True,
                "value": value,
                "error": None,
            }
        except Exception as exc:  # noqa: BLE001
            response = {
                "kind": "rpc_result",
                "rpc_id": rpc_id,
                "ok": False,
                "value": None,
                "error": str(exc),
            }
        try:
            self._send_host(host, response)
        except Exception:  # noqa: BLE001
            pass

    # --- загрузка -----------------------------------------------------
    def _proxy_action(
        self, item: dict[str, Any], owner: str, path: Path
    ) -> ActionDefinition:
        return ActionDefinition(
            id=item["id"],
            description=item["description"],
            args_schema=item["args_schema"],
            result_schema=item["result_schema"],
            run=lambda data, context: None,
            source=str(path / "action.py"),
            owner=owner,
        )

    def _proxy_handler(
        self, item: dict[str, Any], owner: str, path: Path
    ) -> HandlerDefinition:
        event = item["event"]
        return HandlerDefinition(
            id=item["id"],
            description=item["description"],
            event=EventDefinition(
                event["type"],
                event["description"],
                event["data_schema"],
            ),
            start=lambda context: None,
            source=str(path / "handler.py"),
            owner=owner,
        )

    def validate_action(self, action_id: str) -> dict[str, Any]:
        path = self.action_path(action_id)
        if not path.is_dir():
            raise ValueError(f"Действие не найдено: {action_id}")
        catalog = self._describe("action", action_id)
        items = catalog["actions"]
        if len(items) != 1 or items[0]["id"] != action_id:
            raise ValueError(f"Каталог действия {action_id} некорректен")
        definition = self._proxy_action(items[0], f"action:{action_id}", path)
        return self._action_summary(definition, path)

    def validate_handler(self, handler_id: str) -> dict[str, Any]:
        path = self.handler_path(handler_id)
        if not path.is_dir():
            raise ValueError(f"Handler не найден: {handler_id}")
        catalog = self._describe("handler", handler_id)
        items = catalog["handlers"]
        if len(items) != 1 or items[0]["id"] != handler_id:
            raise ValueError(f"Каталог handler {handler_id} некорректен")
        definition = self._proxy_handler(items[0], f"handler:{handler_id}", path)
        return self._handler_summary(definition, path)

    def validate_module(self, module_id: str) -> dict[str, Any]:
        path = self.module_path(module_id)
        if not path.is_dir():
            raise ValueError(f"Модуль не найден: {module_id}")
        catalog = self._describe("module", module_id)
        return {
            "module_id": module_id,
            "description": catalog["description"],
            "actions": [
                self._action_summary(
                    self._proxy_action(item, f"module:{module_id}", path),
                    path / "actions" / item["id"].split(".", 1)[1],
                )
                for item in catalog["actions"]
            ],
            "handlers": [
                self._handler_summary(
                    self._proxy_handler(item, f"module:{module_id}", path),
                    path / "handlers" / item["id"].split(".", 1)[1],
                )
                for item in catalog["handlers"]
            ],
        }

    def validate(self, *, kind: str, capability_id: str) -> dict[str, Any]:
        if kind == "module":
            return self.validate_module(capability_id)
        if kind == "action":
            if "." in capability_id:
                module_id = capability_id.split(".", 1)[0]
                if module_id in self.existing_modules():
                    raise ValueError(
                        f"Часть модуля нельзя проверить отдельно: {capability_id!r}"
                    )
            if self.action_path(capability_id).is_dir():
                return self.validate_action(capability_id)
            raise ValueError(f"Действие не найдено: {capability_id}")
        if kind == "handler":
            if "." in capability_id:
                module_id = capability_id.split(".", 1)[0]
                if module_id in self.existing_modules():
                    raise ValueError(
                        f"Часть модуля нельзя проверить отдельно: {capability_id!r}"
                    )
            if self.handler_path(capability_id).is_dir():
                return self.validate_handler(capability_id)
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

    def load_action(self, action_id: str, *, start_handlers: bool = True) -> dict[str, Any]:
        if self._closing.is_set():
            raise RuntimeError("runtime_stopping")
        with self._lock:
            existing = self._actions.get(action_id)
        if existing is not None:
            if start_handlers:
                self.start_action(action_id)
            return self._action_summary(existing.definition, existing.path)
        path = self.actions_dir / action_id
        if "." in action_id:
            module_id = action_id.split(".", 1)[0]
            if module_id in self.existing_modules():
                raise ValueError(
                    f"Часть модуля нельзя загрузить отдельно: {action_id!r}"
                )
            raise ValueError(f"Действие не найдено: {action_id}")
        if not path.is_dir():
            raise ValueError(f"Действие не найдено: {action_id}")
        catalog = self._describe("action", action_id)
        items = catalog["actions"]
        if len(items) != 1 or items[0]["id"] != action_id:
            raise ValueError(f"Каталог действия {action_id} некорректен")
        host = self._new_host("action", action_id, path, catalog)
        owner = host.key
        definition = self._proxy_action(items[0], owner, path)
        try:
            self.actions.replace_owner([definition], owner=owner)
        except Exception:
            self._forget_host(host)
            raise
        runtime = LoadedAction(
            id=action_id, definition=definition, path=path, host=host, module_id=None
        )
        with self._lock:
            self._actions[action_id] = runtime
        if start_handlers:
            try:
                self.start_action(action_id)
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
        path = self.handlers_dir / handler_id
        if "." in handler_id:
            module_id = handler_id.split(".", 1)[0]
            if module_id in self.existing_modules():
                raise ValueError(
                    f"Часть модуля нельзя загрузить отдельно: {handler_id!r}"
                )
            raise ValueError(f"Handler не найден: {handler_id}")
        if not path.is_dir():
            raise ValueError(f"Handler не найден: {handler_id}")
        catalog = self._describe("handler", handler_id)
        items = catalog["handlers"]
        if len(items) != 1 or items[0]["id"] != handler_id:
            raise ValueError(f"Каталог handler {handler_id} некорректен")
        host = self._new_host("handler", handler_id, path, catalog)
        owner = host.key
        definition = self._proxy_handler(items[0], owner, path)
        try:
            self.events.replace_owner([definition.event], owner=owner)
        except Exception:
            self._forget_host(host)
            raise
        runtime = LoadedHandler(
            id=handler_id, definition=definition, path=path, host=host, module_id=None
        )
        with self._lock:
            self._handlers[handler_id] = runtime
        if start_handlers:
            try:
                self.start_handler(handler_id)
            except Exception:
                self.unload_handler(handler_id)
                raise
        return self._handler_summary(runtime.definition, path)

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
        if not path.is_dir():
            raise ValueError(f"Модуль не найден: {module_id}")
        catalog = self._describe("module", module_id)
        host = self._new_host("module", module_id, path, catalog)
        owner = host.key
        actions = [
            self._proxy_action(item, owner, path / "actions" / item["id"].split(".", 1)[1])
            for item in catalog["actions"]
        ]
        handlers = [
            self._proxy_handler(item, owner, path / "handlers" / item["id"].split(".", 1)[1])
            for item in catalog["handlers"]
        ]
        try:
            self.actions.replace_owner(actions, owner=owner)
            self.events.replace_owner([item.event for item in handlers], owner=owner)
        except Exception:
            self.actions.unregister_owner(owner)
            self.events.unregister_owner(owner)
            self._forget_host(host)
            raise
        definition = ModuleDefinition(
            description=catalog["description"],
            actions=tuple(actions),
            handlers=tuple(handlers),
        )
        host.action_ids = [item.id for item in actions]
        host.handler_ids = [item.id for item in handlers]
        runtime = LoadedModule(
            module_id=module_id,
            definition=definition,
            path=path,
            host=host,
            action_ids=list(host.action_ids),
            handler_ids=list(host.handler_ids),
        )
        with self._lock:
            for item in actions:
                self._actions[item.id] = LoadedAction(
                    id=item.id,
                    definition=item,
                    path=path,
                    host=host,
                    module_id=module_id,
                )
            for item in handlers:
                self._handlers[item.id] = LoadedHandler(
                    id=item.id,
                    definition=item,
                    path=path,
                    host=host,
                    module_id=module_id,
                )
            self._modules[module_id] = runtime
        if start_handlers:
            try:
                self.start_module(module_id)
            except Exception:
                self.unload_module(module_id)
                raise
        return self._module_summary(runtime)

    def _new_host(
        self, kind: str, unit_id: str, path: Path, catalog: dict[str, Any]
    ) -> UnitHost:
        host = UnitHost(
            key=f"{kind}:{unit_id}",
            kind=kind,
            unit_id=unit_id,
            module_id=unit_id if kind == "module" else None,
            path=path,
            catalog=catalog,
        )
        with self._lock:
            self._hosts[host.key] = host
        return host

    def _forget_host(self, host: UnitHost) -> None:
        with self._lock:
            self._hosts.pop(host.key, None)

    def start_module(self, module_id: str) -> None:
        with self._lock:
            runtime = self._modules.get(module_id)
        if runtime is None:
            raise RuntimeError(f"Модуль {module_id} не загружен")
        self._start_host(runtime.host)

    def start_action(self, action_id: str) -> None:
        with self._lock:
            runtime = self._actions.get(action_id)
        if runtime is None:
            raise RuntimeError(f"Действие {action_id} не загружено")
        self._start_host(runtime.host)

    def start_handler(self, handler_id: str) -> None:
        with self._lock:
            runtime = self._handlers.get(handler_id)
        if runtime is None:
            raise RuntimeError(f"Handler {handler_id} не загружен")
        self._start_host(runtime.host)

    # --- dispatch -----------------------------------------------------
    def dispatch(self, *, action: ActionRequest, spec: ActionDefinition, agent: Any) -> None:
        validate_json(action.data, spec.args_schema, where=f"аргументы {action.action_id}")
        if not agent.is_enabled_action(action.action_id):
            self._manager().deliver_result(CallResult(
                call_id=action.call_id, agent_id=agent.agent_id,
                data={"status": "disabled", "info": "Action is currently disabled."},
            ))
            return
        agent_modules = agent.modules() if hasattr(agent, "modules") else set()
        if self.is_globally_paused("action", action.action_id) or any(
            self.is_globally_paused("module", module_id)
            and action.action_id in self.module_action_ids(module_id)
            for module_id in agent_modules
        ):
            self._manager().deliver_result(CallResult(
                call_id=action.call_id, agent_id=agent.agent_id,
                data={"status": "paused", "info": "Capability is globally paused."},
            ))
            return
        with self._lock:
            runtime = self._actions.get(action.action_id)
            core = False
            if runtime is None:
                if not spec.owner.startswith("core"):
                    raise RuntimeError(f"Действие {action.action_id} выключено")
                core = True
            elif not runtime.host.started:
                raise RuntimeError(f"Действие {action.action_id} выключено")
            manager = self._manager()
            if manager is None:
                raise RuntimeError("Менеджер агентов не запущен")
            manager.results.begin(
                agent_id=agent.agent_id,
                call_id=action.call_id,
                action_id=action.action_id,
                result_schema=spec.result_schema,
            )
        if core:
            threading.Thread(
                target=self._run_core_action,
                args=(spec, action, agent),
                name=f"jarvis-core-{action.action_id}",
                daemon=True,
            ).start()
            return
        with self._lock:
            host = runtime.host
            message = {
                "kind": "action",
                "agent_id": agent.agent_id,
                "action_id": action.action_id,
                "call_id": action.call_id,
                "data": dict(action.data),
                "metadata": {"preset": agent.preset, "agent_name": agent.name},
            }
        try:
            self._send_host(host, message)
        except Exception:
            manager.results.discard(agent.agent_id, action.call_id)
            raise

    def _run_core_action(
        self, spec: ActionDefinition, action: ActionRequest, agent: Any
    ) -> None:
        """Исполнить захардкоженное действие ядра в собственном потоке."""

        manager = self._manager()
        context = ActionContext(
            agent_id=agent.agent_id,
            call_id=action.call_id,
            action_id=action.action_id,
            capability_id=action.action_id,
            module_id=None,
            complete=lambda data, parts=(): self._complete_action(
                agent.agent_id, action.call_id, data, parts
            ),
            config=self.config,
            agent_manager=manager,
            capabilities=self,
            services=self.services,
            metadata={"preset": agent.preset, "agent_name": agent.name},
        )
        try:
            result = spec.run(dict(action.data), context)
        except Exception as exc:  # noqa: BLE001
            manager.fail_call(agent.agent_id, action.call_id, exc)
            return
        if result is PENDING:
            return
        self._complete_action(agent.agent_id, action.call_id, result)

    def _manager(self) -> Any:
        return getattr(self.event_bus, "manager", None)

    def _complete_action(
        self,
        agent_id: str | None,
        call_id: str | None,
        data: Any,
        parts: Any = (),
    ) -> bool:
        manager = self._manager()
        if manager is None or agent_id is None or call_id is None:
            self.debug.log(
                "call_result_dropped",
                agent_id=agent_id,
                call_id=call_id,
                reason="no_manager",
            )
            return False
        if not isinstance(data, dict):
            self.debug.log(
                "call_result_rejected",
                agent_id=agent_id,
                call_id=call_id,
                reason="invalid_data",
            )
            manager.fail_call(agent_id, call_id, "Некорректная структура результата")
            return False
        return manager.results.complete(
            manager,
            agent_id=agent_id,
            call_id=call_id,
            data=data,
            parts=tuple(parts),
        )

    def _discard_pending(self, agent_id: str | None, call_id: str | None) -> None:
        manager = self._manager()
        if manager is None or agent_id is None or call_id is None:
            return
        manager.results.discard(agent_id, call_id)

    def _report_error(self, capability: str, exc: Exception | str) -> None:
        manager = self._manager()
        if manager is not None:
            manager.report_capability_error(capability, exc)
        else:
            self.debug.log("capability_error", capability=capability, error=str(exc))

    # --- выгрузка -----------------------------------------------------
    def unload_action(self, action_id: str) -> None:
        with self._lock:
            runtime = self._actions.pop(action_id, None)
        if runtime is None or runtime.module_id is not None:
            return
        self._stop_host(runtime.host)

    def unload_handler(self, handler_id: str) -> None:
        with self._lock:
            runtime = self._handlers.pop(handler_id, None)
        if runtime is None or runtime.module_id is not None:
            return
        self._stop_host(runtime.host)

    def unload_module(self, module_id: str) -> None:
        with self._lock:
            runtime = self._modules.pop(module_id, None)
            if runtime is not None:
                for action_id in runtime.action_ids:
                    self._actions.pop(action_id, None)
                for handler_id in runtime.handler_ids:
                    self._handlers.pop(handler_id, None)
        if runtime is None:
            return
        self._stop_host(runtime.host)

    def is_globally_paused(self, kind: str, capability_id: str) -> bool:
        key = {"module": "modules", "action": "actions", "handler": "handlers"}.get(kind)
        if key is None:
            raise ValueError(f"Неизвестный вид capability: {kind!r}")
        with self._lock:
            return capability_id in self._global_paused[key]

    def global_paused(self) -> dict[str, set[str]]:
        with self._lock:
            return {key: set(value) for key, value in self._global_paused.items()}

    def runnable_snapshot(self, snapshot: dict[str, set[str]]) -> dict[str, set[str]]:
        paused = self.global_paused()
        return {
            "modules": set(snapshot.get("modules", ())) - paused["modules"],
            "actions": set(snapshot.get("actions", ())) - paused["actions"],
            "handlers": set(snapshot.get("handlers", ())) - paused["handlers"],
        }

    def _save_global_state(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self._global_state_path.with_suffix(".tmp")
        state = {"paused": {key: sorted(value) for key, value in self._global_paused.items()}}
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self._global_state_path)

    def toggle_global_state(self, kind: str, capability_id: str) -> dict[str, Any]:
        with self._global_toggle_lock:
            return self._toggle_global_state_locked(kind, capability_id)

    def _toggle_global_state_locked(self, kind: str, capability_id: str) -> dict[str, Any]:
        kind_to_key = {"module": "modules", "action": "actions", "handler": "handlers"}
        key = kind_to_key.get(kind)
        if key is None:
            raise ValueError(f"Неизвестный вид capability: {kind!r}")
        self.validate(kind=kind, capability_id=capability_id)
        manager = self._manager()
        if manager is None:
            raise RuntimeError("Менеджер агентов не запущен")
        affected = manager.agents_assigned_and_enabled(kind, capability_id)
        agent_ids = [agent.agent_id for agent in affected]
        if not self.is_globally_paused(kind, capability_id):
            with self._lock:
                self._global_paused[key].add(capability_id)
                self._save_global_state()
            if kind == "module":
                action_ids = self.module_action_ids(capability_id)
            elif kind == "action":
                action_ids = {capability_id}
            else:
                action_ids = set()
            if action_ids:
                for agent in manager.agents_snapshot():
                    for pending in manager.results.for_capability(action_ids, {agent.agent_id}):
                        manager.pause_call(agent.agent_id, pending.call_id)
            if kind == "module":
                self.unload_module(capability_id)
            elif kind == "action":
                self.unload_action(capability_id)
            else:
                self.unload_handler(capability_id)
            return {"state": "paused", "affected_agent_ids": agent_ids}

        active_snapshots = [agent.capabilities_snapshot() for agent in affected]
        combined = {
            key_name: set().union(*(snapshot[key_name] for snapshot in active_snapshots))
            if active_snapshots else set()
            for key_name in ("modules", "actions", "handlers")
        }
        with self._lock:
            self._global_paused[key].discard(capability_id)
            self._save_global_state()
        try:
            runnable = self.runnable_snapshot(combined)
            self.load_snapshot(runnable, start_handlers=True)
        except Exception:
            with self._lock:
                self._global_paused[key].add(capability_id)
                self._save_global_state()
            raise
        return {"state": "running", "affected_agent_ids": agent_ids}

    def signal(
        self, kind: str, unit_id: str, name: str, data: dict[str, Any] | None = None
    ) -> bool:
        """Передать именованный сигнал запущенной единице.

        Единица объявляет обработчик ``on_signal(name, data)`` в своём
        entrypoint. Если единица не запущена, сигнал игнорируется.
        """

        with self._lock:
            host = self._hosts.get(f"{kind}:{unit_id}")
        if host is None or not host.started:
            return False
        self._send_host(
            host,
            {
                "kind": "signal",
                "name": name,
                "data": dict(data or {}),
            },
        )
        return True

    def create_environment(self, kind: str, unit_id: str) -> dict[str, Any]:
        path = self._unit_path(kind, unit_id)
        if not path.is_dir():
            raise ValueError(f"Единица не найдена: {kind} {unit_id!r}")
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
            "kind": kind,
            "id": unit_id,
            "environment": str(environment),
            "requirements": requirements_name,
        }

    def shutdown(self) -> None:
        self._closing.set()
        for action_id in list(self.loaded_actions()):
            runtime = self._actions.get(action_id)
            if runtime is not None and runtime.module_id is None:
                self.unload_action(action_id)
        for handler_id in list(self.loaded_handlers()):
            runtime = self._handlers.get(handler_id)
            if runtime is not None and runtime.module_id is None:
                self.unload_handler(handler_id)
        for module_id in list(self.loaded_modules()):
            self.unload_module(module_id)

    # --- сводки и каталог --------------------------------------------
    def catalog(self, snapshot: dict[str, set[str]], *, describe_unloaded: bool = False) -> dict[str, Any]:
        with self._lock:
            actions = []
            for action_id in sorted(snapshot.get("actions", ())):
                runtime = self._actions.get(action_id)
                if runtime is None and describe_unloaded:
                    item = self._describe("action", action_id)["actions"][0]
                    actions.append(self._action_summary(self._proxy_action(item, f"action:{action_id}", self.action_path(action_id)), self.action_path(action_id)))
                    continue
                if runtime is None or runtime.module_id is not None:
                    continue
                actions.append(self._action_summary(runtime.definition, runtime.path))
            handlers = []
            events = []
            for handler_id in sorted(snapshot.get("handlers", ())):
                runtime = self._handlers.get(handler_id)
                if runtime is None and describe_unloaded:
                    item = self._describe("handler", handler_id)["handlers"][0]
                    definition = self._proxy_handler(item, f"handler:{handler_id}", self.handler_path(handler_id))
                    handlers.append(self._handler_summary(definition, self.handler_path(handler_id)))
                    event = definition.event
                    events.append({"type": event.type, "description": event.description, "data_schema": event.data_schema})
                    continue
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
                    if not describe_unloaded:
                        continue
                    catalog = self._describe("module", module_id)
                    modules.append(self._module_summary(LoadedModule(
                        module_id=module_id,
                        definition=ModuleDefinition(catalog["description"], (), ()),
                        path=self.module_path(module_id),
                        host=UnitHost(f"module:{module_id}", "module", module_id, module_id, self.module_path(module_id), catalog),
                    )))
                    continue
                modules.append(self._module_summary(runtime))
            return {
                "actions": actions,
                "handlers": handlers,
                "events": events,
                "modules": modules,
            }

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
        catalog = runtime.host.catalog
        return {
            "module_id": runtime.module_id,
            "description": runtime.definition.description,
            "path": str(runtime.path),
            "actions": [
                {
                    "id": item["id"],
                    "type": item["id"],
                    "description": item["description"],
                    "args_schema": item["args_schema"],
                    "result_schema": item["result_schema"],
                    "path": str(
                        runtime.path / "actions" / item["id"].split(".", 1)[1]
                    ),
                }
                for item in catalog["actions"]
            ],
            "handlers": [
                {"id": item["id"], "description": item["description"]}
                for item in catalog["handlers"]
            ],
            "events": [
                {
                    "type": item["event"]["type"],
                    "description": item["event"]["description"],
                    "data_schema": item["event"]["data_schema"],
                }
                for item in catalog["handlers"]
            ],
        }


class _AgentApi:
    """JSON-совместимый фасад над AgentManager для RPC из единиц."""

    def __init__(self, capabilities: CapabilityManager):
        self._capabilities = capabilities

    @property
    def _manager(self):
        manager = self._capabilities._manager()
        if manager is None:
            raise RuntimeError("Менеджер агентов не запущен")
        return manager

    def spawn(self, parent_id: str, name: str, preset: str) -> dict[str, Any]:
        return self._manager.spawn(parent_id=parent_id, name=name, preset=preset)

    def delete(self, agent_id: str) -> dict[str, Any]:
        return self._manager.delete(agent_id=agent_id)

    def interrupt(
        self, agent_id: str, requester_id: str | None = None
    ) -> dict[str, Any]:
        return self._manager.interrupt(
            agent_id=agent_id, requester_id=requester_id
        )

    def list_agents(self) -> list[dict[str, Any]]:
        return self._manager.list_agents()

    def agent_snapshot(self, agent_id: str) -> dict[str, list[str]]:
        snapshot = self._manager.require_agent(agent_id).capabilities_snapshot()
        return {key: sorted(value) for key, value in snapshot.items()}

    def known_snapshot(self, agent_id: str) -> dict[str, list[str]]:
        snapshot = self._manager.require_agent(agent_id).known_snapshot()
        return {key: sorted(value) for key, value in snapshot.items()}

    def toggle_capability(self, kind: str, capability_id: str) -> dict[str, Any]:
        return self._capabilities.toggle_global_state(kind, capability_id)

    def deliver_message(
        self,
        sender_id: str,
        target_id: str,
        text: str,
        action_id: str,
        module_id: str | None,
    ) -> dict[str, Any]:
        manager = self._manager
        sender = manager.require_agent(sender_id)
        target = manager.require_agent(target_id)
        delivered = manager.bus.publish(
            Event(
                type="message_from_agent",
                data={
                    "from_agent_id": sender.agent_id,
                    "from_name": sender.name,
                    "text": text,
                },
                source=f"action:{action_id}",
                target=target.agent_id,
                module_id=module_id,
            )
        )
        return {"delivered": delivered, "agent_id": target.agent_id}

    def enable(self, agent_id: str, kind: str, capability_id: str) -> dict[str, Any]:
        manager = self._manager
        if kind == "module":
            manager.enable_module(agent_id, capability_id)
        elif kind == "action":
            manager.enable_action(agent_id, capability_id)
        elif kind == "handler":
            manager.enable_handler(agent_id, capability_id)
        else:
            raise ValueError(f"Неизвестный вид capability: {kind!r}")
        persistent = False
        if agent_id == "main":
            manager.presets.add_capability("main", kind, capability_id)
            manager.presets.set_disabled("main", kind, capability_id, False)
            persistent = True
        return {
            "enabled": True,
            "kind": kind,
            "id": capability_id,
            "persistent": persistent,
        }

    def disable(self, agent_id: str, kind: str, capability_id: str) -> dict[str, Any]:
        manager = self._manager
        if kind == "module":
            manager.disable_module(agent_id, capability_id)
        elif kind == "action":
            manager.disable_action(agent_id, capability_id)
        elif kind == "handler":
            manager.disable_handler(agent_id, capability_id)
        else:
            raise ValueError(f"Неизвестный вид capability: {kind!r}")
        if agent_id == "main":
            manager.presets.set_disabled("main", kind, capability_id, True)
        return {"enabled": False, "kind": kind, "id": capability_id}

    def presets_list(self) -> list[dict[str, Any]]:
        return [
            {
                "name": preset.name,
                "modules": list(preset.modules),
                "actions": list(preset.actions),
                "handlers": list(preset.handlers),
                "protected": preset.protected,
            }
            for preset in self._manager.presets.list()
        ]

    def preset_add_capability(
        self, kind: str, capability_id: str
    ) -> dict[str, Any]:
        self._manager.presets.add_capability("main", kind, capability_id)
        return {"kind": kind, "id": capability_id, "preset": "main"}
