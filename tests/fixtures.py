"""Временные фикстуры capabilities для тестов ядра."""

from __future__ import annotations

from pathlib import Path


SAY_ACTION = """
from jarvis.capabilities import action_definition
from jarvis.core.protocol import object_schema


def run(data, context):
    return {"spoken": True, "text": data["text"]}


def create_action():
    return action_definition(
        "Произнести текст и вернуть подтверждение.",
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
    "Тестовое внешнее событие.",
    object_schema({"text": {"type": "string"}}),
)


def start(context):
    context.stop_event.wait()


def create_handler():
    return handler_definition(
        "Тестовый наблюдатель.",
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
        "Вернуть значение.",
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
    "Тестовое событие модуля.",
    object_schema({"value": {"type": "string"}}),
)


def start(context):
    context.stop_event.wait()


def create_handler():
    return handler_definition(
        "Тестовый наблюдатель модуля.",
        ECHOED,
        start,
    )
"""

ECHO_MODULE = """
from jarvis.capabilities import module_definition

from .actions.repeat import create_action as repeat
from .handlers.monitor import create_handler as monitor


def create_module():
    return module_definition(
        "Тестовый контейнер.",
        (repeat(),),
        (monitor(),),
    )
"""


def write_master_prompt(project_root: Path) -> Path:
    path = Path(project_root) / "master_prompt.txt"
    path.write_text("environment\n{modalities}\n", encoding="utf-8")
    return path


def write_jarvis_root(root: Path) -> Path:
    root = Path(root)
    actions = root / "actions"
    handlers = root / "handlers"
    actions.mkdir(parents=True, exist_ok=True)
    handlers.mkdir(parents=True, exist_ok=True)
    (actions / "say.py").write_text(SAY_ACTION, encoding="utf-8")
    (handlers / "tick.py").write_text(TICK_HANDLER, encoding="utf-8")
    main = root / "presets" / "main"
    main.mkdir(parents=True, exist_ok=True)
    (main / "personprompt.txt").write_text("main\n", encoding="utf-8")
    (main / "modules.json").write_text("[]\n", encoding="utf-8")
    (main / "actions.json").write_text('["say"]\n', encoding="utf-8")
    (main / "handlers.json").write_text('["tick"]\n', encoding="utf-8")
    return root


def write_echo_module(root: Path) -> Path:
    module = Path(root) / "modules" / "echo"
    actions = module / "actions"
    handlers = module / "handlers"
    actions.mkdir(parents=True, exist_ok=True)
    handlers.mkdir(parents=True, exist_ok=True)
    (module / "module.py").write_text(ECHO_MODULE, encoding="utf-8")
    (actions / "repeat.py").write_text(ECHO_ACTION, encoding="utf-8")
    (handlers / "monitor.py").write_text(ECHO_HANDLER, encoding="utf-8")
    return module
