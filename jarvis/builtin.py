"""Захардкоженные базовые события и действия системы."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .lifecycle import terminate_process
from .module_api import ActionSpec, EventDefinition
from .prompts import MODULE_MANAGER_SYSTEM_PROMPT
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
            "model_error",
            "Ответ модели не удалось разобрать или проверить по JSON Schema.",
            _schema(code=STRING, message=STRING, response=STRING),
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
            name="module_manager",
            description="Управляет пользовательскими модулями.",
            system_prompt=MODULE_MANAGER_SYSTEM_PROMPT,
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
            description="Создать экземпляр выбранного пресета с отдельными контекстом, очередью и потоком.",
            data_schema=_schema(preset=STRING),
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
            type="agent.create",
            description="Создать или обновить переиспользуемый пресет агента.",
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
            owner="builtin",
        ),
        ActionSpec(
            type="agent.preset_delete",
            description="Удалить сохранённый пресет агента.",
            data_schema=_schema(name=STRING),
            handler=lambda data, context: context.agent_manager.delete_preset(data["name"]),
            owner="builtin",
        ),
        ActionSpec(
            type="agent.preset_list",
            description="Получить сохранённые пресеты агентов.",
            data_schema=empty_object_schema(),
            handler=lambda data, context: {"presets": context.agent_manager.list_presets()},
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
            description="Получить список файлов из рабочей папки агента управления модулями.",
            data_schema=_schema(path=STRING),
            handler=_workspace_list,
            owner="builtin",
        ),
        ActionSpec(
            type="workspace.read",
            description="Прочитать файл из рабочей папки агента управления модулями.",
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
            description="Применить подготовленные изменения пользовательского модуля.",
            data_schema=_schema(
                module=STRING,
                operation={"type": "string", "enum": ["create", "update", "delete"]},
                summary=STRING,
            ),
            handler=_module_complete,
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
            preset=data["preset"],
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


def _module_complete(data: dict[str, Any], context: Any) -> dict[str, Any]:
    module_manager = context.module_manager
    if module_manager is None:
        raise RuntimeError("Менеджер модулей не инициализирован")
    summary = module_manager.apply(data["operation"], data["module"])
    return {"applied": True, **summary}
