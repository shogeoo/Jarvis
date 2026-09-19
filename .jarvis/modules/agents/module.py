from jarvis.core.protocol import object_schema
from jarvis.modules import Module, action, event

from .actions.control import (
    delete_agent,
    interrupt_agent,
    list_agents,
    list_presets,
    create_preset,
    delete_preset,
    send_message,
    spawn_agent,
)


STRING = {"type": "string"}
NULLABLE_STRING = {"type": ["string", "null"]}
ANY_JSON = {"type": ["object", "array", "string", "number", "boolean", "null"]}


def schema(**properties):
    return object_schema(properties)


def create_module():
    operation_result = event(
        "agents.operation_result",
        "Результат операции управления агентами или пресетами.",
        schema(
            action_id=STRING,
            action_type=STRING,
            status={"type": "string", "enum": ["success", "error"]},
            result=ANY_JSON,
            error=NULLABLE_STRING,
        ),
    )
    message_event = event(
        "agents.message",
        "Обычное адресное сообщение от другого агента.",
        schema(from_agent_id=STRING, from_name=STRING, text=STRING),
    )
    actions = (
        action(
            "agents.spawn",
            "Создать самостоятельный экземпляр агента с указанными name и preset.",
            schema(name=STRING, preset=STRING),
            spawn_agent,
        ),
        action(
            "agents.message",
            "Отправить обычное событие message агенту по agent_id.",
            schema(agent_id=STRING, text=STRING),
            send_message,
        ),
        action(
            "agents.interrupt",
            "Остановить работающий экземпляр агента.",
            schema(agent_id=STRING, reason=STRING),
            interrupt_agent,
        ),
        action(
            "agents.delete",
            "Удалить остановленный или работающий экземпляр агента.",
            schema(agent_id=STRING, reason=STRING),
            delete_agent,
        ),
        action(
            "agents.list",
            "Получить список активных экземпляров агентов.",
            object_schema({}),
            list_agents,
        ),
        action(
            "agents.preset_create",
            "Создать или заменить пресет, кроме защищённого preset main.",
            schema(
                name=STRING,
                person_prompt=STRING,
                modules={"type": "array", "items": STRING},
            ),
            create_preset,
        ),
        action(
            "agents.preset_delete",
            "Удалить пресет, кроме защищённого preset main.",
            schema(name=STRING),
            delete_preset,
        ),
        action(
            "agents.preset_list",
            "Получить список файловых пресетов.",
            object_schema({}),
            list_presets,
        ),
    )
    return Module(
        module_id="agents",
        description="Создание агентов, межагентные сообщения и управление пресетами",
        actions=actions,
        events=(operation_result, message_event),
    )
