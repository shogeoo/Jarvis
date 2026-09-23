from jarvis.capabilities import action_definition
from jarvis.core.protocol import object_schema


def run(data, context):
    return context.agent_manager.delete(agent_id=data["agent_id"])


def create_action():
    return action_definition(
        "Удалить живой экземпляр из runtime и освободить его capabilities. "
        "Файлы preset не удаляются.",
        object_schema({"agent_id": {"type": "string"}}),
        object_schema(
            {
                "agent_id": {"type": "string"},
                "deleted": {"type": "boolean"},
            }
        ),
        run,
    )
