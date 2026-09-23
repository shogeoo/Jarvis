from jarvis.capabilities import action_definition
from jarvis.core.protocol import object_schema


def run(data, context):
    return context.capabilities.list_existing()


def create_action():
    item = object_schema(
        {
            "id": {"type": "string"},
            "description": {"type": "string"},
            "loaded": {"type": "boolean"},
            "globally_enabled": {"type": "boolean"},
            "globally_disabled": {"type": "boolean"},
        }
    )
    return action_definition(
        "List every capability physically present in .jarvis and report its runtime and global state.",
        object_schema({}),
        object_schema(
            {
                "modules": {"type": "array", "items": item},
                "actions": {"type": "array", "items": item},
                "handlers": {"type": "array", "items": item},
            }
        ),
        run,
    )
