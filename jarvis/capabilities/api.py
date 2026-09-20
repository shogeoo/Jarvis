"""Публичный API действий, handlers, событий и результатов."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from ..core.protocol import ActionResult, Event, InputPart, JSONSchema


ActionHandler = Callable[[dict[str, Any], "ActionContext"], Any]
HandlerStart = Callable[["HandlerContext"], Any]
HandlerStop = Callable[["HandlerContext"], Any]
ContainerPrepare = Callable[[Any], None]
ResultCallback = Callable[[dict[str, Any]], None]


PENDING = object()
"""Маркер отложенного результата действия.

Если функция действия возвращает ``PENDING``, она обязана позже ровно один
раз вызвать ``context.complete``. Ядро не синтезирует результат автоматически.
"""


@dataclass(frozen=True, slots=True)
class EventDefinition:
    type: str
    description: str
    data_schema: JSONSchema


@dataclass(frozen=True, slots=True)
class ActionDefinition:
    id: str
    description: str
    args_schema: JSONSchema
    result_schema: JSONSchema
    run: ActionHandler
    owner: str = ""


@dataclass(frozen=True, slots=True)
class HandlerDefinition:
    id: str
    description: str
    events: tuple[EventDefinition, ...]
    start: HandlerStart
    stop: HandlerStop | None = None
    owner: str = ""


@dataclass(frozen=True, slots=True)
class ModuleContainer:
    module_id: str
    description: str
    execution: str = "in_process"
    requirements: str | None = None
    prepare: ContainerPrepare | None = None


def event_definition(
    type: str, description: str, data_schema: JSONSchema
) -> EventDefinition:
    """Объявить внешнее событие handler."""

    return EventDefinition(type, description, data_schema)


def action_definition(
    id: str,
    description: str,
    args_schema: JSONSchema,
    result_schema: JSONSchema,
    run: ActionHandler,
) -> ActionDefinition:
    """Объявить одно действие в одном файле."""

    return ActionDefinition(
        id=id,
        description=description,
        args_schema=args_schema,
        result_schema=result_schema,
        run=run,
    )


def handler_definition(
    id: str,
    description: str,
    events: Iterable[EventDefinition],
    start: HandlerStart,
    *,
    stop: HandlerStop | None = None,
) -> HandlerDefinition:
    """Объявить один handler в одном файле."""

    return HandlerDefinition(
        id=id,
        description=description,
        events=tuple(events),
        start=start,
        stop=stop,
    )


def input_part(type: str, mime_type: str, base64_data: str) -> InputPart:
    """Создать мультимодальную часть, подготовленную самим handler."""

    return InputPart(type=type, mime_type=mime_type, data=base64_data)


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

    def emit(
        self,
        type: str,
        data: dict[str, Any],
        *,
        target: str | None = None,
        reply_to: str | None = None,
        parts: Iterable[InputPart] = (),
    ) -> Event:
        event = Event(
            type=type,
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
        self.emit_event(event)
        return event


@dataclass(slots=True)
class ActionContext:
    """Служебный конверт передачи действия коду capability."""

    agent_id: str
    action_id: str
    action_type: str
    capability_id: str
    module_id: str | None
    complete: ResultCallback
    config: Any = None
    agent_manager: Any = None
    capabilities: Any = None
    services: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    stop_event: threading.Event = field(default_factory=threading.Event)
