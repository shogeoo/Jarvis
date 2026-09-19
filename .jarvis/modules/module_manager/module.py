from jarvis.core.protocol import object_schema
from jarvis.modules import Module, action, event

from .actions.manage import (
    add_to_preset,
    complete_module,
    delete_path,
    list_workspace,
    read_file,
    run_process,
    validate_module,
    write_file,
)


STRING = {"type": "string"}
NULLABLE_STRING = {"type": ["string", "null"]}
ANY_JSON = {"type": ["object", "array", "string", "number", "boolean", "null"]}


def schema(**properties):
    return object_schema(properties)


def create_module():
    result = event(
        "module_manager.operation_result",
        "Результат операции создания, проверки или назначения модуля.",
        schema(
            action_id=STRING,
            action_type=STRING,
            status={"type": "string", "enum": ["success", "error"]},
            result=ANY_JSON,
            error=NULLABLE_STRING,
        ),
    )
    actions = (
        action(
            "module_manager.workspace_list",
            "Перечислить файлы внутри .jarvis/modules.",
            schema(path=STRING),
            list_workspace,
        ),
        action(
            "module_manager.workspace_read",
            "Прочитать UTF-8 файл внутри .jarvis/modules.",
            schema(path=STRING),
            read_file,
        ),
        action(
            "module_manager.workspace_write",
            "Создать или полностью перезаписать файл внутри .jarvis/modules.",
            schema(path=STRING, content=STRING),
            write_file,
        ),
        action(
            "module_manager.workspace_delete",
            "Удалить файл или каталог внутри .jarvis/modules.",
            schema(path=STRING, recursive={"type": "boolean"}),
            delete_path,
        ),
        action(
            "module_manager.process_run",
            "Запустить проверочную shell-команду из каталога модулей.",
            schema(command=STRING, cwd=NULLABLE_STRING, timeout_seconds={"type": "integer"}),
            run_process,
        ),
        action(
            "module_manager.validate",
            "Проверить manifest, импорты, handlers и JSON Schema модуля.",
            schema(module_id=STRING),
            validate_module,
        ),
        action(
            "module_manager.complete",
            "Применить уже проверенный модуль после успешных рабочих тестов.",
            schema(
                module_id=STRING,
                operation={"type": "string", "enum": ["create", "update", "delete"]},
                summary=STRING,
            ),
            complete_module,
        ),
        action(
            "module_manager.add_to_preset",
            "Добавить module_id в modules.json указанного пресета. Удаление не поддерживается.",
            schema(preset=STRING, module_id=STRING),
            add_to_preset,
        ),
    )
    return Module(
        module_id="module_manager",
        description="Создание, проверка, применение и назначение модулей",
        actions=actions,
        events=(result,),
    )
