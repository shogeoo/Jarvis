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
    "create_automation": '''
from pathlib import Path

from jarvis.capabilities import action_definition
from jarvis.core.protocol import object_schema
from jarvis.infrastructure.automations import AutomationStore


def _open_object():
    return {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": True,
        "x-jarvis-open-object": True,
    }


def run(data, context):
    if context.agent_id != "main":
        raise ValueError("Only main may create automations.")
    automation = AutomationStore.validate(data)
    AutomationStore(Path(context.config.jarvis_dir) / "automations.json").append(
        automation
    )
    return {"status": "created"}


def create_action():
    open_object = _open_object()
    trigger_event = {
        "anyOf": [
            object_schema(
                {"handler_id": {"type": "string"}, "data": open_object}
            ),
            object_schema({"type": {"type": "string"}, "data": open_object}),
        ]
    }
    trigger_call_result = object_schema(
        {
            "type": {"type": "string", "enum": ["call_result"]},
            "call_id": {"type": "string"},
            "data": open_object,
        }
    )
    automation_action = object_schema(
        {"action_id": {"type": "string"}, "data": open_object}
    )
    actions = {"type": "array", "minItems": 1, "items": automation_action}
    return action_definition(
        "Create an exact-match automation for one model-facing event or call_result. "
        "Actions contain action_id and data; Jarvis assigns unique call_id values. "
        "Only main can create automations.",
        {
            "anyOf": [
                object_schema({"event": trigger_event, "actions": actions}),
                object_schema(
                    {"call_result": trigger_call_result, "actions": actions}
                ),
            ]
        },
        object_schema({"status": {"type": "string", "enum": ["created"]}}),
        run,
    )
''',
    "create_preset": '''
from pathlib import Path

from jarvis.capabilities import action_definition
from jarvis.core.protocol import object_schema
from jarvis.presets import PresetStore


def run(data, context):
    preset_id = data["preset_id"]
    if context.agent_id != "main":
        return {
            "status": "not_created",
            "preset_id": preset_id,
            "error": "Only main may create presets.",
        }

    capabilities = {
        "actions": data["actions"],
        "handlers": data["handlers"],
        "modules": data["modules"],
    }
    try:
        for kind, ids in capabilities.items():
            singular = {"actions": "action", "handlers": "handler", "modules": "module"}[kind]
            for capability_id in ids:
                context.capabilities.validate(
                    kind=singular, capability_id=capability_id
                )
        PresetStore(Path(context.config.jarvis_dir) / "presets").create(
            preset_id, data["person_prompt"], capabilities
        )
    except Exception as exc:  # noqa: BLE001
        return {"status": "not_created", "preset_id": preset_id, "error": str(exc)}
    return {"status": "created", "preset_id": preset_id, "error": None}


def create_action():
    return action_definition(
        "Create a new agent preset from person_prompt and existing on-disk "
        "capability IDs. Each actions, handlers, and modules array may be empty. "
        "Existing presets cannot be replaced. Only main may create presets.",
        object_schema(
            {
                "preset_id": {"type": "string"},
                "person_prompt": {"type": "string"},
                "actions": {"type": "array", "items": {"type": "string"}},
                "handlers": {"type": "array", "items": {"type": "string"}},
                "modules": {"type": "array", "items": {"type": "string"}},
            }
        ),
        object_schema(
            {
                "status": {"type": "string", "enum": ["created", "not_created"]},
                "preset_id": {"type": "string"},
                "error": {"type": ["string", "null"]},
            }
        ),
        run,
    )
''',
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
