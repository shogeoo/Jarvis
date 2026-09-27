"""Временные фикстуры capabilities для тестов ядра."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


SAY_ACTION = """
from jarvis.capabilities import action_definition
from jarvis.core.protocol import object_schema


def run(data, context):
    return {"spoken": True, "text": data["text"]}


def create_action():
    return action_definition(
        "Speak text and return confirmation.",
        object_schema({"text": {"type": "string"}}),
        object_schema({"spoken": {"type": "boolean"}, "text": {"type": "string"}}),
        run,
    )
"""

TICK_HANDLER = """
from jarvis.capabilities import event_definition, handler_definition
from jarvis.core.protocol import object_schema


TICK = event_definition(
    "tick.event",
    "An external test event.",
    object_schema({"text": {"type": "string"}}),
)


def start(context):
    context.stop_event.wait()


def create_handler():
    return handler_definition(
        "A test observer.",
        TICK,
        start,
    )
"""

ECHO_ACTION = """
from jarvis.capabilities import action_definition
from jarvis.core.protocol import object_schema


def run(data, context):
    return {"value": data["value"]}


def create_action():
    return action_definition(
        "Return a value.",
        object_schema({"value": {"type": "string"}}),
        object_schema({"value": {"type": "string"}}),
        run,
    )
"""

ECHO_HANDLER = """
from jarvis.capabilities import event_definition, handler_definition
from jarvis.core.protocol import object_schema


ECHOED = event_definition(
    "echo.echoed",
    "A module test event.",
    object_schema({"value": {"type": "string"}}),
)


def start(context):
    context.stop_event.wait()


def create_handler():
    return handler_definition(
        "A module test observer.",
        ECHOED,
        start,
    )
"""

ECHO_MODULE = """
from jarvis.capabilities import module_definition

from .actions.repeat.action import create_action as repeat
from .handlers.monitor.handler import create_handler as monitor


def create_module():
    return module_definition(
        "A test integration.",
        (repeat(),),
        (monitor(),),
    )
"""

def link_environment(unit_dir: Path, python: str | None = None) -> Path:
    """Создать .venv/bin/python единицы симлинком на текущий интерпретатор."""

    bin_dir = Path(unit_dir) / ".venv" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    target = bin_dir / "python"
    if not target.exists():
        os.symlink(python or sys.executable, target)
    return target


def write_action(
    actions_dir: Path, action_id: str, code: str, python: str | None = None
) -> Path:
    unit = Path(actions_dir) / action_id
    unit.mkdir(parents=True, exist_ok=True)
    (unit / "action.py").write_text(code, encoding="utf-8")
    link_environment(unit, python)
    return unit


def write_handler(
    handlers_dir: Path, handler_id: str, code: str, python: str | None = None
) -> Path:
    unit = Path(handlers_dir) / handler_id
    unit.mkdir(parents=True, exist_ok=True)
    (unit / "handler.py").write_text(code, encoding="utf-8")
    link_environment(unit, python)
    return unit


def write_jarvis_root(root: Path, python: str | None = None) -> Path:
    root = Path(root)
    actions = root / "actions"
    handlers = root / "handlers"
    write_action(actions, "say", SAY_ACTION, python)
    write_handler(handlers, "tick", TICK_HANDLER, python)
    main = root / "presets" / "main"
    main.mkdir(parents=True, exist_ok=True)
    (main / "personprompt.txt").write_text("main\n", encoding="utf-8")
    (main / "preset.json").write_text('{"protected": true}\n', encoding="utf-8")
    (main / "capabilities.json").write_text(
        json.dumps({"modules": [], "actions": ["say"], "handlers": ["tick"]}, indent=2) + "\n",
        encoding="utf-8",
    )
    worker = root / "presets" / "worker"
    worker.mkdir(parents=True, exist_ok=True)
    (worker / "personprompt.txt").write_text("worker\n", encoding="utf-8")
    (worker / "capabilities.json").write_text(
        json.dumps({"modules": [], "actions": ["say"], "handlers": ["tick"]}, indent=2) + "\n",
        encoding="utf-8",
    )
    return root


def write_echo_module(root: Path, python: str | None = None) -> Path:
    module = Path(root) / "modules" / "echo"
    actions = module / "actions"
    handlers = module / "handlers"
    actions.mkdir(parents=True, exist_ok=True)
    handlers.mkdir(parents=True, exist_ok=True)
    (module / "module.py").write_text(ECHO_MODULE, encoding="utf-8")
    (actions / "repeat").mkdir(exist_ok=True)
    (actions / "repeat" / "action.py").write_text(ECHO_ACTION, encoding="utf-8")
    (handlers / "monitor").mkdir(exist_ok=True)
    (handlers / "monitor" / "handler.py").write_text(ECHO_HANDLER, encoding="utf-8")
    link_environment(module, python)
    return module
