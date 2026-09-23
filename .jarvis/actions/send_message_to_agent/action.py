from jarvis.capabilities import action_definition
from jarvis.core.protocol import object_schema


def run(data, context):
    return context.agent_manager.deliver_message(
        sender_id=context.agent_id,
        target_id=data["agent_id"],
        text=data["text"],
        action_id=context.action_id,
        module_id=context.module_id,
    )


def create_action():
    return action_definition(
        "Адресно передать живому экземпляру по agent_id прямой текст text. "
        "Пиши в text саму задачу без служебного префикса и без слов "
        "«сообщение от агента». Получатель увидит обычное событие "
        "в своей FIFO.",
        object_schema(
            {
                "agent_id": {"type": "string"},
                "text": {"type": "string"},
            }
        ),
        object_schema(
            {
                "delivered": {"type": "boolean"},
                "agent_id": {"type": "string"},
            }
        ),
        run,
    )
