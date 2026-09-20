"""Временные фикстуры capabilities для тестов ядра."""

from __future__ import annotations

import json
from pathlib import Path


SAY_ACTION = """
from jarvis.capabilities import action_definition
from jarvis.core.protocol import object_schema


def run(data, context):
    return {"spoken": True, "text": data["text"]}


def create_action():
    return action_definition(
        "say",
        "Произнести текст и вернуть подтверждение.",
        object_schema({"text": {"type": "string"}}),
        object_schema({"spoken": {"type": "boolean"}, "text": {"type": "string"}}),
        run,
    )
"""

TICK_HANDLER = """
from jarvis.capabilities import event_definition, handler_definition
from jarvis.core.protocol import object_schema


def start(context):
    context.stop_event.wait()


def create_handler():
    tick = event_definition(
        "tick.event",
        "Тестовое внешнее событие.",
        object_schema({"text": {"type": "string"}}),
    )
    return handler_definition(
        "tick",
        "Тестовый наблюдатель.",
        (tick,),
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
        "echo.repeat",
        "Вернуть значение.",
        object_schema({"value": {"type": "string"}}),
        object_schema({"value": {"type": "string"}}),
        run,
    )
"""

ECHO_HANDLER = """
from jarvis.capabilities import event_definition, handler_definition
from jarvis.core.protocol import object_schema


def start(context):
    context.stop_event.wait()


def create_handler():
    echoed = event_definition(
        "echo.echoed",
        "Тестовое событие модуля.",
        object_schema({"value": {"type": "string"}}),
    )
    return handler_definition(
        "echo.monitor",
        "Тестовый наблюдатель модуля.",
        (echoed,),
        start,
    )
"""


def write_jarvis_root(root: Path) -> Path:
    root = Path(root)
    actions = root / "actions"
    handlers = root / "handlers"
    actions.mkdir(parents=True, exist_ok=True)
    handlers.mkdir(parents=True, exist_ok=True)
    (actions / "say.py").write_text(SAY_ACTION, encoding="utf-8")
    (handlers / "tick.py").write_text(TICK_HANDLER, encoding="utf-8")
    (root / "environment.txt").write_text("environment\n", encoding="utf-8")
    main = root / "presets" / "main"
    main.mkdir(parents=True, exist_ok=True)
    (main / "personprompt.txt").write_text("main\n", encoding="utf-8")
    (main / "capabilities.json").write_text(
        json.dumps(
            {"modules": [], "actions": ["say"], "handlers": ["tick"]},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return root


def write_echo_module(root: Path) -> Path:
    module = Path(root) / "modules" / "echo"
    actions = module / "actions"
    handlers = module / "handlers"
    actions.mkdir(parents=True, exist_ok=True)
    handlers.mkdir(parents=True, exist_ok=True)
    (module / "module.json").write_text(
        json.dumps(
            {
                "module_id": "echo",
                "description": "Тестовый контейнер.",
                "execution": "in_process",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (actions / "repeat.py").write_text(ECHO_ACTION, encoding="utf-8")
    (handlers / "monitor.py").write_text(ECHO_HANDLER, encoding="utf-8")
    return module
