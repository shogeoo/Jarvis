"""Изолированный процесс модуля с JSON Lines IPC."""

from __future__ import annotations

import argparse
import importlib.util
import json
import queue
import sys
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace

from .api import ActionContext, Module, ModuleContext


_protocol = sys.stdout
_write_lock = threading.Lock()


def _send(value):
    with _write_lock:
        _protocol.write(json.dumps(value, ensure_ascii=False) + "\n")
        _protocol.flush()


def _load(path: Path, entrypoint: str, factory_name: str) -> Module:
    source = path / entrypoint
    package = f"jarvis_isolated_{path.name}_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(
        package, source, submodule_search_locations=[str(path.resolve())]
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Не удалось импортировать {source}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[package] = module
    spec.loader.exec_module(module)
    factory = getattr(module, factory_name)
    definition = factory()
    if not isinstance(definition, Module):
        raise TypeError("factory должна вернуть Module")
    return definition


def _catalog(definition: Module):
    return {
        "module_id": definition.module_id,
        "description": definition.description,
        "actions": [
            {
                "type": spec.type,
                "description": spec.description,
                "data_schema": spec.data_schema,
            }
            for spec in definition.actions
        ],
        "handlers": [
            {
                "name": handler.name,
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
            for handler in definition.handlers
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--module", required=True)
    parser.add_argument("--entrypoint", required=True)
    parser.add_argument("--factory", required=True)
    parser.add_argument("--describe", action="store_true")
    args = parser.parse_args(argv)
    path = Path(args.module)
    sys.stdout = sys.stderr
    definition = _load(path, args.entrypoint, args.factory)
    catalog = _catalog(definition)
    if args.describe:
        _send({"kind": "description", "catalog": catalog})
        return 0

    stop = threading.Event()
    actions = {spec.type: spec for spec in definition.actions}
    jobs = queue.Queue()
    contexts = []

    def emit(event):
        _send(
            {
                "kind": "event",
                "event": {
                    "type": event.type,
                    "data": event.data,
                    "target": event.target,
                    "reply_to": event.reply_to,
                    "parts": [
                        {"type": part.type, "mime_type": part.mime_type, "data": part.data}
                        for part in event.parts
                    ],
                },
            }
        )

    config = SimpleNamespace(
        project_root=Path.cwd(),
        jarvis_dir=path.parents[1],
    )
    if definition.prepare is not None:
        definition.prepare(config)

    def run_action_jobs():
        while not stop.is_set():
            item = jobs.get()
            if item is None:
                return
            spec, data, context = item
            try:
                spec.handler(data, context)
            except Exception as exc:
                _send({"kind": "module_error", "error": str(exc)})

    action_thread = threading.Thread(target=run_action_jobs, daemon=True)
    action_thread.start()

    _send({"kind": "ready", "catalog": catalog})
    handler_threads = []
    for handler in definition.handlers:
        context = ModuleContext(
            module_id=definition.module_id,
            module_path=path,
            emit_event=emit,
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
                module_id=definition.module_id,
                config=config,
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
        action_thread.join(timeout=2)
        for thread in handler_threads:
            thread.join(timeout=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
