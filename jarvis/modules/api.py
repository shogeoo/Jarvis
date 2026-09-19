"""Публичный контракт пользовательских модулей.

Модуль — комплект обработчиков событий и действий (или только одного из
них). Каталог модуля обязан иметь структуру::

    module/
      module.json
      module.py
      actions/
      handlers/

``module.py`` возвращает :class:`Module`; код конкретных действий и
обработчиков размещается в соответствующих каталогах и импортируется
фабрикой через относительные импорты (например, ``from .actions.send import send``).
Все модули находятся непосредственно в ``.jarvis/modules/<module_id>/``.
Доступ конкретного агента задаётся массивом module_id в его пресете.

Events могут объявляться непосредственно в ``Module.events`` (например,
адресный результат action) либо принадлежать конкретному фоновому handler.
Ядро не создаёт событие результата автоматически: action публикует его через
``ActionContext.emit``.

Минимальный пример ``module.py``::

    from jarvis.modules import Module, action, event_handler, event
    from .actions.send import send
    from .handlers.poll import poll

    def create_module():
        return Module(
            module_id="example",
            description="Пример",
            actions=(action("example.send", "Отправить текст", {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
                "additionalProperties": False,
            }, send),),
            handlers=(event_handler(
                "example.poller", "Получает сообщения", (
                    event("example.message", "Новое сообщение", {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                        "additionalProperties": False,
                    }),
                ), poll),),
        )
"""

from __future__ import annotations

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
    events: tuple[EventDefinition, ...] = field(default_factory=tuple)
    handlers: tuple[HandlerSpec, ...] = field(default_factory=tuple)
    prepare: ModulePrepare | None = None


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
        if not type.startswith(f"{self.module_id}."):
            raise ValueError(
                f"Модуль {self.module_id} не может публиковать событие {type}"
            )
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
    """Контекст вызова; длительные действия завершаются по stop_event."""

    agent_id: str
    action_id: str
    event_bus: Any
    agent_manager: Any
    modules: Any
    module_id: str
    config: Any = None
    services: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    stop_event: threading.Event = field(default_factory=threading.Event)

    def emit(
        self,
        type: str,
        data: dict[str, Any],
        *,
        target: str | None = None,
        reply_to: str | None = None,
        source: str | None = None,
        parts: Iterable[InputPart] = (),
    ) -> Event:
        if not type.startswith(f"{self.module_id}."):
            raise ValueError(
                f"Модуль {self.module_id} не может публиковать событие {type}"
            )
        event = Event(
            type=type,
            data=data,
            source=source or f"action:{type}",
            target=target or self.agent_id,
            reply_to=reply_to,
            parts=tuple(parts),
            module_id=self.module_id,
        )
        self.event_bus.publish(event)
        return event
