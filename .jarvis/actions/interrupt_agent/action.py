from jarvis.capabilities import action_definition
from jarvis.core.protocol import object_schema


def run(data, context):
    return context.agent_manager.interrupt(
        agent_id=data["agent_id"], requester_id=context.agent_id
    )


def create_action():
    return action_definition(
        "Прервать текущую генерацию указанного агента, сохранив его контекст и возможность получать новые события.",
        object_schema({"agent_id": {"type": "string"}}),
        object_schema(
            {
                "agent_id": {"type": "string"},
                "state": {"type": "string"},
            }
        ),
        run,
    )
