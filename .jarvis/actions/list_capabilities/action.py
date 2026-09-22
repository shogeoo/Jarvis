from jarvis.capabilities import action_definition
from jarvis.core.protocol import object_schema


def run(data, context):
    snapshot = context.agent_manager.agent_snapshot(context.agent_id)
    existing = context.capabilities.list_existing()
    return {
        "modules": [
            {**item, "active_for_agent": item["module_id"] in snapshot["modules"]}
            for item in existing["modules"]
        ],
        "actions": [
            {**item, "active_for_agent": item["action_id"] in snapshot["actions"]}
            for item in existing["actions"]
        ],
        "handlers": [
            {**item, "active_for_agent": item["handler_id"] in snapshot["handlers"]}
            for item in existing["handlers"]
        ],
    }


def _entry(name):
    return {
        "type": "object",
        "properties": {
            name: {"type": "string"},
            "description": {"type": "string"},
            "loaded": {"type": "boolean"},
            "active_for_agent": {"type": "boolean"},
        },
        "required": [name, "description", "loaded", "active_for_agent"],
        "additionalProperties": False,
    }


def create_action():
    return action_definition(
        "Показать все capability на диске: модули целиком, отдельные действия "
        "и handlers, их описание, глобальное состояние загрузки и доступность "
        "текущему экземпляру.",
        object_schema({}),
        object_schema(
            {
                "modules": {"type": "array", "items": _entry("module_id")},
                "actions": {"type": "array", "items": _entry("action_id")},
                "handlers": {"type": "array", "items": _entry("handler_id")},
            }
        ),
        run,
    )
