from jarvis.capabilities import action_definition
from jarvis.core.protocol import object_schema


def run(data, context):
    return context.agent_manager.enable(
        agent_id=context.agent_id,
        kind=data["kind"],
        capability_id=data["id"],
    )


def create_action():
    return action_definition(
        "Загрузить capability при необходимости и включить её текущему "
        "экземпляру: модуль целиком либо отдельное действие или handler. "
        "Если действие вызвал root-agent main, capability также навсегда "
        "добавляется в пресет main.",
        object_schema(
            {
                "kind": {
                    "type": "string",
                    "enum": ["module", "action", "handler"],
                },
                "id": {"type": "string"},
            }
        ),
        object_schema(
            {
                "enabled": {"type": "boolean"},
                "kind": {"type": "string"},
                "id": {"type": "string"},
                "persistent": {"type": "boolean"},
            }
        ),
        run,
    )
