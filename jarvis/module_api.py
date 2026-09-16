"""Публичный контракт пользовательских модулей.

Модуль — это каталог в ``.jarvis/modules`` с ``module.json`` и Python
файлом, который возвращает :class:`Module`. В модуле можно объявить любое
количество действий и обработчиков, включая ноль действий или ноль
обработчиков.

Минимальный пример ``module.py``::

    from jarvis.module_api import Module, action, event_handler, event

    def send(data, ctx):
        return {"sent": True, "text": data["text"]}

    def poll(ctx):
        ctx.emit("example.message", {"text": "..."})

    def create_module():
        return Module(
            name="example",
            version="1.0.0",
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

from .protocol import Event, JSONSchema


ActionHandler = Callable[[dict[str, Any], "ActionContext"], Any]
HandlerStart = Callable[["ModuleContext"], Any]
HandlerStop = Callable[["ModuleContext"], Any]


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
    audiences: frozenset[str] | None = None
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
    name: str
    version: str
    description: str
    actions: tuple[ActionSpec, ...] = field(default_factory=tuple)
    handlers: tuple[HandlerSpec, ...] = field(default_factory=tuple)


def event(type: str, description: str, data_schema: JSONSchema) -> EventDefinition:
    """Объявить тип события, которое публикует обработчик модуля."""

    return EventDefinition(type, description, data_schema)


def action(
    type: str,
    description: str,
    data_schema: JSONSchema,
    handler: ActionHandler,
    *,
    audiences: Iterable[str] | None = None,
) -> ActionSpec:
    """Объявить действие модуля."""

    return ActionSpec(
        type=type,
        description=description,
        data_schema=data_schema,
        handler=handler,
        audiences=(
            frozenset({"main", "subagent"})
            if audiences is None
            else frozenset(audiences)
        ),
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


@dataclass(slots=True)
class ModuleContext:
    """Контекст фонового обработчика модуля."""

    module_name: str
    module_path: Path
    emit_event: Callable[[Event], None]
    config: Any = None
    stop_event: threading.Event = field(default_factory=threading.Event)

    def emit(
        self,
        type: str,
        data: dict[str, Any],
        *,
        target: str | None = "main",
        reply_to: str | None = None,
    ) -> Event:
        event = Event(
            type=type,
            data=data,
            source=f"module:{self.module_name}",
            target=target,
            reply_to=reply_to,
        )
        self.emit_event(event)
        return event


@dataclass(slots=True)
class ActionContext:
    """Контекст одного вызова действия."""

    agent_id: str
    action_id: str
    event_bus: Any
    agent_manager: Any
    module_manager: Any
    config: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def emit(
        self,
        type: str,
        data: dict[str, Any],
        *,
        target: str | None = "main",
        reply_to: str | None = None,
        source: str | None = None,
    ) -> Event:
        event = Event(
            type=type,
            data=data,
            source=source or f"action:{type}",
            target=target,
            reply_to=reply_to,
        )
        self.event_bus.publish(event)
        return event
