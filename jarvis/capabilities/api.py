"""Публичный API действий, handlers, событий и результатов.

Фабрики предоставляют JSON-декларации. Полные идентификаторы совпадают
с каталогами и namespace модуля. Источник определения фиксируется
автоматически для сверки «один каталог — одна единица».

Каждая единица — самодостаточный каталог. .env и .venv необязательны.
prepare/teardown управляют её ресурсами при старте и остановке.
"""

from __future__ import annotations

import inspect
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from ..core.protocol import Event, InputPart, JSONSchema, parameter_schema, result_description, validate_result_schema, validate_data_schema, validate_catalog_text


ActionHandler = Callable[[dict[str, Any], "ActionContext"], Any]
HandlerStart = Callable[["HandlerContext"], Any]
HandlerStop = Callable[["HandlerContext"], Any]
LifecycleHook = Callable[[Any], None]
ResultCallback = Callable[..., None]


PENDING = object()
"""Маркер отложенного результата действия.

Если функция действия возвращает ``PENDING``, она обязана позже ровно один
раз вызвать ``context.complete``. Ядро не синтезирует результат автоматически.
"""


def _caller_source() -> str:
    frame = inspect.currentframe()
    try:
        outer = frame.f_back if frame is not None else None
        while outer is not None and outer.f_code.co_filename == __file__:
            outer = outer.f_back
        return outer.f_code.co_filename if outer is not None else ""
    finally:
        del frame


@dataclass(frozen=True, slots=True)
class EventDefinition:
    event_id: str
    description: str
    data_schema: JSONSchema

    def __post_init__(self):
        object.__setattr__(self, "data_schema", parameter_schema(self.data_schema))


@dataclass(frozen=True, slots=True)
class ActionDefinition:
    id: str
    description: str
    data_schema: JSONSchema
    result_schema: JSONSchema
    run: ActionHandler
    source: str = ""
    owner: str = ""
    prepare: LifecycleHook | None = None
    teardown: LifecycleHook | None = None

    def __post_init__(self):
        object.__setattr__(self, "data_schema", parameter_schema(self.data_schema))
        object.__setattr__(self, "result_schema", result_description(self.result_schema))


@dataclass(frozen=True, slots=True)
class HandlerDefinition:
    id: str
    description: str
    event: EventDefinition
    start: HandlerStart
    stop: HandlerStop | None = None
    source: str = ""
    owner: str = ""
    prepare: LifecycleHook | None = None
    teardown: LifecycleHook | None = None


@dataclass(frozen=True, slots=True)
class ModuleDefinition:
    description: str
    actions: tuple[ActionDefinition, ...]
    handlers: tuple[HandlerDefinition, ...]
    prepare: LifecycleHook | None = None
    teardown: LifecycleHook | None = None
    module_id: str = ""


def event_definition(
    event_id: str, description: str, data_schema: JSONSchema
) -> EventDefinition:
    """Объявить внешнее событие handler."""

    return EventDefinition(event_id, description, data_schema)


def _check_parameter_document(schema: JSONSchema) -> None:
    validate_data_schema(schema)
    validate_catalog_text(schema)
    if "additionalProperties" in schema:
        raise ValueError("Use properties to declare allowed parameters; omit additionalProperties")
    for name, child in schema.get("properties", {}).items():
        if not {"type", "description", "default"} <= set(child):
            raise ValueError(f"Parameter {name} must declare type, description and default")
        if name in schema.get("required", []) and child["default"] is not None:
            raise ValueError("Required parameter defaults must be null")
        _check_parameter_document(child)
    if isinstance(schema.get("items"), dict):
        _check_parameter_document(schema["items"])


def action_definition(
    description: str | dict[str, Any],
    data_schema: JSONSchema | None = None,
    result_schema: JSONSchema | None = None,
    run: ActionHandler | None = None,
    *,
    prepare: LifecycleHook | None = None,
    teardown: LifecycleHook | None = None,
) -> ActionDefinition:
    """Declare one action; the loader verifies its full directory identifier."""

    action_id = ""
    if isinstance(description, dict):
        document = description
        if set(document) != {"action_id", "description", "data_schema", "result_schema"}:
            raise ValueError("Action description must contain action_id, description, data_schema and result_schema")
        action_id = document["action_id"]
        if not isinstance(action_id, str) or not action_id:
            raise ValueError("Action description requires a non-empty action_id")
        description, data_schema, result_schema = document["description"], document["data_schema"], document["result_schema"]
        _check_parameter_document(data_schema)
        validate_result_schema(result_schema)
        validate_catalog_text(document)
    return ActionDefinition(
        id=action_id,
        description=description,
        data_schema=data_schema,
        result_schema=result_schema,
        run=run,
        source=_caller_source(),
        prepare=prepare,
        teardown=teardown,
    )


def handler_definition(
    description: str | dict[str, Any],
    event: EventDefinition | None = None,
    start: HandlerStart | None = None,
    *,
    stop: HandlerStop | None = None,
    prepare: LifecycleHook | None = None,
    teardown: LifecycleHook | None = None,
) -> HandlerDefinition:
    """Declare one handler and exactly one explicitly named event."""

    handler_id = ""
    if isinstance(description, dict):
        document = description
        if set(document) != {"handler_id", "description", "event"}:
            raise ValueError("Handler description must contain handler_id, description and one event")
        event_doc = document["event"]
        if set(event_doc) != {"event_id", "description", "data_schema"}:
            raise ValueError("Handler must declare exactly one event_id, description and data_schema")
        handler_id, description = document["handler_id"], document["description"]
        if not isinstance(handler_id, str) or not handler_id or not isinstance(event_doc["event_id"], str) or not event_doc["event_id"]:
            raise ValueError("Handler description requires non-empty handler_id and event_id")
        _check_parameter_document(event_doc["data_schema"])
        validate_catalog_text(document)
        event = event_definition(event_doc["event_id"], event_doc["description"], event_doc["data_schema"])
    return HandlerDefinition(
        id=handler_id,
        description=description,
        event=event,
        start=start,
        stop=stop,
        source=_caller_source(),
        prepare=prepare,
        teardown=teardown,
    )


def module_definition(
    description: str | dict[str, Any],
    actions: Iterable[ActionDefinition],
    handlers: Iterable[HandlerDefinition],
    *,
    prepare: LifecycleHook | None = None,
    teardown: LifecycleHook | None = None,
) -> ModuleDefinition:
    """Собрать модуль-контейнер в обязательном module.py."""

    module_id = ""
    if isinstance(description, dict):
        if set(description) != {"module_id", "description"}:
            raise ValueError("Module description must contain module_id and description")
        # IDs of members are checked against their module namespace by loader.
        module_id, description = description["module_id"], description["description"]
        if not isinstance(module_id, str) or not module_id:
            raise ValueError("Module description requires a non-empty module_id")
    return ModuleDefinition(
        description=description,
        actions=tuple(actions),
        handlers=tuple(handlers),
        prepare=prepare,
        teardown=teardown,
        module_id=module_id,
    )


def input_part(
    type: str, mime_type: str, base64_data: str, name: str = ""
) -> InputPart:
    """Создать мультимодальную часть результата или события.

    ``name`` используется для file-частей как имя файла.
    """

    return InputPart(type=type, mime_type=mime_type, data=base64_data, name=name)


@dataclass(slots=True)
class HandlerContext:
    """Контекст фонового наблюдателя среды."""

    handler_id: str
    unit_path: Path
    emit_event: Callable[[Event], None]
    module_id: str | None = None
    agent_manager: Any = None
    capabilities: Any = None
    config: Any = None
    services: dict[str, Any] = field(default_factory=dict)
    stop_event: threading.Event = field(default_factory=threading.Event)
    event_id: str = ""

    def emit(
        self,
        event_id: EventDefinition | str,
        data: dict[str, Any],
        *,
        target: str | None = None,
        reply_to: str | None = None,
        parts: Iterable[InputPart] = (),
    ) -> Event:
        event_id = event_id.event_id if isinstance(event_id, EventDefinition) else event_id
        if self.event_id and event_id != self.event_id:
            raise ValueError("A handler may emit only its declared event_id")
        item = Event(
            event_id=event_id,
            data=data,
            source=(
                f"module:{self.module_id}"
                if self.module_id is not None
                else f"handler:{self.handler_id}"
            ),
            target=target,
            reply_to=reply_to,
            parts=tuple(parts),
            module_id=self.module_id,
            handler_id=self.handler_id,
        )
        self.emit_event(item)
        return item


@dataclass(slots=True)
class ActionContext:
    """Служебный конверт передачи действия коду capability."""

    agent_id: str
    call_id: str
    action_id: str
    capability_id: str
    module_id: str | None
    complete: ResultCallback
    config: Any = None
    agent_manager: Any = None
    capabilities: Any = None
    services: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    stop_event: threading.Event = field(default_factory=threading.Event)
