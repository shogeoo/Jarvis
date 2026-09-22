from jarvis.capabilities import action_definition
from jarvis.core.protocol import object_schema


def run(data, context):
    manager = context.agent_manager
    created = manager.spawn(
        parent_id=context.agent_id,
        name=data["name"],
        preset=data["preset"],
    )
    return {
        "spawned": True,
        "agent_id": created["agent_id"],
        "name": created["name"],
        "preset": created["preset"],
    }


def create_action():
    return action_definition(
        "Создать независимый живой экземпляр с указанным читаемым name "
        "из существующего preset. Задачу передавай после получения результата "
        "отдельным сообщением через действие send_message_to_agent.",
        object_schema(
            {
                "name": {"type": "string"},
                "preset": {"type": "string"},
            }
        ),
        object_schema(
            {
                "spawned": {"type": "boolean"},
                "agent_id": {"type": "string"},
                "name": {"type": "string"},
                "preset": {"type": "string"},
            }
        ),
        run,
    )
