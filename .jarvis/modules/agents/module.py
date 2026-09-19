from jarvis.core.protocol import object_schema
from jarvis.modules import ActionQueue, Module, action, event, event_handler

from .actions.control import submit
from .handlers.control import run, stop


STRING = {"type": "string"}
NULLABLE_STRING = {"type": ["string", "null"]}
ANY_JSON = {"type": ["object", "array", "string", "number", "boolean", "null"]}


def schema(**properties):
    return object_schema(properties)


def create_module():
    tasks = ActionQueue()
    operation_result = event(
        "agents.operation_result",
        "Результат фонового выполнения операции управления агентами.",
        schema(
            action_type=STRING,
            status={"type": "string", "enum": ["success", "error"]},
            result=ANY_JSON,
            error=NULLABLE_STRING,
        ),
    )
    message = event(
        "agents.message",
        "Адресное сообщение от другого агента.",
        schema(from_agent_id=STRING, from_name=STRING, text=STRING),
    )
    actions = (
        action("agents.spawn", "Создать независимый живой экземпляр с указанным читаемым name из существующего preset. Задачу передавай после события успеха отдельным agents.message.", schema(name=STRING, preset=STRING), submit(tasks)),
        action("agents.message", "Адресно передать text живому экземпляру по agent_id. Получатель увидит обычное событие agents.message в своей FIFO.", schema(agent_id=STRING, text=STRING), submit(tasks)),
        action("agents.interrupt", "Остановить цикл указанного живого экземпляра. reason сохраняется только в результате операции.", schema(agent_id=STRING, reason=STRING), submit(tasks)),
        action("agents.delete", "Удалить живой экземпляр из runtime и освободить его экземплярные модули. Файлы preset не удаляются.", schema(agent_id=STRING, reason=STRING), submit(tasks)),
        action("agents.list", "Получить agent_id, name, preset, parent_id, state и активные модули всех живых экземпляров.", object_schema({}), submit(tasks)),
        action("agents.preset_list", "Получить сохранённые presets, их стартовые modules и признак protected.", object_schema({}), submit(tasks)),
    )
    handler = event_handler(
        "agents.control",
        "Выполняет операции с агентами и возвращает адресные результаты.",
        (operation_result, message),
        lambda ctx: run(tasks, ctx),
        stop=lambda ctx: stop(tasks, ctx),
    )
    return Module(
        module_id="agents",
        description=(
            "Управление экземплярами агентов, межагентные сообщения и presets. "
            "Actions ставят задачи, handler возвращает результаты."
        ),
        actions=actions,
        handlers=(handler,),
    )
