from jarvis.capabilities import action_definition
from jarvis.core.protocol import object_schema


def run(data, context):
    known = context.agent_manager.known_snapshot(context.agent_id)
    return context.capabilities.list_available(known)


def create_action():
    entry = object_schema({
        "id": {"type": "string"},
        "description": {"type": "string"},
    })
    return action_definition(
        "Перечислить все capabilities системы, ещё не назначенные этому агенту. "
        "Уже известные, в том числе отключённые, не включаются в ответ.",
        object_schema({}),
        object_schema({
            "modules": {"type": "array", "items": entry},
            "actions": {"type": "array", "items": entry},
            "handlers": {"type": "array", "items": entry},
        }),
        run,
    )
