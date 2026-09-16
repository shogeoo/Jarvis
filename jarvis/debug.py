"""Консольный журнал работы event-action ядра."""

from __future__ import annotations

import sys
import threading
from typing import Any

from .protocol import json_text


class Debugger:
    """Печатает полные структурированные сообщения без сокращения полей."""

    def __init__(self, *, enabled: bool = True, stream=None):
        self.enabled = enabled
        self.stream = stream or sys.stderr
        self._lock = threading.Lock()

    def log(self, kind: str, **data: Any) -> None:
        if not self.enabled:
            return
        payload = {"kind": kind, **data}
        line = f"[jarvis:{kind}]\n{json_text(payload, indent=2)}\n"
        with self._lock:
            self.stream.write(line)
            self.stream.flush()

    def event(self, direction: str, agent_id: str, event: Any) -> None:
        self.log(
            "event",
            direction=direction,
            agent_id=agent_id,
            event=event.debug_value(),
        )

    def action(self, stage: str, agent_id: str, action: Any, **extra: Any) -> None:
        self.log(
            f"action_{stage}",
            agent_id=agent_id,
            action_id=action.id,
            action=action.model_value(),
            **extra,
        )

    def model(self, agent_id: str, output: str, *, actions: Any = None) -> None:
        data: dict[str, Any] = {"agent_id": agent_id, "output": output}
        if actions is not None:
            data["actions"] = [action.model_value() for action in actions]
        self.log("model_output", **data)

    def state(self, agent_id: str, state: str, **extra: Any) -> None:
        self.log("agent_state", agent_id=agent_id, state=state, **extra)
