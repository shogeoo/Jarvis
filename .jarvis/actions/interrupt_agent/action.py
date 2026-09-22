from jarvis.capabilities import action_definition
from jarvis.core.protocol import object_schema


def run(data, context):
    return context.agent_manager.interrupt(
        agent_id=data["agent_id"], reason=data["reason"]
    )


def create_action():
    return action_definition(
        "Остановить цикл указанного живого экземпляра. reason сохраняется "
        "только в результате операции.",
        object_schema(
            {
                "agent_id": {"type": "string"},
                "reason": {"type": "string"},
            }
        ),
        object_schema(
            {
                "agent_id": {"type": "string"},
                "state": {"type": "string"},
                "reason": {"type": "string"},
            }
        ),
        run,
    )
