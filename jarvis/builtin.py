"""Встроенные события и действия ядра Jarvis.

Эти компоненты находятся в исходниках и никогда не загружаются из
``.jarvis/modules``. Пользовательские модули получают тот же протокол через
публичный ``jarvis.module_api``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

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
            "agent.task",
            "Задание, с которым родитель запустил субагента.",
            _schema(
                agent_id=STRING,
                parent_id=STRING,
                preset=STRING,
                task=STRING,
            ),
        ),
        EventDefinition(
            "agent.message",
            "Структурированное текстовое сообщение между родителем и ребёнком.",
            _schema(from_agent=STRING, text=STRING),
        ),
        EventDefinition(
            "agent.completed",
            "Субагент закончил работу.",
            _schema(agent_id=STRING, summary=STRING),
        ),
        EventDefinition(
            "agent.failed",
            "Субагент не смог обработать событие или запрос модели.",
            _schema(agent_id=STRING, error=STRING),
        ),
        EventDefinition(
            "agent.interrupted",
            "Субагент был прерван.",
            _schema(agent_id=STRING, reason=STRING),
        ),
        EventDefinition(
            "agent.deleted",
            "Субагент был удалён.",
            _schema(agent_id=STRING, reason=STRING),
        ),
        EventDefinition(
            "module.completed",
            "Модуль подключён, обновлён или удалён.",
            _schema(
                agent_id=STRING,
                module=STRING,
                operation={"type": "string", "enum": ["create", "update", "delete"]},
                version=NULLABLE_STRING,
                summary=STRING,
                actions={"type": "array", "items": STRING},
                events={"type": "array", "items": STRING},
                handlers={"type": "array", "items": STRING},
            ),
        ),
        EventDefinition(
            "handler_error",
            "Фоновый обработчик модуля завершился с ошибкой.",
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
    """Зарегистрировать базовые действия для главного и рабочих агентов."""

    manager.add_preset(
        AgentPreset(
            name="module_builder",
            description="Создаёт, изменяет и удаляет пользовательские модули.",
            system_prompt=MODULE_BUILDER_INSTRUCTIONS,
            audience="module_builder",
        ),
        persist=False,
    )
    actions = [
        ActionSpec(
            type="no_action",
            description="Закончить текущий цикл и ждать следующего события.",
            data_schema=empty_object_schema(),
            handler=lambda data, context: None,
            audiences=None,
            owner="builtin",
        ),
        ActionSpec(
            type="speech",
            description="Озвучить текст. В text можно использовать теги Fish Audio.",
            data_schema=_schema(text=STRING),
            handler=_speech(printer, speaker),
            audiences=frozenset({"main", "subagent"}),
            owner="builtin",
        ),
        ActionSpec(
            type="agent.spawn",
            description="Запустить субагента параллельно с заданием.",
            data_schema=_schema(
                preset=STRING,
                task=STRING,
                system_prompt=NULLABLE_STRING,
                actions={"type": "array", "items": STRING},
            ),
            handler=_spawn(manager),
            audiences=frozenset({"main", "subagent"}),
            owner="builtin",
        ),
        ActionSpec(
            type="agent.message",
            description="Передать структурированное текстовое сообщение родителю или ребёнку.",
            data_schema=_schema(agent_id=STRING, text=STRING),
            handler=_message(manager),
            audiences=frozenset({"main", "subagent", "module_builder"}),
            owner="builtin",
        ),
        ActionSpec(
            type="agent.interrupt",
            description="Прервать работающего субагента.",
            data_schema=_schema(agent_id=STRING, reason=STRING),
            handler=_interrupt(manager),
            audiences=frozenset({"main", "subagent"}),
            owner="builtin",
        ),
        ActionSpec(
            type="agent.delete",
            description="Остановить и удалить субагента.",
            data_schema=_schema(agent_id=STRING, reason=STRING),
            handler=_delete(manager),
            audiences=frozenset({"main", "subagent"}),
            owner="builtin",
        ),
        ActionSpec(
            type="agent.list",
            description="Получить состояния всех запущенных агентов.",
            data_schema=empty_object_schema(),
            handler=lambda data, context: {"agents": manager.list_agents()},
            audiences=frozenset({"main", "subagent"}),
            owner="builtin",
        ),
        ActionSpec(
            type="agent.preset_create",
            description="Сохранить или обновить пресет субагента.",
            data_schema=_schema(
                name=STRING,
                description=STRING,
                system_prompt=STRING,
                actions={"type": "array", "items": STRING},
            ),
            handler=lambda data, context: context.agent_manager.create_preset(
                name=data["name"],
                description=data["description"],
                system_prompt=data["system_prompt"],
                allowed_actions=data["actions"],
            ),
            audiences=frozenset({"main", "subagent"}),
            owner="builtin",
        ),
        ActionSpec(
            type="agent.preset_delete",
            description="Удалить сохранённый пресет субагента.",
            data_schema=_schema(name=STRING),
            handler=lambda data, context: context.agent_manager.delete_preset(data["name"]),
            audiences=frozenset({"main", "subagent"}),
            owner="builtin",
        ),
        ActionSpec(
            type="agent.preset_list",
            description="Получить сохранённые пресеты субагентов.",
            data_schema=empty_object_schema(),
            handler=lambda data, context: {"presets": context.agent_manager.list_presets()},
            audiences=frozenset({"main", "subagent"}),
            owner="builtin",
        ),
        ActionSpec(
            type="agent.complete",
            description="Сообщить родителю о завершении своей задачи.",
            data_schema=_schema(summary=STRING),
            handler=_complete(manager),
            audiences=frozenset({"subagent"}),
            owner="builtin",
        ),
        ActionSpec(
            type="module.create",
            description="Запустить разработчика для создания модуля.",
            data_schema=_schema(module=STRING, request=STRING),
            handler=_module_request(manager, "create"),
            audiences=frozenset({"main", "subagent"}),
            owner="builtin",
        ),
        ActionSpec(
            type="module.update",
            description="Запустить разработчика для изменения модуля.",
            data_schema=_schema(module=STRING, request=STRING),
            handler=_module_request(manager, "update"),
            audiences=frozenset({"main", "subagent"}),
            owner="builtin",
        ),
        ActionSpec(
            type="module.delete",
            description="Запустить разработчика для удаления модуля.",
            data_schema=_schema(module=STRING, request=STRING),
            handler=_module_request(manager, "delete"),
            audiences=frozenset({"main", "subagent"}),
            owner="builtin",
        ),
        ActionSpec(
            type="module.list",
            description="Получить список подключённых модулей.",
            data_schema=empty_object_schema(),
            handler=lambda data, context: {"modules": manager.module_manager.list_modules()},
            audiences=frozenset({"main", "subagent"}),
            owner="builtin",
        ),
        ActionSpec(
            type="module.describe",
            description="Получить полное описание подключённого модуля.",
            data_schema=_schema(module=STRING),
            handler=lambda data, context: manager.module_manager.describe(data["module"]),
            audiences=frozenset({"main", "subagent"}),
            owner="builtin",
        ),
        ActionSpec(
            type="workspace.list",
            description="Список файлов в рабочей папке модуля.",
            data_schema=_schema(path=STRING),
            handler=_workspace_list,
            audiences=frozenset({"module_builder", "subagent"}),
            owner="builtin",
        ),
        ActionSpec(
            type="workspace.read",
            description="Прочитать файл из рабочей папки.",
            data_schema=_schema(path=STRING),
            handler=_workspace_read,
            audiences=frozenset({"module_builder", "subagent"}),
            owner="builtin",
        ),
        ActionSpec(
            type="workspace.write",
            description="Создать или полностью записать файл.",
            data_schema=_schema(path=STRING, content=STRING),
            handler=_workspace_write,
            audiences=frozenset({"module_builder", "subagent"}),
            owner="builtin",
        ),
        ActionSpec(
            type="workspace.delete",
            description="Удалить файл или каталог из рабочей папки.",
            data_schema=_schema(path=STRING, recursive=BOOLEAN),
            handler=_workspace_delete,
            audiences=frozenset({"module_builder", "subagent"}),
            owner="builtin",
        ),
        ActionSpec(
            type="process.run",
            description="Запустить shell-команду и вернуть полный вывод.",
            data_schema=_schema(command=STRING, cwd=NULLABLE_STRING, timeout_seconds=INTEGER),
            handler=_process_run,
            audiences=frozenset({"module_builder", "subagent"}),
            owner="builtin",
        ),
        ActionSpec(
            type="module.validate",
            description="Проверить module.json и factory модуля без подключения.",
            data_schema=_schema(module=STRING),
            handler=lambda data, context: context.module_manager.validate(data["module"]),
            audiences=frozenset({"module_builder"}),
            owner="builtin",
        ),
        ActionSpec(
            type="module.complete",
            description="Применить готовый модуль и сообщить родителю результат.",
            data_schema=_schema(summary=STRING),
            handler=_module_complete(manager),
            audiences=frozenset({"module_builder"}),
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
        speaker.submit(text)
        return {"spoken": True}

    return handler


def _spawn(manager: AgentManager):
    def handler(data: dict[str, Any], context: Any) -> dict[str, Any]:
        return manager.spawn(
            parent_id=context.agent_id,
            preset=data["preset"],
            task=data["task"],
            system_prompt=data["system_prompt"],
            allowed_actions=data["actions"] or None,
            metadata={"preset": data["preset"]},
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


def _complete(manager: AgentManager):
    def handler(data: dict[str, Any], context: Any) -> dict[str, Any]:
        return manager.complete(agent_id=context.agent_id, summary=data["summary"])

    return handler


def _module_request(manager: AgentManager, operation: str):
    def handler(data: dict[str, Any], context: Any) -> dict[str, Any]:
        module_name = data["module"]
        if manager.module_manager is None:
            raise RuntimeError("Менеджер модулей не инициализирован")
        manager.module_manager.validate_name(module_name)
        workspace = manager.module_manager.modules_dir / module_name
        task = (
            f"Операция: {operation}. Модуль: {module_name}. Рабочая папка: {workspace}.\n"
            f"Требования родителя:\n{data['request']}\n"
            "Работай только с этим модулем и сообщай вопросы родителю."
        )
        result = manager.spawn(
            parent_id=context.agent_id,
            preset="module_builder",
            task=task,
            metadata={
                "preset": "module_builder",
                "module": module_name,
                "operation": operation,
                "workspace": str(workspace),
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
    try:
        result = subprocess.run(
            data["command"],
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=data["timeout_seconds"],
        )
        return {
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "returncode": None,
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or "",
            "timed_out": True,
        }


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
        manager.complete(
            agent_id=context.agent_id,
            summary=data["summary"],
            event_type="module.completed",
            extra={
                "module": module_name,
                "operation": operation,
                "version": summary.get("version"),
                "actions": [item["type"] for item in summary.get("actions", [])],
                "events": [item["type"] for item in summary.get("events", [])],
                "handlers": [item["name"] for item in summary.get("handlers", [])],
            },
        )
        return {"applied": True, **summary}

    return handler
