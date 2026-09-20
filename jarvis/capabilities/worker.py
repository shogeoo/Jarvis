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
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

from .api import (
    PENDING,
    ActionContext,
    ActionDefinition,
    HandlerContext,
    HandlerDefinition,
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


def _manifest(path: Path) -> dict[str, Any]:
    manifest_path = path / "module.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    module_id = manifest.get("module_id")
    if not isinstance(module_id, str) or not _SIMPLE.fullmatch(module_id):
        raise ValueError(f"Некорректный module_id: {module_id!r}")
    if module_id != path.name:
        raise ValueError(
            f"module_id {module_id!r} не совпадает с каталогом {path.name!r}"
        )
    execution = manifest.get("execution", "in_process")
    if execution not in {"in_process", "isolated"}:
        raise ValueError(f"Неизвестный execution модуля {module_id}: {execution}")
    return manifest


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
    if not definition.id or not callable(definition.run):
        raise ValueError(f"Некорректное действие {definition.id!r}")
    for name, schema in (
        ("аргументов", definition.args_schema),
        ("результата", definition.result_schema),
    ):
        validate_strict_schema(schema, where=f"схема {name} {definition.id}")


def _check_handler(definition: HandlerDefinition) -> None:
    if not definition.id or not callable(definition.start):
        raise ValueError(f"Некорректный handler {definition.id!r}")
    for event in definition.events:
        validate_strict_schema(event.data_schema, where=f"схема события {event.type}")


def _load(path: Path, module_id: str, manifest: dict[str, Any]):
    package = _ensure_package(module_id, path)
    try:
        actions: dict[str, ActionDefinition] = {}
        handlers: list[HandlerDefinition] = []
        for unit in _units(path / "actions"):
            expected_id = f"{module_id}.{unit.stem}"
            module = _import_file(
                unit,
                package,
                "actions",
                [str(unit.parent.resolve()), str(path.resolve())],
            )
            factory = getattr(module, "create_action", None)
            if not callable(factory):
                raise ValueError(f"В файле {unit} нет create_action()")
            definition = factory()
            if not isinstance(definition, ActionDefinition):
                raise TypeError("create_action должен вернуть ActionDefinition")
            if definition.id != expected_id:
                raise ValueError(
                    f"ID действия {definition.id!r} не совпадает с файлом {unit.name!r}"
                )
            _check_action(definition)
            actions[definition.id] = definition
        for unit in _units(path / "handlers"):
            expected_id = f"{module_id}.{unit.stem}"
            module = _import_file(
                unit,
                package,
                "handlers",
                [str(unit.parent.resolve()), str(path.resolve())],
            )
            factory = getattr(module, "create_handler", None)
            if not callable(factory):
                raise ValueError(f"В файле {unit} нет create_handler()")
            definition = factory()
            if not isinstance(definition, HandlerDefinition):
                raise TypeError("create_handler должен вернуть HandlerDefinition")
            if definition.id != expected_id:
                raise ValueError(
                    f"ID handler {definition.id!r} не совпадает с файлом {unit.name!r}"
                )
            _check_handler(definition)
            handlers.append(definition)
        if not actions and not handlers:
            raise ValueError(f"Модуль {module_id} не содержит действий или handlers")
        return actions, handlers
    except Exception:
        _forget_import(package)
        raise


def _catalog(module_id: str, manifest: dict[str, Any], actions, handlers):
    return {
        "module_id": module_id,
        "description": manifest.get("description", ""),
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
                "events": [
                    {
                        "type": event.type,
                        "description": event.description,
                        "data_schema": event.data_schema,
                    }
                    for event in handler.events
                ],
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
    manifest = _manifest(path)
    actions, handlers = _load(path, module_id, manifest)
    catalog = _catalog(module_id, manifest, actions, handlers)
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
    teardown = None
    if manifest.get("prepare") is not None:
        source_name, function_name = manifest["prepare"].split(":", 1)
        package = _ensure_package(module_id, path)
        prepared = _import_file(
            path / source_name, package, None, [str(path.resolve())]
        )
        prepare = getattr(prepared, function_name, None)
        if not callable(prepare):
            raise ValueError(f"Функция {manifest['prepare']} не найдена")
        prepare(config)
    if manifest.get("teardown") is not None:
        source_name, function_name = manifest["teardown"].split(":", 1)
        package = _ensure_package(module_id, path)
        torn = _import_file(
            path / source_name, package, None, [str(path.resolve())]
        )
        teardown = getattr(torn, function_name, None)
        if not callable(teardown):
            raise ValueError(f"Функция {manifest['teardown']} не найдена")

    def complete(agent_id, action_id, data):
        _send(
            {
                "kind": "action_result",
                "agent_id": agent_id,
                "action_id": action_id,
                "data": data,
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
                complete=lambda data, agent_id=message["agent_id"], action_id=message[
                    "action_id"
                ]: complete(agent_id, action_id, data),
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
