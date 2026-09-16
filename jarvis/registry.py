"""Потокобезопасные реестры действий и событий."""

from __future__ import annotations

import threading
from dataclasses import replace
from typing import Any, Iterable

from .module_api import ActionSpec, EventDefinition
from .protocol import JSONSchema, validate_json, validate_strict_schema


class ActionRegistry:
    """Актуальный набор действий, из которого строятся схемы агентов."""

    def __init__(self):
        self._lock = threading.RLock()
        self._actions: dict[str, ActionSpec] = {}

    def register(self, spec: ActionSpec, *, owner: str | None = None) -> ActionSpec:
        if not spec.type:
            raise ValueError("У действия должен быть непустой type")
        if not isinstance(spec.data_schema, dict):
            raise ValueError(f"Схема действия {spec.type} должна быть объектом")
        if spec.data_schema.get("type") != "object":
            raise ValueError(f"Схема действия {spec.type} должна быть объектом")
        validate_strict_schema(spec.data_schema, where=f"схема действия {spec.type}")
        registered = replace(spec, owner=owner or spec.owner)
        with self._lock:
            if registered.type in self._actions:
                raise ValueError(f"Действие уже зарегистрировано: {registered.type}")
            self._actions[registered.type] = registered
        return registered

    def replace_owner(self, specs: Iterable[ActionSpec], *, owner: str) -> None:
        specs = [replace(spec, owner=owner) for spec in specs]
        names = [spec.type for spec in specs]
        if len(names) != len(set(names)):
            raise ValueError(f"Владелец {owner} объявил повторяющееся действие")
        with self._lock:
            for name, old in list(self._actions.items()):
                if old.owner == owner:
                    del self._actions[name]
            for spec in specs:
                if spec.type in self._actions:
                    raise ValueError(f"Действие уже зарегистрировано: {spec.type}")
                self._actions[spec.type] = spec

    def unregister_owner(self, owner: str) -> None:
        with self._lock:
            for name, spec in list(self._actions.items()):
                if spec.owner == owner:
                    del self._actions[name]

    def get(self, action_type: str) -> ActionSpec | None:
        with self._lock:
            return self._actions.get(action_type)

    def require(self, action_type: str) -> ActionSpec:
        spec = self.get(action_type)
        if spec is None:
            raise KeyError(f"Действие не зарегистрировано: {action_type}")
        return spec

    def all(self) -> dict[str, ActionSpec]:
        with self._lock:
            return dict(self._actions)

    def for_agent(
        self,
        audience: str,
        allowed: set[str] | None = None,
    ) -> dict[str, ActionSpec]:
        with self._lock:
            result = {}
            for name, spec in self._actions.items():
                if allowed is not None and name not in allowed:
                    continue
                if spec.audiences is not None and audience not in spec.audiences:
                    continue
                result[name] = spec
            return result

    def validate(self, action_type: str, data: dict[str, Any]) -> ActionSpec:
        spec = self.require(action_type)
        validate_json(data, spec.data_schema, where=f"аргументы {action_type}")
        return spec

    @staticmethod
    def catalog(specs: dict[str, ActionSpec]) -> list[dict[str, Any]]:
        return [
            {
                "type": spec.type,
                "description": spec.description,
                "data_schema": spec.data_schema,
            }
            for spec in sorted(specs.values(), key=lambda item: item.type)
        ]


class EventRegistry:
    """Схемы всех событий, разрешённых внутренней шиной."""

    def __init__(self):
        self._lock = threading.RLock()
        self._events: dict[str, tuple[EventDefinition, str]] = {}

    def register(self, spec: EventDefinition, *, owner: str = "builtin") -> None:
        if not spec.type:
            raise ValueError("У события должен быть непустой type")
        if spec.data_schema.get("type") != "object":
            raise ValueError(f"Схема события {spec.type} должна быть объектом")
        validate_strict_schema(spec.data_schema, where=f"схема события {spec.type}")
        with self._lock:
            if spec.type in self._events:
                raise ValueError(f"Событие уже зарегистрировано: {spec.type}")
            self._events[spec.type] = (spec, owner)

    def replace_owner(self, specs: Iterable[EventDefinition], *, owner: str) -> None:
        specs = list(specs)
        names = [spec.type for spec in specs]
        if len(names) != len(set(names)):
            raise ValueError(f"Владелец {owner} объявил повторяющееся событие")
        with self._lock:
            for name, (_, old_owner) in list(self._events.items()):
                if old_owner == owner:
                    del self._events[name]
            for spec in specs:
                if spec.type in self._events:
                    raise ValueError(f"Событие уже зарегистрировано: {spec.type}")
                self._events[spec.type] = (spec, owner)

    def unregister_owner(self, owner: str) -> None:
        with self._lock:
            for name, (_, event_owner) in list(self._events.items()):
                if event_owner == owner:
                    del self._events[name]

    def get(self, event_type: str) -> EventDefinition | None:
        with self._lock:
            value = self._events.get(event_type)
            return value[0] if value else None

    def validate(self, event_type: str, data: dict[str, Any]) -> None:
        spec = self.get(event_type)
        if spec is None:
            raise ValueError(f"Событие не зарегистрировано: {event_type}")
        validate_json(data, spec.data_schema, where=f"данные события {event_type}")

    def all(self) -> dict[str, EventDefinition]:
        with self._lock:
            return {name: spec for name, (spec, _) in self._events.items()}
