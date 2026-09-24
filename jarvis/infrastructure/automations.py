"""Persistent exact-match automation rules for model-facing inputs."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any


class AutomationStore:
    """Read, append, and remove validated JSON automation rules."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()

    @staticmethod
    def validate(automation: Any) -> dict[str, Any]:
        if not isinstance(automation, dict) or set(automation) not in (
            {"event", "actions"},
            {"call_result", "actions"},
        ):
            raise ValueError("Automation must contain exactly one trigger and actions")

        if "event" in automation:
            trigger = automation["event"]
            valid_event = (
                isinstance(trigger, dict)
                and set(trigger) in ({"handler_id", "data"}, {"type", "data"})
                and isinstance(trigger.get("data"), dict)
                and isinstance(trigger.get("handler_id", trigger.get("type")), str)
                and bool(trigger.get("handler_id", trigger.get("type")))
            )
            if not valid_event:
                raise ValueError("event must be a model-facing event object")
        else:
            trigger = automation["call_result"]
            if (
                not isinstance(trigger, dict)
                or set(trigger) != {"type", "call_id", "data"}
                or trigger.get("type") != "call_result"
                or not isinstance(trigger.get("call_id"), str)
                or not trigger["call_id"].strip()
                or not isinstance(trigger.get("data"), dict)
            ):
                raise ValueError("call_result must match the model-facing call_result shape")

        actions = automation["actions"]
        if not isinstance(actions, list) or not actions:
            raise ValueError("actions must be a non-empty array")
        for index, action in enumerate(actions):
            if (
                not isinstance(action, dict)
                or set(action) != {"action_id", "data"}
                or not isinstance(action.get("action_id"), str)
                or not action["action_id"]
                or action["action_id"] == "no_action"
                or not isinstance(action.get("data"), dict)
            ):
                raise ValueError(
                    f"actions[{index}] must contain action_id and data, without call_id"
                )
        return json.loads(json.dumps(automation, ensure_ascii=False))

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            try:
                value = json.loads(self.path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                self._write([])
                return []
            if not isinstance(value, list):
                raise ValueError(f"{self.path} must contain a JSON array")
            return [self.validate(item) for item in value]

    def append(self, automation: Any) -> int:
        validated = self.validate(automation)
        with self._lock:
            current = self.list()
            current.append(validated)
            self._write(current)
            return len(current)

    def remove(self, automation: Any) -> int:
        """Remove every rule identical to the given one; return how many were removed."""
        target = self.validate(automation)
        with self._lock:
            current = self.list()
            wanted = self._rule_identity(target)
            kept = [
                item
                for item in current
                if not self._same_json(self._rule_identity(item), wanted)
            ]
            removed = len(current) - len(kept)
            if removed:
                self._write(kept)
            return removed

    @staticmethod
    def _rule_identity(automation: dict[str, Any]) -> dict[str, Any]:
        # The call_id value identifies one invocation, so it is not part of a
        # rule's identity — the same comparison matching() uses for call_result.
        rule = json.loads(json.dumps(automation, ensure_ascii=False))
        if "call_result" in rule:
            rule["call_result"].pop("call_id", None)
        return rule

    def matching(self, model_value: dict[str, Any]) -> list[dict[str, Any]]:
        matches = []
        for automation in self.list():
            trigger_key = "event" if "event" in automation else "call_result"
            if trigger_key == "event":
                if self._same_json(automation[trigger_key], model_value):
                    matches.append(automation)
                continue

            actual = dict(model_value)
            expected = dict(automation[trigger_key])
            # The call id identifies one invocation, so a rule matches any call
            # result whose remaining model-facing structure and values are exact.
            actual.pop("call_id", None)
            expected.pop("call_id", None)
            if self._same_json(actual, expected):
                matches.append(automation)
        return matches

    @staticmethod
    def _same_json(left: Any, right: Any) -> bool:
        return json.dumps(
            left, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ) == json.dumps(
            right, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )

    def _write(self, value: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix="automations.", suffix=".tmp", dir=self.path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)
