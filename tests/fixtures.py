"""Временные фикстуры capabilities для тестов ядра."""

from __future__ import annotations

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

from .actions.repeat.action import create_action as repeat
from .handlers.monitor.handler import create_handler as monitor


def create_module():
    return module_definition(
        "Тестовый контейнер.",
        (repeat(),),
        (monitor(),),
    )
"""

CONTROL_ACTIONS = {
    "delete_agent": '''
from jarvis.capabilities import action_definition
from jarvis.core.protocol import object_schema
def run(data, context):
    return context.agent_manager.delete(agent_id=data["agent_id"])
def create_action():
    return action_definition("Delete agent", object_schema({"agent_id": {"type": "string"}}),
        object_schema({"agent_id": {"type": "string"}, "deleted": {"type": "boolean"}}), run)
''',
    "interrupt_agent": '''
from jarvis.capabilities import action_definition
from jarvis.core.protocol import object_schema
def run(data, context):
    return context.agent_manager.interrupt(agent_id=data["agent_id"], requester_id=context.agent_id)
def create_action():
    return action_definition("Interrupt agent", object_schema({"agent_id": {"type": "string"}}),
        object_schema({"agent_id": {"type": "string"}, "state": {"type": "string"}}), run)
''',
}


def toggle_capability_action() -> str:
    return '''
from jarvis.capabilities import action_definition
from jarvis.core.protocol import object_schema
def run(data, context):
    result = context.agent_manager.toggle_capability(kind=data["kind"], capability_id=data["id"])
    return {"kind": data["kind"], "id": data["id"], **result}
def create_action():
    return action_definition("Toggle global capability state",
        object_schema({"kind": {"type": "string", "enum": ["module", "action", "handler"]}, "id": {"type": "string"}}),
        object_schema({"kind": {"type": "string"}, "id": {"type": "string"}, "state": {"type": "string", "enum": ["paused", "running"]}, "affected_agent_ids": {"type": "array", "items": {"type": "string"}}}), run)
'''


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


def write_master_prompt(project_root: Path) -> Path:
    path = Path(project_root) / "master_prompt.txt"
    path.write_text("environment\n", encoding="utf-8")
    return path


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
    (main / "modules.json").write_text("[]\n", encoding="utf-8")
    (main / "actions.json").write_text('["say"]\n', encoding="utf-8")
    (main / "handlers.json").write_text('["tick"]\n', encoding="utf-8")
    worker = root / "presets" / "worker"
    worker.mkdir(parents=True, exist_ok=True)
    (worker / "personprompt.txt").write_text("worker\n", encoding="utf-8")
    (worker / "modules.json").write_text("[]\n", encoding="utf-8")
    (worker / "actions.json").write_text('["say"]\n', encoding="utf-8")
    (worker / "handlers.json").write_text('["tick"]\n', encoding="utf-8")
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
