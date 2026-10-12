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
import select
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from ..core.lifecycle import terminate_process
from ..core.protocol import (
    Event,
    InputPart,
)
from ..core.registry import ActionRegistry, EventRegistry
from ..infrastructure.config import DEFAULT_JARVIS_DIR
from ..infrastructure.debug import Debugger
from ..infrastructure.storage_migration import migrate_capability_directories
from .agent_api import _AgentApi
from .api import (
    ActionDefinition,
    EventDefinition,
    HandlerDefinition,
    ModuleDefinition,
)
from .hosts import LoadedAction, LoadedHandler, LoadedModule, UnitHost

DEFAULT_ROOT = DEFAULT_JARVIS_DIR


from .catalog import CapabilityCatalog
from .discovery import CapabilityDiscovery
from .dispatch import CapabilityDispatch
from .rpc import CapabilityRpc
from .state import CapabilityState


class CapabilityManager(
    CapabilityDiscovery, CapabilityCatalog, CapabilityState, CapabilityRpc, CapabilityDispatch
):
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
        migrate_capability_directories(self.root)
        self.storage_root = self.root / "capabilities"
        self.actions_dir = self.storage_root / "actions"
        self.handlers_dir = self.storage_root / "handlers"
        self.modules_dir = self.storage_root / "modules"
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
        self._global_state_error = None
        try:
            raw_state = json.loads(self._global_state_path.read_text(encoding="utf-8"))
            if not isinstance(raw_state, dict) or not isinstance(
                raw_state.get("paused"), dict
            ):
                raise ValueError("Invalid global capability state")
            for key in ("modules", "actions", "handlers"):
                if not isinstance(raw_state["paused"].get(key), list) or not all(
                    isinstance(item, str) for item in raw_state["paused"][key]
                ):
                    raise ValueError("Invalid global capability state: " + key)
        except FileNotFoundError:
            raw_state = {}
        except Exception as exc:
            self._global_state_error = str(exc)
            self.debug.error(
                "Cannot read capability_state.json; file preserved: " + str(exc)
            )
            raw_state = {}
        old_paused = raw_state.get("paused", {})
        self._global_paused = {
            kind: set(old_paused.get(kind, raw_state.get(kind, [])))
            for kind in ("modules", "actions", "handlers")
        }
        if self._global_state_error is None:
            self._save_global_state()
        self._agent_api = _AgentApi(self)
        self._closing = threading.Event()
        self._cancel_waiters: dict[tuple[str, str], threading.Event] = {}

    # --- процесс единицы ---------------------------------------------
    def _python(self, path: Path) -> Path:
        """Dependencies and custom environments belong to capability code."""
        return Path(sys.executable)

    def _worker_command(self, kind: str, unit_id: str) -> list[str]:
        path = self._unit_path(kind, unit_id)
        package = Path(__file__).resolve().parents[1]
        # Load only the SDK package, not the core environment's site-packages.
        bootstrap = (
            "import importlib.util,runpy,sys;"
            f"spec=importlib.util.spec_from_file_location('jarvis',{str(package / '__init__.py')!r},submodule_search_locations=[{str(package)!r}]);"
            "module=importlib.util.module_from_spec(spec);sys.modules['jarvis']=module;"
            "spec.loader.exec_module(module);"
            "runpy.run_module('jarvis.capabilities.worker',run_name='__main__',alter_sys=True)"
        )
        return [
            str(self._python(path)),
            "-c",
            bootstrap,
            "--jarvis-dir",
            str(self.root.resolve()),
            "--unit",
            self._rel(kind, unit_id),
        ]

    def _worker_env(self) -> dict[str, str]:
        return dict(os.environ)

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
        log_dir = self.root / "runtime" / "logs"
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
                    self._fail_call(
                        host,
                        message.get("agent_id"),
                        message.get("call_id"),
                        message.get("error", ""),
                    )
                elif kind == "call_result":
                    self._complete_action(
                        message.get("agent_id"),
                        message.get("call_id"),
                        message.get("data"),
                        tuple(
                            InputPart(
                                item["type"],
                                item["mime_type"],
                                item["data"],
                                item.get("name", ""),
                            )
                            for item in message.get("parts", [])
                        ),
                    )
                elif kind == "cancelled":
                    with self._lock:
                        waiter = self._cancel_waiters.get(
                            (message.get("agent_id"), message.get("call_id"))
                        )
                    if waiter is not None:
                        waiter.set()
                elif kind == "event":
                    raw = message["event"]
                    self.event_bus.publish(
                        Event(
                            event_id=raw["event_id"],
                            data=raw["data"],
                            source=host.key,
                            target=raw.get("target"),
                            reply_to=raw.get("reply_to"),
                            parts=tuple(
                                InputPart(
                                    item["type"],
                                    item["mime_type"],
                                    item["data"],
                                    item.get("name", ""),
                                )
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

    def _fail_call(
        self, host: UnitHost, agent_id: str | None, call_id: str | None, error: str
    ) -> None:
        manager = self._manager()
        if agent_id is not None and call_id is not None and manager is not None:
            manager.fail_call(agent_id, call_id, error)
        else:
            self._host_failed(host, error)

    def _host_failed(self, host: UnitHost, error: str) -> None:
        manager = self._manager()
        action_ids = set(host.action_ids) or (
            {host.unit_id} if host.kind == "action" else set()
        )
        if manager is not None:
            for agent in manager.agents_snapshot():
                for pending in manager.results.for_capability(
                    action_ids, {agent.agent_id}
                ):
                    manager.fail_call(agent.agent_id, pending.call_id, error)
        if host.kind == "module":
            self.unload_module(host.unit_id)
        elif host.kind == "action":
            self.unload_action(host.unit_id)
        else:
            self.unload_handler(host.unit_id)
        self.debug.log("capability_error", kind=host.kind, id=host.unit_id, error=error)

    def cancel_call(self, agent_id: str, call_id: str) -> None:
        manager = self._manager()
        if manager is None:
            return
        pending = next(
            (
                item
                for item in manager.results.for_agent(agent_id)
                if item.call_id == call_id
            ),
            None,
        )
        if pending is None:
            return
        self.cancel_execution(agent_id, call_id, pending.action_id)

    def cancel_execution(self, agent_id: str, call_id: str, action_id: str) -> None:
        manager = self._manager()
        if manager is not None:
            manager.processes.cancel_call(agent_id, call_id)
        with self._lock:
            runtime = self._actions.get(action_id)
        if runtime is not None and runtime.host.started:
            key = (agent_id, call_id)
            waiter = threading.Event()
            with self._lock:
                self._cancel_waiters[key] = waiter
            try:
                self._send_host(
                    runtime.host,
                    {"kind": "cancel_action", "agent_id": agent_id, "call_id": call_id},
                )
                if not waiter.wait(3):
                    self._host_failed(
                        runtime.host, "Не удалось остановить выполнение действия"
                    )
            finally:
                with self._lock:
                    self._cancel_waiters.pop(key, None)

    # --- загрузка -----------------------------------------------------
    def _proxy_action(
        self, item: dict[str, Any], owner: str, path: Path
    ) -> ActionDefinition:
        return ActionDefinition(
            id=item["id"],
            description=item["description"],
            data_schema=item["data_schema"],
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
                event["event_id"],
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

    def validate(self, capability_id: str) -> dict[str, Any]:
        kind = self.resolve_kind(capability_id)
        if capability_id in self.builtin_modules() or (
            self.actions.get(capability_id) is not None
            and self.actions.get(capability_id).owner.startswith("core")
        ):
            return self.capability_info(capability_id)
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

    def load_snapshot(
        self, snapshot: dict[str, set[str]], *, start_handlers: bool
    ) -> None:
        loaded: list[tuple[str, str]] = []
        try:
            for module_id in sorted(snapshot.get("modules", ())):
                if module_id not in self.loaded_modules():
                    self.load_module(module_id, start_handlers=False)
                    loaded.append(("module", module_id))
            for action_id in snapshot.get("actions", ()):
                if action_id not in self.loaded_actions():
                    self.load_action(action_id, start_handlers=False)
                    loaded.append(("action", action_id))
            for handler_id in snapshot.get("handlers", ()):
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

    def load_action(
        self, action_id: str, *, start_handlers: bool = True
    ) -> dict[str, Any]:
        if "." not in action_id and self.resolve_kind(action_id) != "action":
            raise ValueError("Expected standalone action capability")
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

    def load_handler(
        self, handler_id: str, *, start_handlers: bool = True
    ) -> dict[str, Any]:
        if "." not in handler_id and self.resolve_kind(handler_id) != "handler":
            raise ValueError("Expected standalone handler capability")
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

    def load_module(
        self, module_id: str, *, start_handlers: bool = True
    ) -> dict[str, Any]:
        if self.resolve_kind(module_id) != "module":
            raise ValueError("Expected module capability")
        if module_id in self.builtin_modules():
            return self.builtin_module_catalog(module_id)
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
            self._proxy_action(
                item, owner, path / "actions" / item["id"].split(".", 1)[1]
            )
            for item in catalog["actions"]
        ]
        handlers = [
            self._proxy_handler(
                item, owner, path / "handlers" / item["id"].split(".", 1)[1]
            )
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
        if module_id in self.builtin_modules():
            return
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
