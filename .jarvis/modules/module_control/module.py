from jarvis.core.protocol import object_schema
from jarvis.modules import ActionQueue, Module, action, event, event_handler

from .actions.control import submit
from .handlers.control import run, stop


STRING = {"type": "string"}
NULLABLE_STRING = {"type": ["string", "null"]}
ANY_JSON = {"type": ["object", "array", "string", "number", "boolean", "null"]}


def create_module():
    tasks = ActionQueue()
    result = event(
        "module_control.operation_result",
        "Адресный результат просмотра, включения или выключения модуля.",
        object_schema(
            {
                "action_type": STRING,
                "status": {"type": "string", "enum": ["success", "error"]},
                "result": ANY_JSON,
                "error": NULLABLE_STRING,
            }
        ),
    )
    actions = (
        action(
            "module_control.list_existing",
            "Показать все module_id, существующие на диске, их description, глобальное состояние загрузки и доступность текущему экземпляру.",
            object_schema({}),
            submit(tasks),
        ),
        action(
            "module_control.list_active",
            "Показать точный enabled_modules текущего живого экземпляра; preset и другие экземпляры не учитываются.",
            object_schema({}),
            submit(tasks),
        ),
        action(
            "module_control.enable",
            "Загрузить существующий module_id при необходимости и включить его текущему экземпляру. Если action вызвал root-agent main, module_id также навсегда добавляется в main/modules.json.",
            object_schema({"module_id": STRING}),
            submit(tasks),
        ),
        action(
            "module_control.disable",
            "Убрать module_id только из RAM текущего экземпляра. Если пользователей больше нет, runtime модуля полностью выгружается.",
            object_schema({"module_id": STRING}),
            submit(tasks),
        ),
    )
    handler = event_handler(
        "module_control.control",
        "Выполняет изменения оперативного набора модулей экземпляра.",
        (result,),
        lambda ctx: run(tasks, ctx),
        stop=lambda ctx: stop(tasks, ctx),
    )
    return Module(
        module_id="module_control",
        description="Каталог существующих модулей и управление модулями текущего экземпляра агента.",
        actions=actions,
        handlers=(handler,),
    )
