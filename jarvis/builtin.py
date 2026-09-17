"""Захардкоженные базовые события и действия системы."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .lifecycle import terminate_process
from .module_api import ActionSpec, EventDefinition
from .prompts import MODULE_BUILDER_INSTRUCTIONS
from .protocol import JSONSchema, empty_object_schema, object_schema
from .registry import ActionRegistry, EventRegistry
from .runtime import AgentManager, AgentPreset


STRING = {"type": "string"}
NULLABLE_STRING = {"type": ["string", "null"]}
INTEGER = {"type": "integer"}
BOOLEAN = {"type": "boolean"}
ANY_JSON = {
    "type": ["object", "array", "string", "number", "boolean", "null"]
}


def _schema(**properties: JSONSchema) -> JSONSchema:
    return object_schema(properties)


def builtin_event_definitions() -> list[EventDefinition]:
    return [
        EventDefinition(
            "speech",
            "Текст, распознанный из речи пользователя.",
            _schema(text=STRING),
        ),
        EventDefinition(
            "action_result",
            "Результат завершившегося действия.",
            _schema(
                action_id=STRING,
                action_type=STRING,
                status={"type": "string", "enum": ["success", "error"]},
                result=ANY_JSON,
                error=NULLABLE_STRING,
            ),
        ),
        EventDefinition(
            "message",
            "Обычное сообщение от одного агента другому.",
            object_schema({"from": STRING, "text": STRING}),
        ),
        EventDefinition(
            "handler_error",
            "Ошибка фонового обработчика пользовательского модуля.",
            _schema(module=STRING, handler=STRING, error=STRING),
        ),
    ]


def register_builtin_events(registry: EventRegistry) -> None:
    for definition in builtin_event_definitions():
        registry.register(definition)


def _workspace(context: Any) -> Path:
    workspace = context.metadata.get("workspace")
    if workspace:
        return Path(workspace)
    return context.module_manager.modules_dir


def _path_arg(context: Any, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else _workspace(context) / path


def register_builtin_actions(
    registry: ActionRegistry,
    manager: AgentManager,
    *,
    printer: Any = None,
    speaker: Any = None,
) -> None:
    manager.add_preset(
        AgentPreset(
            name="module_builder",
            description="Создаёт, изменяет и удаляет пользовательские модули.",
            purpose=MODULE_BUILDER_INSTRUCTIONS,
            allowed_actions=frozenset(
                {
                    "no_action",
                    "agent.message",
                    "workspace.list",
                    "workspace.read",
                    "workspace.write",
                    "workspace.delete",
                    "process.run",
                    "module.validate",
                    "module.complete",
                }
            ),
        ),
        persist=False,
    )
    actions = [
        ActionSpec(
            type="no_action",
            description="Закончить текущий цикл и ждать следующего события.",
            data_schema=empty_object_schema(),
            handler=lambda data, context: None,
            owner="builtin",
        ),
        ActionSpec(
            type="speech",
            description=(
                "Произнести прямую речь из text. Допустимы [теги] интонации. "
                "text не содержит Markdown, списков, двоеточий, табуляции, "
                "переносов строк или служебных пояснений."
            ),
            data_schema=_schema(text=STRING),
            handler=_speech(printer, speaker),
            owner="builtin",
        ),
        ActionSpec(
            type="agent.spawn",
            description="Создать агента с отдельными контекстом, очередью и потоком.",
            data_schema=_schema(
                agent_type=STRING,
                name=STRING,
                task=STRING,
                purpose=NULLABLE_STRING,
                actions={"type": "array", "items": STRING},
            ),
            handler=_spawn(manager),
            owner="builtin",
        ),
        ActionSpec(
            type="agent.message",
            description="Отправить обычное событие message любому агенту.",
            data_schema=_schema(agent_id=STRING, text=STRING),
            handler=_message(manager),
            owner="builtin",
        ),
        ActionSpec(
            type="agent.interrupt",
            description="Остановить агент, если это разрешено текущей конфигурацией.",
            data_schema=_schema(agent_id=STRING, reason=STRING),
            handler=_interrupt(manager),
            owner="builtin",
        ),
        ActionSpec(
            type="agent.delete",
            description="Остановить и удалить агент, если это разрешено конфигурацией.",
            data_schema=_schema(agent_id=STRING, reason=STRING),
            handler=_delete(manager),
            owner="builtin",
        ),
        ActionSpec(
            type="agent.list",
            description="Получить список экземпляров агентов и их состояний.",
            data_schema=empty_object_schema(),
            handler=lambda data, context: {"agents": manager.list_agents()},
            owner="builtin",
        ),
        ActionSpec(
            type="agent.preset_create",
            description="Сохранить переиспользуемый тип агента.",
            data_schema=_schema(
                name=STRING,
                description=STRING,
                purpose=STRING,
                actions={"type": "array", "items": STRING},
            ),
            handler=lambda data, context: context.agent_manager.create_preset(
                name=data["name"],
                description=data["description"],
                purpose=data["purpose"],
                allowed_actions=data["actions"],
            ),
            owner="builtin",
        ),
        ActionSpec(
            type="agent.preset_delete",
            description="Удалить сохранённый тип агента.",
            data_schema=_schema(name=STRING),
            handler=lambda data, context: context.agent_manager.delete_preset(data["name"]),
            owner="builtin",
        ),
        ActionSpec(
            type="agent.preset_list",
            description="Получить сохранённые типы агентов.",
            data_schema=empty_object_schema(),
            handler=lambda data, context: {"presets": context.agent_manager.list_presets()},
            owner="builtin",
        ),
        ActionSpec(
            type="module.create",
            description="Запустить метасубагента для создания модуля.",
            data_schema=_schema(module=STRING, request=STRING),
            handler=_module_request(manager, "create"),
            owner="builtin",
        ),
        ActionSpec(
            type="module.update",
            description="Запустить метасубагента для изменения модуля.",
            data_schema=_schema(module=STRING, request=STRING),
            handler=_module_request(manager, "update"),
            owner="builtin",
        ),
        ActionSpec(
            type="module.delete",
            description="Запустить метасубагента для удаления модуля.",
            data_schema=_schema(module=STRING, request=STRING),
            handler=_module_request(manager, "delete"),
            owner="builtin",
        ),
        ActionSpec(
            type="module.list",
            description="Получить список подключённых модулей.",
            data_schema=empty_object_schema(),
            handler=lambda data, context: {"modules": manager.module_manager.list_modules()},
            owner="builtin",
        ),
        ActionSpec(
            type="module.describe",
            description="Получить описание действий и событий модуля.",
            data_schema=_schema(module=STRING),
            handler=lambda data, context: manager.module_manager.describe(data["module"]),
            owner="builtin",
        ),
        ActionSpec(
            type="workspace.list",
            description="Получить список файлов из рабочей папки метасубагента.",
            data_schema=_schema(path=STRING),
            handler=_workspace_list,
            owner="builtin",
        ),
        ActionSpec(
            type="workspace.read",
            description="Прочитать файл из рабочей папки метасубагента.",
            data_schema=_schema(path=STRING),
            handler=_workspace_read,
            owner="builtin",
        ),
        ActionSpec(
            type="workspace.write",
            description="Создать или полностью записать файл.",
            data_schema=_schema(path=STRING, content=STRING),
            handler=_workspace_write,
            owner="builtin",
        ),
        ActionSpec(
            type="workspace.delete",
            description="Удалить файл или каталог из рабочей папки.",
            data_schema=_schema(path=STRING, recursive=BOOLEAN),
            handler=_workspace_delete,
            owner="builtin",
        ),
        ActionSpec(
            type="process.run",
            description="Запустить shell-команду и вернуть полный вывод.",
            data_schema=_schema(command=STRING, cwd=NULLABLE_STRING, timeout_seconds=INTEGER),
            handler=_process_run,
            owner="builtin",
        ),
        ActionSpec(
            type="module.validate",
            description="Проверить module.json, фабрику, импорты и схемы модуля.",
            data_schema=_schema(module=STRING),
            handler=lambda data, context: context.module_manager.validate(data["module"]),
            owner="builtin",
        ),
        ActionSpec(
            type="module.complete",
            description="Применить готовый модуль и сообщить родителю обычным сообщением.",
            data_schema=_schema(summary=STRING),
            handler=_module_complete(manager),
            owner="builtin",
        ),
    ]
    for spec in actions:
        registry.register(spec)


def _speech(printer: Any, speaker: Any):
    def handler(data: dict[str, Any], context: Any) -> dict[str, Any]:
        text = data["text"]
        if printer is not None:
            printer.print_reply(text)
        if speaker is None:
            return {"spoken": False, "reason": "tts_disabled"}
        speaker.submit(text).result()
        return {"spoken": True}

    return handler


def _spawn(manager: AgentManager):
    def handler(data: dict[str, Any], context: Any) -> dict[str, Any]:
        return manager.spawn(
            parent_id=context.agent_id,
            agent_type=data["agent_type"],
            name=data["name"],
            task=data["task"],
            purpose=data["purpose"],
            allowed_actions=data["actions"] or None,
            metadata={"agent_type": data["agent_type"]},
        )

    return handler


def _message(manager: AgentManager):
    def handler(data: dict[str, Any], context: Any) -> dict[str, Any]:
        return manager.send_message(
            sender_id=context.agent_id,
            agent_id=data["agent_id"],
            text=data["text"],
        )

    return handler


def _interrupt(manager: AgentManager):
    def handler(data: dict[str, Any], context: Any) -> dict[str, Any]:
        return manager.interrupt(
            requester_id=context.agent_id,
            agent_id=data["agent_id"],
            reason=data["reason"],
        )

    return handler


def _delete(manager: AgentManager):
    def handler(data: dict[str, Any], context: Any) -> dict[str, Any]:
        return manager.delete(
            requester_id=context.agent_id,
            agent_id=data["agent_id"],
            reason=data["reason"],
        )

    return handler


def _module_request(manager: AgentManager, operation: str):
    def handler(data: dict[str, Any], context: Any) -> dict[str, Any]:
        module_name = data["module"]
        if manager.module_manager is None:
            raise RuntimeError("Менеджер модулей не инициализирован")
        manager.module_manager.validate_name(module_name)
        modules_root = manager.module_manager.modules_dir
        modules_root.mkdir(parents=True, exist_ok=True)
        workspace = manager.module_manager.module_path(module_name)
        task = (
            f"Операция: {operation}. Модуль: {module_name}.\n"
            f"Целевая папка модуля: {workspace}.\n"
            f"Рабочая папка разработчика: {modules_root}.\n"
            f"Python окружения Jarvis: {sys.executable}.\n"
            f"Публичный контракт: {Path(__file__).with_name('module_api.py')}.\n"
            f"Требования родителя:\n{data['request']}\n"
            "Работай с любыми модулями внутри рабочей папки и сообщай вопросы родителю."
        )
        result = manager.spawn(
            parent_id=context.agent_id,
            agent_type="module_builder",
            name=f"module-builder-{module_name}",
            task=task,
            metadata={
                "module": module_name,
                "operation": operation,
                "workspace": str(modules_root),
            },
        )
        result.update({"module": module_name, "operation": operation})
        return result

    return handler


def _workspace_list(data: dict[str, Any], context: Any) -> dict[str, Any]:
    path = _path_arg(context, data["path"])
    if not path.exists():
        return {"path": str(path), "files": []}
    if not path.is_dir():
        raise ValueError(f"Не каталог: {path}")
    return {
        "path": str(path),
        "files": sorted(str(item.relative_to(path)) for item in path.rglob("*")),
    }


def _workspace_read(data: dict[str, Any], context: Any) -> dict[str, Any]:
    path = _path_arg(context, data["path"])
    return {"path": str(path), "content": path.read_text(encoding="utf-8")}


def _workspace_write(data: dict[str, Any], context: Any) -> dict[str, Any]:
    path = _path_arg(context, data["path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data["content"], encoding="utf-8")
    return {"path": str(path), "bytes": path.stat().st_size}


def _workspace_delete(data: dict[str, Any], context: Any) -> dict[str, Any]:
    path = _path_arg(context, data["path"])
    if path.is_dir():
        if not data["recursive"]:
            path.rmdir()
        else:
            shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)
    return {"path": str(path), "deleted": True}


def _process_run(data: dict[str, Any], context: Any) -> dict[str, Any]:
    cwd_value = data["cwd"]
    cwd = str(_path_arg(context, cwd_value)) if cwd_value else str(_workspace(context))
    proc = context.agent_manager.processes.start(
        data["command"],
        shell=True,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=data["timeout_seconds"])
        return {
            "returncode": proc.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "timed_out": False,
        }
    except subprocess.TimeoutExpired:
        terminate_process(proc, group=True)
        stdout, stderr = proc.communicate(timeout=2)
        return {
            "returncode": None,
            "stdout": stdout,
            "stderr": stderr,
            "timed_out": True,
        }
    finally:
        context.agent_manager.processes.finish(proc)


def _module_complete(manager: AgentManager):
    def handler(data: dict[str, Any], context: Any) -> dict[str, Any]:
        module_manager = context.module_manager
        if module_manager is None:
            raise RuntimeError("Менеджер модулей не инициализирован")
        module_name = context.metadata.get("module")
        operation = context.metadata.get("operation")
        if not module_name or not operation:
            raise ValueError("У агента нет операции и имени модуля")
        summary = module_manager.apply(operation, module_name)
        manager.finish(
            agent_id=context.agent_id,
            text=f"Закончил работу с модулем {module_name}. {data['summary']}",
        )
        return {"applied": True, **summary}

    return handler
