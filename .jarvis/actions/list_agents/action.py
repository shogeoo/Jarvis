from jarvis.capabilities import action_definition
from jarvis.core.protocol import object_schema


STRING = {"type": "string"}
NULLABLE_STRING = {"type": ["string", "null"]}
STRING_LIST = {"type": "array", "items": {"type": "string"}}


def run(data, context):
    return {"agents": context.agent_manager.list_agents()}


def create_action():
    return action_definition(
        "Получить agent_id, name, preset, parent_id, state и активные "
        "capabilities всех живых экземпляров.",
        object_schema({}),
        object_schema(
            {
                "agents": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "agent_id": STRING,
                            "name": STRING,
                            "parent_id": NULLABLE_STRING,
                            "state": STRING,
                            "preset": STRING,
                            "modules": STRING_LIST,
                            "actions": STRING_LIST,
                            "handlers": STRING_LIST,
                        },
                        "required": [
                            "agent_id",
                            "name",
                            "parent_id",
                            "state",
                            "preset",
                            "modules",
                            "actions",
                            "handlers",
                        ],
                        "additionalProperties": False,
                    },
                }
            }
        ),
        run,
    )
