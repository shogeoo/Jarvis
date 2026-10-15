"""Неизменяемое event-thinking-action ядро Jarvis."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

from ..infrastructure.debug import Debugger

if TYPE_CHECKING:
    from .agents import AgentManager
from .protocol import (
    CallResult,
    validate_result,
)


class PendingAction:
    """Незавершённое действие, ожидающее ровно один авторский результат."""

    def __init__(
        self,
        *,
        agent_id: str,
        call_id: str,
        action_id: str,
        result_schema: dict[str, Any],
    ):
        self.agent_id = agent_id
        self.call_id = call_id
        self.action_id = action_id
        self.result_schema = result_schema


class CallResultTracker:
    """Связывает авторские результаты с действиями по call_id."""

    def __init__(self, *, debug: Debugger | None = None):
        self.debug = debug or Debugger(enabled=False)
        self._lock = threading.RLock()
        self._pending: dict[tuple[str, str], PendingAction] = {}

    def has_pending(self, agent_id: str, call_id: str) -> bool:
        with self._lock:
            return (agent_id, call_id) in self._pending

    def begin(
        self,
        *,
        agent_id: str,
        call_id: str,
        action_id: str,
        result_schema: dict[str, Any],
    ) -> None:
        with self._lock:
            if (agent_id, call_id) in self._pending:
                raise ValueError(f"call_id повторяется в ответе: {call_id!r}")
            self._pending[(agent_id, call_id)] = PendingAction(
                agent_id=agent_id,
                call_id=call_id,
                action_id=action_id,
                result_schema=result_schema,
            )

    def complete(
        self,
        manager: "AgentManager",
        *,
        agent_id: str,
        call_id: str,
        data: dict[str, Any],
        parts: tuple = (),
    ) -> bool:
        with self._lock:
            pending = self._pending.get((agent_id, call_id))
        if pending is None:
            self.debug.log(
                "call_result_rejected",
                agent_id=agent_id,
                call_id=call_id,
                reason="unknown_or_completed",
            )
            return False
        try:
            validate_result(
                data,
                pending.result_schema,
                where=f"результат {pending.action_id}",
            )
        except ValueError as exc:
            self.debug.log(
                "call_result_rejected",
                agent_id=agent_id,
                call_id=call_id,
                reason="invalid_schema",
                error=str(exc),
            )
            manager.fail_call(
                agent_id, call_id, f"Некорректный результат {pending.action_id}: {exc}"
            )
            return False
        with self._lock:
            if self._pending.pop((agent_id, call_id), None) is not pending:
                return False
        result = CallResult(
            call_id=call_id, data=data, agent_id=agent_id, parts=tuple(parts)
        )
        return manager.deliver_result(result)

    def discard(self, agent_id: str, call_id: str) -> PendingAction | None:
        with self._lock:
            return self._pending.pop((agent_id, call_id), None)

    def for_agent(self, agent_id: str) -> list[PendingAction]:
        with self._lock:
            return [
                item for item in self._pending.values() if item.agent_id == agent_id
            ]

    def for_capability(
        self, action_ids: set[str], agent_ids: set[str] | None = None
    ) -> list[PendingAction]:
        with self._lock:
            return [
                item
                for item in self._pending.values()
                if item.action_id in action_ids
                and (agent_ids is None or item.agent_id in agent_ids)
            ]

    def discard_agent(self, agent_id: str) -> None:
        with self._lock:
            for key in [key for key in self._pending if key[0] == agent_id]:
                self._pending.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._pending.clear()
