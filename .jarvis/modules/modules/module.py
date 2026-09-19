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
        "modules.operation_result",
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
            "modules.list_existing",
            "Показать все module_id, существующие на диске, их description, глобальное состояние загрузки и доступность текущему экземпляру.",
            object_schema({}),
            submit(tasks),
        ),
        action(
            "modules.list_active",
            "Показать точный enabled_modules текущего живого экземпляра; preset и другие экземпляры не учитываются.",
            object_schema({}),
            submit(tasks),
        ),
        action(
            "modules.enable",
            "Загрузить существующий module_id при необходимости и включить его только текущему экземпляру. Preset на диске не изменяется.",
            object_schema({"module_id": STRING}),
            submit(tasks),
        ),
        action(
            "modules.disable",
            "Убрать module_id только из RAM текущего экземпляра. Если пользователей больше нет, runtime модуля полностью выгружается.",
            object_schema({"module_id": STRING}),
            submit(tasks),
        ),
    )
    handler = event_handler(
        "modules.control",
        "Выполняет изменения оперативного набора модулей экземпляра.",
        (result,),
        lambda ctx: run(tasks, ctx),
        stop=lambda ctx: stop(tasks, ctx),
    )
    return Module(
        module_id="modules",
        description="Каталог существующих модулей и управление модулями текущего экземпляра агента.",
        actions=actions,
        handlers=(handler,),
    )
