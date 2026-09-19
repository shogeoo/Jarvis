from jarvis.core.protocol import object_schema
from jarvis.modules import ActionQueue, Module, action, event, event_handler

from .actions.manage import submit
from .handlers.manage import run, stop


STRING = {"type": "string"}
NULLABLE_STRING = {"type": ["string", "null"]}
ANY_JSON = {"type": ["object", "array", "string", "number", "boolean", "null"]}


def schema(**properties):
    return object_schema(properties)


def create_module():
    tasks = ActionQueue()
    result = event(
        "module_manager.operation_result",
        "Адресный результат фоновой операции module_manager. action_type указывает исходную операцию; result содержит данные при success, error — причину при error.",
        schema(
            action_type=STRING,
            status={"type": "string", "enum": ["success", "error"]},
            result=ANY_JSON,
            error=NULLABLE_STRING,
        ),
    )
    actions = (
        action("module_manager.workspace_list", "Рекурсивно перечислить исходники по path относительно .jarvis/modules; .venv и __pycache__ исключаются.", schema(path=STRING), submit(tasks)),
        action("module_manager.workspace_read", "Прочитать UTF-8 файл по path относительно .jarvis/modules. Содержимое module-local .venv недоступно.", schema(path=STRING), submit(tasks)),
        action("module_manager.workspace_write", "Создать или полностью перезаписать UTF-8 файл по path внутри .jarvis/modules, автоматически создав родительские каталоги.", schema(path=STRING, content=STRING), submit(tasks)),
        action("module_manager.workspace_delete", "Удалить файл либо каталог внутри .jarvis/modules; recursive разрешает рекурсивное удаление каталога, корень modules защищён.", schema(path=STRING, recursive={"type": "boolean"}), submit(tasks)),
        action(
            "module_manager.process_run",
            "Запустить проверочную shell-команду с cwd относительно .jarvis/modules и вернуть stdout, stderr, returncode и timed_out. Используй для реальных тестов.",
            schema(command=STRING, cwd=NULLABLE_STRING, timeout_seconds={"type": "integer"}),
            submit(tasks),
        ),
        action("module_manager.validate", "Импортировать выключенный in_process-модуль либо запросить описание isolated-worker и проверить manifest, factory, префиксы и строгие schemas без включения агентам.", schema(module_id=STRING), submit(tasks)),
        action("module_manager.disable", "Перед редактированием удалить module_id из RAM всех живых экземпляров и полностью выгрузить registry, queues, handlers, workers и импортированный код; файлы остаются.", schema(module_id=STRING), submit(tasks)),
        action("module_manager.enable", "После тестов повторно проверить код; для отредактированного модуля загрузить его и вернуть прежним живым экземплярам, для нового только подтвердить готовность к назначению.", schema(module_id=STRING), submit(tasks)),
        action("module_manager.prepare_environment", "Создать .venv внутри module_id и установить его requirements.txt. Обычные зависимости обязаны иметь точные версии через ==.", schema(module_id=STRING), submit(tasks)),
        action("module_manager.add_to_agent", "Загрузить готовый module_id и включить его только указанному живому agent_id, не изменяя preset или другие экземпляры.", schema(agent_id=STRING, module_id=STRING), submit(tasks)),
    )
    handler = event_handler(
        "module_manager.control",
        "Выполняет операции с кодом модулей и возвращает адресные результаты.",
        (result,),
        lambda ctx: run(tasks, ctx),
        stop=lambda ctx: stop(tasks, ctx),
    )
    return Module(
        module_id="module_manager",
        description="Редактирование, тестирование, выключение и динамическое включение модулей Jarvis.",
        actions=actions,
        handlers=(handler,),
    )
