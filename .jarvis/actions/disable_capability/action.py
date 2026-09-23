from jarvis.capabilities import action_definition
from jarvis.core.protocol import object_schema


def run(data, context):
    kind, capability_id = data["kind"], data["id"]
    if kind == "module" and capability_id == context.module_id:
        raise ValueError("Исполняемая capability не может выключить сама себя")
    if kind == "action" and capability_id == context.action_id:
        raise ValueError("Исполняемое действие не может выключить само себя")
    return context.agent_manager.disable(
        agent_id=context.agent_id,
        kind=kind,
        capability_id=capability_id,
    )


def create_action():
    return action_definition(
        "Убрать capability только из RAM текущего экземпляра. Если "
        "пользователей больше нет, её runtime полностью выгружается.",
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
            }
        ),
        run,
    )
