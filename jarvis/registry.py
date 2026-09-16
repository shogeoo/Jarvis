"""Потокобезопасные реестры действий и событий."""

from __future__ import annotations

import threading
from dataclasses import replace
from typing import Any, Iterable

from .module_api import ActionSpec, EventDefinition
from .protocol import validate_json, validate_strict_schema


class ActionRegistry:
    """Актуальный набор действий с поддержкой нескольких областей."""

    def __init__(self):
        self._lock = threading.RLock()
        self._records: dict[str, dict[str, ActionSpec]] = {}
        self._actions: dict[str, ActionSpec] = {}

    @staticmethod
    def _validate_spec(spec: ActionSpec) -> None:
        if not spec.type:
            raise ValueError("У действия должен быть непустой type")
        if not isinstance(spec.data_schema, dict):
            raise ValueError(f"Схема действия {spec.type} должна быть объектом")
        if spec.data_schema.get("type") != "object":
            raise ValueError(f"Схема действия {spec.type} должна быть объектом")
        validate_strict_schema(spec.data_schema, where=f"схема действия {spec.type}")

    def register(self, spec: ActionSpec, *, owner: str | None = None) -> ActionSpec:
        self._validate_spec(spec)
        owner_name = owner or spec.owner
        with self._lock:
            self._records.setdefault(spec.type, {})[owner_name] = replace(
                spec, owner=owner_name
            )
            self._rebuild(spec.type)
            return self._actions[spec.type]

    def replace_owner(self, specs: Iterable[ActionSpec], *, owner: str) -> None:
        specs = [replace(spec, owner=owner) for spec in specs]
        names = [spec.type for spec in specs]
        if len(names) != len(set(names)):
            raise ValueError(f"Владелец {owner} объявил повторяющееся действие")
        for spec in specs:
            self._validate_spec(spec)
        with self._lock:
            old = []
            for records in self._records.values():
                if owner in records:
                    old.append(records[owner].type)
                    del records[owner]
            for action_type in old:
                self._rebuild(action_type)
            try:
                for spec in specs:
                    existing = self._records.get(spec.type, {})
                    if any(
                        other.data_schema != spec.data_schema
                        for other in existing.values()
                    ):
                        raise ValueError(f"Разные схемы для действия {spec.type}")
                    self._records.setdefault(spec.type, {})[owner] = spec
                    self._rebuild(spec.type)
            except Exception:
                for spec in specs:
                    self._records.get(spec.type, {}).pop(owner, None)
                    self._rebuild(spec.type)
                raise

    def unregister_owner(self, owner: str) -> None:
        with self._lock:
            affected = []
            for action_type, records in self._records.items():
                if owner in records:
                    del records[owner]
                    affected.append(action_type)
            for action_type in affected:
                self._rebuild(action_type)

    def _rebuild(self, action_type: str) -> None:
        records = self._records.get(action_type, {})
        if not records:
            self._records.pop(action_type, None)
            self._actions.pop(action_type, None)
            return
        values = list(records.values())
        base = values[0]
        audiences: set[str] = set()
        unrestricted = False
        for spec in values:
            if spec.data_schema != base.data_schema:
                raise ValueError(f"Разные схемы для действия {action_type}")
            if spec.audiences is None:
                unrestricted = True
            else:
                audiences.update(spec.audiences)
        self._actions[action_type] = replace(
            base,
            audiences=None if unrestricted else frozenset(audiences),
            owner="|".join(sorted(records)),
        )

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
        def visible(spec: ActionSpec) -> bool:
            if spec.audiences is None or audience in spec.audiences:
                return True
            return "subagent" in spec.audiences and audience not in {"main"}

        with self._lock:
            return {
                name: spec
                for name, spec in self._actions.items()
                if (allowed is None or name in allowed)
                and visible(spec)
            }

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
    """Схемы событий, общие для всех областей модулей."""

    def __init__(self):
        self._lock = threading.RLock()
        self._records: dict[str, dict[str, EventDefinition]] = {}
        self._events: dict[str, EventDefinition] = {}

    @staticmethod
    def _validate_spec(spec: EventDefinition) -> None:
        if not spec.type:
            raise ValueError("У события должен быть непустой type")
        if spec.data_schema.get("type") != "object":
            raise ValueError(f"Схема события {spec.type} должна быть объектом")
        validate_strict_schema(spec.data_schema, where=f"схема события {spec.type}")

    def register(self, spec: EventDefinition, *, owner: str = "builtin") -> None:
        self._validate_spec(spec)
        with self._lock:
            existing = self._records.get(spec.type, {})
            if any(item.data_schema != spec.data_schema for item in existing.values()):
                raise ValueError(f"Разные схемы для события {spec.type}")
            self._records.setdefault(spec.type, {})[owner] = spec
            self._rebuild(spec.type)

    def replace_owner(self, specs: Iterable[EventDefinition], *, owner: str) -> None:
        specs = list(specs)
        names = [spec.type for spec in specs]
        if len(names) != len(set(names)):
            raise ValueError(f"Владелец {owner} объявил повторяющееся событие")
        for spec in specs:
            self._validate_spec(spec)
        with self._lock:
            old = []
            for records in self._records.values():
                if owner in records:
                    old.append(records[owner].type)
                    del records[owner]
            for event_type in old:
                self._rebuild(event_type)
            try:
                for spec in specs:
                    existing = self._records.get(spec.type, {})
                    if any(
                        item.data_schema != spec.data_schema
                        for item in existing.values()
                    ):
                        raise ValueError(f"Разные схемы для события {spec.type}")
                    self._records.setdefault(spec.type, {})[owner] = spec
                    self._rebuild(spec.type)
            except Exception:
                for spec in specs:
                    self._records.get(spec.type, {}).pop(owner, None)
                    self._rebuild(spec.type)
                raise

    def unregister_owner(self, owner: str) -> None:
        with self._lock:
            affected = []
            for event_type, records in self._records.items():
                if owner in records:
                    del records[owner]
                    affected.append(event_type)
            for event_type in affected:
                self._rebuild(event_type)

    def _rebuild(self, event_type: str) -> None:
        records = self._records.get(event_type, {})
        if not records:
            self._records.pop(event_type, None)
            self._events.pop(event_type, None)
            return
        self._events[event_type] = next(iter(records.values()))

    def get(self, event_type: str) -> EventDefinition | None:
        with self._lock:
            return self._events.get(event_type)

    def validate(self, event_type: str, data: dict[str, Any]) -> None:
        spec = self.get(event_type)
        if spec is None:
            raise ValueError(f"Событие не зарегистрировано: {event_type}")
        validate_json(data, spec.data_schema, where=f"данные события {event_type}")

    def all(self) -> dict[str, EventDefinition]:
        with self._lock:
            return dict(self._events)
