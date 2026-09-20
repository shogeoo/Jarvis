"""Изолированный процесс модуля-контейнера с JSON Lines IPC."""

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


def _send(value):
    with _write_lock:
        _protocol.write(json.dumps(value, ensure_ascii=False) + "\n")
        _protocol.flush()


def _forget_import(name: str) -> None:
    for key in tuple(sys.modules):
        if key == name or key.startswith(name + "."):
            sys.modules.pop(key, None)


def _units(directory: Path) -> list[Path]:
    if not directory.exists():
        return []
    return sorted(
        path
        for path in directory.iterdir()
        if path.is_file()
        and path.suffix == ".py"
        and not path.name.startswith(".")
        and path.name != "__init__.py"
        and _SIMPLE.fullmatch(path.stem)
    )


def _ensure_package(module_id: str, path: Path) -> str:
    package = f"jarvis_container_{module_id}_{uuid.uuid4().hex}"
    package_module = ModuleType(package)
    package_module.__path__ = [str(path.resolve())]
    sys.modules[package] = package_module
    return package


def _import_file(path: Path, package: str, subpackage: str | None, search_paths: list[str]):
    parent = f"{package}.{subpackage}" if subpackage else package
    if parent not in sys.modules:
        subpackage_module = ModuleType(parent)
        subpackage_module.__path__ = [str(path.parent.resolve())]
        sys.modules[parent] = subpackage_module
    import_name = (
        f"{parent}.{path.stem}_{uuid.uuid4().hex}"
    )
    # Без submodule_search_locations: относительные импорты разрешаются
    # по dotted-имени.
    spec = importlib.util.spec_from_file_location(import_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Не удалось импортировать {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[import_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        _forget_import(import_name)
        raise
    return module


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
        raise ValueError("ID захардкожен: назначается из имени файла")
    return replace(definition, id=unit_id)


def _load(path: Path, module_id: str):
    package = _ensure_package(module_id, path)
    try:
        module = _import_file(path / "module.py", package, None, [])
        factory = getattr(module, "create_module", None)
        if not callable(factory):
            raise ValueError(f"В {path / 'module.py'} нет create_module()")
        module_definition = factory()
        if not isinstance(module_definition, ModuleDefinition):
            raise TypeError("create_module должен вернуть ModuleDefinition")
        if module_definition.execution not in {"in_process", "isolated"}:
            raise ValueError(f"Неизвестный execution модуля {module_id}")
        by_source: dict[str, tuple[str, Any]] = {}
        for item in module_definition.actions:
            if not isinstance(item, ActionDefinition):
                raise TypeError("Модуль вернул не действие")
            key = str(Path(item.source).resolve())
            if key in by_source:
                raise ValueError(f"Один файл — одно действие: {item.source}")
            by_source[key] = ("action", item)
        for item in module_definition.handlers:
            if not isinstance(item, HandlerDefinition):
                raise TypeError("Модуль вернул не handler")
            key = str(Path(item.source).resolve())
            if key in by_source:
                raise ValueError(f"Один файл — одна единица: {item.source}")
            by_source[key] = ("handler", item)
        actions: dict[str, ActionDefinition] = {}
        for unit in _units(path / "actions"):
            entry = by_source.get(str(unit.resolve()))
            if entry is None or entry[0] != "action":
                raise ValueError(
                    f"Файл {unit.name} не подключён в module.py модуля {module_id}"
                )
            _check_action(entry[1])
            full_id = f"{module_id}.{unit.stem}"
            actions[full_id] = _assign(entry[1], full_id)
        handlers: list[HandlerDefinition] = []
        for unit in _units(path / "handlers"):
            entry = by_source.get(str(unit.resolve()))
            if entry is None or entry[0] != "handler":
                raise ValueError(
                    f"Файл {unit.name} не подключён в module.py модуля {module_id}"
                )
            _check_handler(entry[1])
            full_id = f"{module_id}.{unit.stem}"
            handlers.append(_assign(entry[1], full_id))
        if not actions and not handlers:
            raise ValueError(f"Модуль {module_id} не содержит действий или handlers")
        return module_definition, actions, handlers
    except Exception:
        _forget_import(package)
        raise


def _catalog(module_id: str, module_definition, actions, handlers):
    return {
        "module_id": module_id,
        "description": module_definition.description,
        "actions": [
            {
                "id": spec.id,
                "description": spec.description,
                "args_schema": spec.args_schema,
                "result_schema": spec.result_schema,
            }
            for spec in actions.values()
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
            for handler in handlers
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--jarvis-dir", required=True)
    parser.add_argument("--module-id", required=True)
    parser.add_argument("--describe", action="store_true")
    args = parser.parse_args(argv)
    root = Path(args.jarvis_dir)
    module_id = args.module_id
    path = root / "modules" / module_id
    sys.stdout = sys.stderr
    module_definition, actions, handlers = _load(path, module_id)
    catalog = _catalog(module_id, module_definition, actions, handlers)
    if args.describe:
        _send({"kind": "description", "catalog": catalog})
        return 0

    stop = threading.Event()
    jobs = queue.Queue()

    def emit(handler_id):
        def _emit(event):
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

    config = SimpleNamespace(project_root=Path.cwd(), jarvis_dir=root)
    if module_definition.prepare is not None:
        module_definition.prepare(config)
    teardown = module_definition.teardown

    def complete(agent_id, action_id, data, parts=()):
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

    def run_action_jobs():
        while not stop.is_set():
            item = jobs.get()
            if item is None:
                return
            spec, data, context = item
            try:
                result = spec.run(data, context)
            except Exception as exc:
                _send(
                    {
                        "kind": "module_error",
                        "agent_id": context.agent_id,
                        "action_id": context.action_id,
                        "error": str(exc),
                    }
                )
                continue
            if result is PENDING:
                continue
            complete(context.agent_id, context.action_id, result)

    action_thread = threading.Thread(target=run_action_jobs, daemon=True)
    action_thread.start()

    _send({"kind": "ready", "catalog": catalog})
    handler_threads = []
    contexts = []
    for handler in handlers:
        context = HandlerContext(
            handler_id=handler.id,
            unit_path=path,
            emit_event=emit(handler.id),
            module_id=module_id,
            config=config,
            services={},
            stop_event=stop,
        )
        contexts.append((context, handler))

        def run_handler(selected=handler, selected_context=context):
            try:
                selected.start(selected_context)
            except Exception as exc:
                if not stop.is_set():
                    _send({"kind": "module_error", "error": str(exc)})

        thread = threading.Thread(target=run_handler, daemon=True)
        thread.start()
        handler_threads.append(thread)

    try:
        for line in sys.stdin:
            message = json.loads(line)
            if message.get("kind") == "shutdown":
                break
            if message.get("kind") != "action":
                continue
            action_type = message["type"]
            spec = actions[action_type]
            context = ActionContext(
                agent_id=message["agent_id"],
                action_id=message["action_id"],
                action_type=action_type,
                capability_id=action_type,
                module_id=module_id,
                complete=lambda data, parts=(), agent_id=message["agent_id"], action_id=message[
                    "action_id"
                ]: complete(agent_id, action_id, data, parts),
                config=config,
                agent_manager=None,
                capabilities=None,
                services={},
                stop_event=stop,
            )
            jobs.put((spec, message["data"], context))
    finally:
        stop.set()
        jobs.put(None)
        for context, handler in contexts:
            if handler.stop is not None:
                try:
                    handler.stop(context)
                except Exception:
                    pass
        if teardown is not None:
            try:
                teardown(config)
            except Exception:
                pass
        action_thread.join(timeout=2)
        for thread in handler_threads:
            thread.join(timeout=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
