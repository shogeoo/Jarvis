"""Публичный API действий, handlers, событий и результатов.

Идентификаторы в коде не пишутся: фабрики возвращают определения без ID,
а загрузчик назначает ID из имени файла. Источник определения фиксируется
автоматически для сверки «один файл — одна единица».
"""

from __future__ import annotations

import inspect
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from ..core.protocol import ActionResult, Event, InputPart, JSONSchema


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
    source: str = ""
    owner: str = ""


@dataclass(frozen=True, slots=True)
class HandlerDefinition:
    id: str
    description: str
    event: EventDefinition
    start: HandlerStart
    stop: HandlerStop | None = None
    source: str = ""
    owner: str = ""


@dataclass(frozen=True, slots=True)
class ModuleDefinition:
    description: str
    actions: tuple[ActionDefinition, ...]
    handlers: tuple[HandlerDefinition, ...]
    execution: str = "in_process"
    prepare: LifecycleHook | None = None
    teardown: LifecycleHook | None = None


def event_definition(
    type: str, description: str, data_schema: JSONSchema
) -> EventDefinition:
    """Объявить внешнее событие handler."""

    return EventDefinition(type, description, data_schema)


def action_definition(
    description: str,
    args_schema: JSONSchema,
    result_schema: JSONSchema,
    run: ActionHandler,
) -> ActionDefinition:
    """Объявить одно действие в одном файле. ID назначит загрузчик."""

    return ActionDefinition(
        id="",
        description=description,
        args_schema=args_schema,
        result_schema=result_schema,
        run=run,
        source=_caller_source(),
    )


def handler_definition(
    description: str,
    event: EventDefinition,
    start: HandlerStart,
    *,
    stop: HandlerStop | None = None,
) -> HandlerDefinition:
    """Объявить один handler с ровно одним событием. ID назначит загрузчик."""

    return HandlerDefinition(
        id="",
        description=description,
        event=event,
        start=start,
        stop=stop,
        source=_caller_source(),
    )


def module_definition(
    description: str,
    actions: Iterable[ActionDefinition],
    handlers: Iterable[HandlerDefinition],
    *,
    execution: str = "in_process",
    prepare: LifecycleHook | None = None,
    teardown: LifecycleHook | None = None,
) -> ModuleDefinition:
    """Собрать модуль-контейнер в обязательном module.py."""

    return ModuleDefinition(
        description=description,
        actions=tuple(actions),
        handlers=tuple(handlers),
        execution=execution,
        prepare=prepare,
        teardown=teardown,
    )


def input_part(type: str, mime_type: str, base64_data: str) -> InputPart:
    """Создать мультимодальную часть результата или события."""

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
        event: EventDefinition | str,
        data: dict[str, Any],
        *,
        target: str | None = None,
        reply_to: str | None = None,
        parts: Iterable[InputPart] = (),
    ) -> Event:
        type_name = event.type if isinstance(event, EventDefinition) else event
        item = Event(
            type=type_name,
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
