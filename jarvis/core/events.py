"""Неизменяемое event-thinking-action ядро Jarvis."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from ..infrastructure.debug import Debugger

if TYPE_CHECKING:
    from .agent import Agent
from .protocol import (
    Event,
)
from .registry import EventRegistry


class EventBus:
    """Адресно доставляет события actions и рассылает события handlers."""

    def __init__(self, events: EventRegistry, *, debug: Debugger | None = None):
        self.events = events
        self.debug = debug or Debugger(enabled=False)
        self._agents: dict[str, Agent] = {}
        self._lock = threading.RLock()

    def bind(self, agent: "Agent") -> None:
        with self._lock:
            self._agents[agent.agent_id] = agent

    def unbind(self, agent_id: str) -> None:
        with self._lock:
            self._agents.pop(agent_id, None)

    def publish(self, event: Event) -> bool:
        try:
            self.events.validate(event.event_id, event.data)
        except ValueError as exc:
            self.debug.log("event_rejected", event=event.debug_value(), error=str(exc))
            return False

        with self._lock:
            if event.target is not None:
                recipients = [self._agents.get(event.target)]
            elif event.handler_id is not None:
                recipients = [
                    agent
                    for agent in self._agents.values()
                    if agent.accepts_event(event)
                ]
            else:
                recipients = []
            delivered = False
            for agent in recipients:
                if agent is not None and (
                    event.handler_id is None or agent.accepts_event(event)
                ):
                    accepted = agent.enqueue(event)
                    delivered = (accepted is not False) or delivered
        if not delivered:
            self.debug.log(
                "event_dropped",
                reason="no_recipient",
                event=event.debug_value(),
            )
        return delivered
