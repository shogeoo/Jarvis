"""Процесс-хост одной самодостаточной capability-единицы.

Единица — это каталог ``actions/<id>/``, ``handlers/<id>/`` или
``modules/<id>/`` со своим ``.venv``. Хост импортирует entrypoint единицы
(``action.py``, ``handler.py``, ``module.py``), публикует каталог и общается
с ядром по JSON Lines через stdin/stdout. Управляющие вызовы к живому
состоянию ядра идут отдельными RPC-сообщениями.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import queue
import re
import sys
import threading
import uuid
from dataclasses import replace
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

from .api import (
    PENDING,
    ActionContext,
    ActionDefinition,
    EventDefinition,
    HandlerContext,
    HandlerDefinition,
    ModuleDefinition,
)
from ..core.protocol import validate_strict_schema


_protocol = sys.stdout
_write_lock = threading.Lock()
_SIMPLE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
_UNIT_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]*$")
_ENTRYPOINTS = {"action": "action.py", "handler": "handler.py", "module": "module.py"}
RPC_TIMEOUT = 120.0


def _send(value: dict[str, Any]) -> None:
    with _write_lock:
        _protocol.write(json.dumps(value, ensure_ascii=False) + "\n")
        _protocol.flush()


def _forget_import(name: str) -> None:
    for key in tuple(sys.modules):
        if key == name or key.startswith(name + "."):
            sys.modules.pop(key, None)


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", name)


def _ensure_package(name: str, path: Path) -> ModuleType:
    existing = sys.modules.get(name)
    if isinstance(existing, ModuleType):
        return existing
    module = ModuleType(name)
    module.__path__ = [str(path.resolve())]
    module.__package__ = name
    sys.modules[name] = module
    return module


def _register_tree(dotted: str, directory: Path) -> None:
    _ensure_package(dotted, directory)
    for child in sorted(directory.iterdir()):
        if (
            child.is_dir()
            and not child.name.startswith((".", "__"))
            and child.name not in {"assets", "models"}
            and _UNIT_NAME.fullmatch(child.name)
        ):
            _register_tree(f"{dotted}.{_safe(child.name)}", child)


def _import_file(path: Path, dotted: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(dotted, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Не удалось импортировать {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[dotted] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        _forget_import(dotted)
        raise
    return module


def _unit_entries(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(
        child
        for child in directory.iterdir()
        if child.is_dir()
        and not child.name.startswith(".")
        and _SIMPLE.fullmatch(child.name)
    )


def _check_action(definition: ActionDefinition) -> None:
    if not callable(definition.run):
        raise ValueError("Некорректное действие: run должен быть функцией")
    for name, schema in (
        ("аргументов", definition.args_schema),
        ("результата", definition.result_schema),
    ):
        validate_strict_schema(schema, where=f"схема {name} действия")


def _check_handler(definition: HandlerDefinition) -> None:
    if not callable(definition.start):
        raise ValueError("Некорректный handler: start должен быть функцией")
    if definition.stop is not None and not callable(definition.stop):
        raise ValueError("Некорректный stop handler")
    event = definition.event
    if not isinstance(event, EventDefinition):
        raise ValueError("Handler обязан объявлять ровно одно событие")
    validate_strict_schema(event.data_schema, where=f"схема события {event.type}")


def _assign(definition, unit_id: str):
    if getattr(definition, "id", ""):
        raise ValueError("ID захардкожен: назначается из имени каталога")
    return replace(definition, id=unit_id)


def _signals_of(module: ModuleType) -> tuple[Any, ...]:
    handler = getattr(module, "on_signal", None)
    return (handler,) if callable(handler) else ()


class _Unit:
    """Загруженное описание одной единицы."""

    def __init__(
        self,
        *,
        kind: str,
        unit_id: str,
        module_id: str | None,
        path: Path,
        description: str,
        actions: dict[str, ActionDefinition],
        handlers: list[HandlerDefinition],
        prepare: tuple[Any, ...],
        teardown: tuple[Any, ...],
        signals: tuple[Any, ...] = (),
    ):
        self.kind = kind
        self.unit_id = unit_id
        self.module_id = module_id
        self.path = path
        self.description = description
        self.actions = actions
        self.handlers = handlers
        self.prepare = prepare
        self.teardown = teardown
        self.signals = signals

    def catalog(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "unit_id": self.unit_id,
            "module_id": self.module_id,
            "description": self.description,
            "actions": [
                {
                    "id": spec.id,
                    "description": spec.description,
                    "args_schema": spec.args_schema,
                    "result_schema": spec.result_schema,
                }
                for spec in self.actions.values()
            ],
            "handlers": [
                {
                    "id": handler.id,
                    "description": handler.description,
                    "event": {
                        "type": handler.event.type,
                        "description": handler.event.description,
                        "data_schema": handler.event.data_schema,
                    },
                }
                for handler in self.handlers
            ],
        }


def _load_leaf(root: Path, rel: Path, kind: str, unit_id: str) -> _Unit:
    path = (root / rel).resolve()
    entrypoint = path / _ENTRYPOINTS[kind]
    if not entrypoint.is_file():
        raise ValueError(f"В каталоге {path} нет обязательного {entrypoint.name}")
    dotted = f"jarvis_unit_{_safe(unit_id)}_{uuid.uuid4().hex}"
    _register_tree(dotted, path)
    module = _import_file(entrypoint, f"{dotted}.{_ENTRYPOINTS[kind][:-3]}")
    if kind == "action":
        factory = getattr(module, "create_action", None)
        if not callable(factory):
            raise ValueError(f"В {entrypoint} нет create_action()")
        definition = factory()
        if not isinstance(definition, ActionDefinition):
            raise TypeError("create_action должен вернуть ActionDefinition")
        assigned = _assign(definition, unit_id)
        _check_action(assigned)
        return _Unit(
            kind=kind,
            unit_id=unit_id,
            module_id=None,
            path=path,
            description=assigned.description,
            actions={unit_id: assigned},
            handlers=[],
            prepare=tuple(
                hook for hook in (definition.prepare,) if hook is not None
            ),
            teardown=tuple(
                hook for hook in (definition.teardown,) if hook is not None
            ),
            signals=_signals_of(module),
        )
    factory = getattr(module, "create_handler", None)
    if not callable(factory):
        raise ValueError(f"В {entrypoint} нет create_handler()")
    definition = factory()
    if not isinstance(definition, HandlerDefinition):
        raise TypeError("create_handler должен вернуть HandlerDefinition")
    assigned = _assign(definition, unit_id)
    _check_handler(assigned)
    return _Unit(
        kind=kind,
        unit_id=unit_id,
        module_id=None,
        path=path,
        description=assigned.description,
        actions={},
        handlers=[assigned],
        prepare=tuple(hook for hook in (definition.prepare,) if hook is not None),
        teardown=tuple(
            hook for hook in (definition.teardown,) if hook is not None
        ),
        signals=_signals_of(module),
    )


def _load_module(root: Path, rel: Path, module_id: str) -> _Unit:
    path = (root / rel).resolve()
    entrypoint = path / "module.py"
    if not entrypoint.is_file():
        raise ValueError(f"В модуле {path} нет обязательного module.py")
    dotted = f"jarvis_module_{_safe(module_id)}_{uuid.uuid4().hex}"
    _register_tree(dotted, path)
    module = _import_file(entrypoint, f"{dotted}.module")
    factory = getattr(module, "create_module", None)
    if not callable(factory):
        raise ValueError(f"В {entrypoint} нет create_module()")
    definition = factory()
    if not isinstance(definition, ModuleDefinition):
        raise TypeError("create_module должен вернуть ModuleDefinition")

    by_source: dict[str, tuple[str, Any]] = {}
    for item in definition.actions:
        if not isinstance(item, ActionDefinition):
            raise TypeError(f"Модуль {module_id} вернул не действие")
        if item.id:
            raise ValueError("ID действия захардкожен: назначается из каталога")
        if not item.source:
            raise ValueError("Действие без файла-источника")
        key = str(Path(item.source).resolve())
        if key in by_source:
            raise ValueError(f"Один каталог — одно действие: {item.source}")
        by_source[key] = ("action", item)
    for item in definition.handlers:
        if not isinstance(item, HandlerDefinition):
            raise TypeError(f"Модуль {module_id} вернул не handler")
        if item.id:
            raise ValueError("ID handler захардкожен: назначается из каталога")
        if not item.source:
            raise ValueError("Handler без файла-источника")
        key = str(Path(item.source).resolve())
        if key in by_source:
            raise ValueError(f"Один каталог — одна единица: {item.source}")
        by_source[key] = ("handler", item)

    actions: dict[str, ActionDefinition] = {}
    prepare: list[Any] = []
    teardown: list[Any] = []
    for unit_dir in _unit_entries(path / "actions"):
        source = str((unit_dir / "action.py").resolve())
        entry = by_source.get(source)
        if entry is None or entry[0] != "action":
            raise ValueError(
                f"Каталог {unit_dir.name} не подключён в module.py модуля {module_id}"
            )
        definition_item = entry[1]
        full_id = f"{module_id}.{unit_dir.name}"
        actions[full_id] = _assign(definition_item, full_id)
        _check_action(actions[full_id])
        prepare.extend(hook for hook in (definition_item.prepare,) if hook)
        teardown.extend(hook for hook in (definition_item.teardown,) if hook)
    handlers: list[HandlerDefinition] = []
    for unit_dir in _unit_entries(path / "handlers"):
        source = str((unit_dir / "handler.py").resolve())
        entry = by_source.get(source)
        if entry is None or entry[0] != "handler":
            raise ValueError(
                f"Каталог {unit_dir.name} не подключён в module.py модуля {module_id}"
            )
        definition_item = entry[1]
        full_id = f"{module_id}.{unit_dir.name}"
        assigned = _assign(definition_item, full_id)
        _check_handler(assigned)
        handlers.append(assigned)
        prepare.extend(hook for hook in (definition_item.prepare,) if hook)
        teardown.extend(hook for hook in (definition_item.teardown,) if hook)
    if not actions and not handlers:
        raise ValueError(f"Модуль {module_id} не содержит действий или handlers")
    if definition.prepare is not None:
        prepare.insert(0, definition.prepare)
    if definition.teardown is not None:
        teardown.append(definition.teardown)
    return _Unit(
        kind="module",
        unit_id=module_id,
        module_id=module_id,
        path=path,
        description=definition.description,
        actions=actions,
        handlers=handlers,
        prepare=tuple(prepare),
        teardown=tuple(teardown),
        signals=_signals_of(module),
    )


def load_unit(root: Path, rel: Path) -> _Unit:
    parts = rel.parts
    if len(parts) == 2 and parts[0] in {"actions", "handlers"}:
        kind = "action" if parts[0] == "actions" else "handler"
        unit_id = parts[1]
        if not _SIMPLE.fullmatch(unit_id):
            raise ValueError(f"Некорректный id единицы: {unit_id!r}")
        return _load_leaf(root, rel, kind, unit_id)
    if len(parts) == 2 and parts[0] == "modules":
        module_id = parts[1]
        if not _SIMPLE.fullmatch(module_id):
            raise ValueError(f"Некорректный module_id: {module_id!r}")
        return _load_module(root, rel, module_id)
    raise ValueError(f"Неизвестная единица: {rel}")


class _RemoteError(RuntimeError):
    """Ошибка, поднятая живым состоянием ядра при RPC-вызове."""


class _Rpc:
    def __init__(self, send) -> None:
        self._send = send
        self._lock = threading.Lock()
        self._counter = 0
        self._pending: dict[int, dict[str, Any]] = {}

    def call(self, target: str, method: str, args, kwargs):
        with self._lock:
            self._counter += 1
            call_id = self._counter
            slot: dict[str, Any] = {"event": threading.Event()}
            self._pending[call_id] = slot
        self._send(
            {
                "kind": "rpc",
                "call_id": call_id,
                "target": target,
                "method": method,
                "args": list(args),
                "kwargs": dict(kwargs),
            }
        )
        if not slot["event"].wait(timeout=RPC_TIMEOUT):
            with self._lock:
                self._pending.pop(call_id, None)
            raise RuntimeError(f"RPC-таймаут: {target}.{method}")
        with self._lock:
            self._pending.pop(call_id, None)
        if not slot.get("ok"):
            raise _RemoteError(slot.get("error") or "неизвестная ошибка ядра")
        return slot.get("value")

    def resolve(self, message: dict[str, Any]) -> None:
        with self._lock:
            slot = self._pending.get(message.get("call_id"))
        if slot is None:
            return
        slot.update(
            ok=message.get("ok"),
            value=message.get("value"),
            error=message.get("error"),
        )
        slot["event"].set()


class Remote:
    """Ленивый JSON-RPC прокси к объектам живого ядра."""

    def __init__(self, rpc: _Rpc, target: str, path: str = "") -> None:
        object.__setattr__(self, "_rpc", rpc)
        object.__setattr__(self, "_target", target)
        object.__setattr__(self, "_path", path)

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        prefix = self._path + "." if self._path else ""
        return Remote(self._rpc, self._target, f"{prefix}{name}")

    def __call__(self, *args, **kwargs):
        if not self._path:
            raise TypeError("Прокси нельзя вызвать без метода")
        return self._rpc.call(self._target, self._path, args, kwargs)

    def __repr__(self) -> str:  # pragma: no cover - отладочный вид
        return f"<remote {self._target}.{self._path}>"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jarvis-dir", required=True)
    parser.add_argument("--unit", required=True)
    parser.add_argument("--describe", action="store_true")
    args = parser.parse_args(argv)
    root = Path(args.jarvis_dir).resolve()
    rel = Path(args.unit)
    sys.stdout = sys.stderr

    try:
        unit = load_unit(root, rel)
    except Exception as exc:  # noqa: BLE001
        if args.describe:
            _send({"kind": "unit_error", "error": str(exc)})
            return 1
        raise
    catalog = unit.catalog()
    if args.describe:
        _send({"kind": "description", "catalog": catalog})
        return 0

    config = SimpleNamespace(
        project_root=Path.cwd(), jarvis_dir=root, unit_dir=unit.path
    )
    rpc = _Rpc(_send)
    agent_manager = Remote(rpc, "agent_manager")
    capabilities = Remote(rpc, "capabilities")
    capabilities.root = root

    jobs: queue.Queue = queue.Queue()
    stop = threading.Event()
    services: dict[str, Any] = {}
    sent_results: set[tuple[str, str]] = set()

    def complete(agent_id: str, action_id: str, data, parts=()) -> None:
        sent_results.add((agent_id, action_id))
        _send(
            {
                "kind": "action_result",
                "agent_id": agent_id,
                "action_id": action_id,
                "data": data,
                "parts": [
                    {
                        "type": part.type,
                        "mime_type": part.mime_type,
                        "data": part.data,
                    }
                    for part in parts
                ],
            }
        )

    for hook in unit.prepare:
        hook(config)

    def run_actions() -> None:
        while not stop.is_set():
            item = jobs.get()
            if item is None:
                return
            spec, message = item
            agent_id = message["agent_id"]
            action_id = message["action_id"]
            context = ActionContext(
                agent_id=agent_id,
                action_id=action_id,
                action_type=message["type"],
                capability_id=message["type"],
                module_id=unit.module_id,
                complete=lambda data, parts=(), agent_id=agent_id, action_id=action_id: complete(
                    agent_id, action_id, data, parts
                ),
                config=config,
                agent_manager=agent_manager,
                capabilities=capabilities,
                services=services,
                metadata=dict(message.get("metadata") or {}),
                stop_event=stop,
            )
            try:
                result = spec.run(message["data"], context)
            except Exception as exc:  # noqa: BLE001
                _send(
                    {
                        "kind": "capability_error",
                        "agent_id": agent_id,
                        "action_id": action_id,
                        "error": str(exc),
                    }
                )
                continue
            if result is PENDING:
                continue
            complete(agent_id, action_id, result)

    action_thread = threading.Thread(target=run_actions, daemon=True)
    action_thread.start()

    _send({"kind": "ready", "catalog": catalog})

    def emit(handler_id: str):
        def _emit(event) -> None:
            _send(
                {
                    "kind": "event",
                    "event": {
                        "type": event.type,
                        "data": event.data,
                        "target": event.target,
                        "reply_to": event.reply_to,
                        "handler_id": handler_id,
                        "parts": [
                            {
                                "type": part.type,
                                "mime_type": part.mime_type,
                                "data": part.data,
                            }
                            for part in event.parts
                        ],
                    },
                }
            )

        return _emit

    contexts: list[tuple[HandlerContext, HandlerDefinition]] = []
    handler_threads: list[threading.Thread] = []
    for handler in unit.handlers:
        context = HandlerContext(
            handler_id=handler.id,
            unit_path=unit.path,
            emit_event=emit(handler.id),
            module_id=unit.module_id,
            agent_manager=agent_manager,
            capabilities=capabilities,
            config=config,
            services=services,
            stop_event=stop,
        )
        contexts.append((context, handler))

        def run_handler(selected=handler, selected_context=context) -> None:
            try:
                selected.start(selected_context)
            except Exception as exc:  # noqa: BLE001
                if not stop.is_set():
                    _send({"kind": "capability_error", "error": str(exc)})

        thread = threading.Thread(target=run_handler, daemon=True)
        thread.start()
        handler_threads.append(thread)

    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = message.get("kind")
            if kind == "shutdown":
                break
            if kind == "rpc_result":
                rpc.resolve(message)
                continue
            if kind == "signal":
                for callback in unit.signals:
                    try:
                        callback(message.get("name"), message.get("data") or {})
                    except Exception as exc:  # noqa: BLE001
                        sys.stderr.write(f"signal error: {exc}\n")
                continue
            if kind != "action":
                continue
            spec = unit.actions.get(message.get("type"))
            if spec is None:
                _send(
                    {
                        "kind": "capability_error",
                        "agent_id": message.get("agent_id"),
                        "action_id": message.get("action_id"),
                        "error": f"Неизвестное действие: {message.get('type')}",
                    }
                )
                continue
            jobs.put((spec, message))
    finally:
        stop.set()
        jobs.put(None)
        for context, handler in contexts:
            if handler.stop is not None:
                try:
                    handler.stop(context)
                except Exception:
                    pass
        for hook in reversed(unit.teardown):
            try:
                hook(config)
            except Exception:
                pass
        action_thread.join(timeout=2)
        for thread in handler_threads:
            thread.join(timeout=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
