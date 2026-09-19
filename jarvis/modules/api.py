"""Стабильный публичный API подключаемых модулей.

Action является быстрым dispatcher: он помещает :class:`ActionTask` в
:class:`ActionQueue` и сразу возвращается. Только фоновые handlers публикуют
model-visible events через :meth:`ModuleContext.emit`. Поэтому модуль без
handlers не способен вернуть модели результат действия.
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from ..core.protocol import Event, InputPart, JSONSchema


ActionHandler = Callable[[dict[str, Any], "ActionContext"], Any]
HandlerStart = Callable[["ModuleContext"], Any]
HandlerStop = Callable[["ModuleContext"], Any]
ModulePrepare = Callable[[Any], None]


@dataclass(frozen=True, slots=True)
class EventDefinition:
    type: str
    description: str
    data_schema: JSONSchema


@dataclass(frozen=True, slots=True)
class ActionSpec:
    type: str
    description: str
    data_schema: JSONSchema
    handler: ActionHandler
    owner: str = "module"


@dataclass(frozen=True, slots=True)
class HandlerSpec:
    name: str
    description: str
    events: tuple[EventDefinition, ...]
    start: HandlerStart
    stop: HandlerStop | None = None


@dataclass(frozen=True, slots=True)
class Module:
    module_id: str
    description: str
    actions: tuple[ActionSpec, ...] = field(default_factory=tuple)
    handlers: tuple[HandlerSpec, ...] = field(default_factory=tuple)
    prepare: ModulePrepare | None = None


@dataclass(frozen=True, slots=True)
class ActionTask:
    """Внутренняя задача модуля, невидимая модели."""

    agent_id: str
    action_id: str
    action_type: str
    data: dict[str, Any]


class ActionQueue:
    """Связывает быстрые action-dispatchers с фоновым handler."""

    def __init__(self):
        self._items: queue.Queue[ActionTask | None] = queue.Queue()

    def submit(self, data: dict[str, Any], context: "ActionContext") -> None:
        self._items.put(
            ActionTask(
                agent_id=context.agent_id,
                action_id=context.action_id,
                action_type=context.action_type,
                data=dict(data),
            )
        )

    def get(self, timeout: float = 0.1) -> ActionTask | None:
        try:
            return self._items.get(timeout=timeout)
        except queue.Empty:
            return None

    def clear(self) -> None:
        while True:
            try:
                self._items.get_nowait()
            except queue.Empty:
                return

    def close(self) -> None:
        self._items.put(None)


def event(type: str, description: str, data_schema: JSONSchema) -> EventDefinition:
    """Объявить тип события, которое публикует обработчик модуля."""

    return EventDefinition(type, description, data_schema)


def action(
    type: str,
    description: str,
    data_schema: JSONSchema,
    handler: ActionHandler,
) -> ActionSpec:
    """Объявить действие модуля."""

    return ActionSpec(
        type=type,
        description=description,
        data_schema=data_schema,
        handler=handler,
    )


def event_handler(
    name: str,
    description: str,
    events: Iterable[EventDefinition],
    start: HandlerStart,
    *,
    stop: HandlerStop | None = None,
) -> HandlerSpec:
    """Объявить один фоновый обработчик событий модуля."""

    return HandlerSpec(
        name=name,
        description=description,
        events=tuple(events),
        start=start,
        stop=stop,
    )


def input_part(type: str, mime_type: str, base64_data: str) -> InputPart:
    """Создать мультимодальную часть, подготовленную самим модулем."""

    return InputPart(type=type, mime_type=mime_type, data=base64_data)


@dataclass(slots=True)
class ModuleContext:
    """Контекст фонового обработчика модуля."""

    module_id: str
    module_path: Path
    emit_event: Callable[[Event], None]
    agent_manager: Any = None
    modules: Any = None
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
            source=f"module:{self.module_id}",
            target=target,
            reply_to=reply_to,
            parts=tuple(parts),
            module_id=self.module_id,
        )
        self.emit_event(event)
        return event


@dataclass(slots=True)
class ActionContext:
    """Служебный конверт передачи action коду модуля."""

    agent_id: str
    action_id: str
    action_type: str
    module_id: str
    config: Any = None
    services: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    stop_event: threading.Event = field(default_factory=threading.Event)
