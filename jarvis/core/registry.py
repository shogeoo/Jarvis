"""Потокобезопасные реестры действий и событий."""

from __future__ import annotations

import threading
from dataclasses import replace
from typing import Any, Iterable

from ..capabilities.api import ActionDefinition, EventDefinition
from .protocol import validate_json, validate_strict_schema


class ActionRegistry:
    """Актуальный набор действий с поддержкой нескольких областей."""

    def __init__(self):
        self._lock = threading.RLock()
        self._records: dict[str, dict[str, ActionDefinition]] = {}
        self._actions: dict[str, ActionDefinition] = {}

    @staticmethod
    def _validate_spec(spec: ActionDefinition) -> None:
        if not spec.id:
            raise ValueError("У действия должен быть непустой id")
        for name, schema in (
            ("аргументов", spec.args_schema),
            ("результата", spec.result_schema),
        ):
            if not isinstance(schema, dict):
                raise ValueError(f"Схема {name} действия {spec.id} должна быть объектом")
            is_object_schema = schema.get("type") == "object"
            is_object_union = (
                name == "аргументов"
                and isinstance(schema.get("anyOf"), list)
                and bool(schema["anyOf"])
                and all(
                    isinstance(variant, dict)
                    and variant.get("type") == "object"
                    for variant in schema["anyOf"]
                )
            )
            if not is_object_schema and not is_object_union:
                raise ValueError(f"Схема {name} действия {spec.id} должна быть объектом")
            validate_strict_schema(schema, where=f"схема {name} действия {spec.id}")
        if not callable(spec.run):
            raise ValueError(f"Некорректное действие {spec.id!r}")

    def register(
        self, spec: ActionDefinition, *, owner: str | None = None
    ) -> ActionDefinition:
        self._validate_spec(spec)
        owner_name = owner or spec.owner
        with self._lock:
            existing = self._records.get(spec.id, {})
            if existing and owner_name not in existing:
                raise ValueError(
                    f"Действие {spec.id} уже принадлежит другой capability"
                )
            self._records.setdefault(spec.id, {})[owner_name] = replace(
                spec, owner=owner_name
            )
            self._rebuild(spec.id)
            return self._actions[spec.id]

    def replace_owner(
        self, specs: Iterable[ActionDefinition], *, owner: str
    ) -> None:
        specs = [replace(spec, owner=owner) for spec in specs]
        names = [spec.id for spec in specs]
        if len(names) != len(set(names)):
            raise ValueError(f"Владелец {owner} объявил повторяющееся действие")
        for spec in specs:
            self._validate_spec(spec)
        with self._lock:
            previous_records = {
                name: dict(records) for name, records in self._records.items()
            }
            previous_actions = dict(self._actions)
            old = []
            for records in self._records.values():
                if owner in records:
                    old.append(records[owner].id)
                    del records[owner]
            for action_id in old:
                self._rebuild(action_id)
            try:
                for spec in specs:
                    existing = self._records.get(spec.id, {})
                    if existing:
                        raise ValueError(
                            f"Действие {spec.id} уже принадлежит другой capability"
                        )
                    self._records.setdefault(spec.id, {})[owner] = spec
                    self._rebuild(spec.id)
            except Exception:
                self._records = previous_records
                self._actions = previous_actions
                raise

    def unregister_owner(self, owner: str) -> None:
        with self._lock:
            affected = []
            for action_id, records in self._records.items():
                if owner in records:
                    del records[owner]
                    affected.append(action_id)
            for action_id in affected:
                self._rebuild(action_id)

    def _rebuild(self, action_id: str) -> None:
        records = self._records.get(action_id, {})
        if not records:
            self._records.pop(action_id, None)
            self._actions.pop(action_id, None)
            return
        values = list(records.values())
        base = values[0]
        for spec in values:
            if (
                spec.args_schema != base.args_schema
                or spec.result_schema != base.result_schema
            ):
                raise ValueError(f"Разные схемы для действия {action_id}")
        self._actions[action_id] = replace(
            base,
            owner="|".join(sorted(records)),
        )

    def get(self, action_id: str) -> ActionDefinition | None:
        with self._lock:
            return self._actions.get(action_id)

    def require(self, action_id: str) -> ActionDefinition:
        spec = self.get(action_id)
        if spec is None:
            raise KeyError(f"Действие не зарегистрировано: {action_id}")
        return spec

    def all(self) -> dict[str, ActionDefinition]:
        with self._lock:
            return dict(self._actions)

    def for_capabilities(
        self, *, modules: set[str], actions: set[str], primary: bool = False
    ) -> dict[str, ActionDefinition]:
        owners = {
            "core",
            *(f"module:{name}" for name in modules),
            *(f"action:{name}" for name in actions),
        }
        if primary:
            owners.add("core:speech")
            owners.add("core:primary")
        with self._lock:
            return {
                name: spec
                for name, spec in self._actions.items()
                if any(owner in owners for owner in spec.owner.split("|"))
            }

    def validate(self, action_id: str, data: dict[str, Any]) -> ActionDefinition:
        spec = self.require(action_id)
        validate_json(data, spec.args_schema, where=f"аргументы {action_id}")
        return spec

    @staticmethod
    def catalog(specs: dict[str, ActionDefinition]) -> list[dict[str, Any]]:
        return [
            {
                "type": spec.id,
                "description": spec.description,
                "args_schema": spec.args_schema,
                "result_schema": spec.result_schema,
            }
            for spec in sorted(specs.values(), key=lambda item: item.id)
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
            previous_records = {
                name: dict(records) for name, records in self._records.items()
            }
            previous_events = dict(self._events)
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
                self._records = previous_records
                self._events = previous_events
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

    def for_capabilities(
        self, *, modules: set[str], handlers: set[str], primary: bool = False
    ) -> dict[str, EventDefinition]:
        owners = {
            "core",
            *(f"module:{name}" for name in modules),
            *(f"handler:{name}" for name in handlers),
        }
        if primary:
            owners.add("core:speech")
        with self._lock:
            return {
                event_type: definition
                for event_type, definition in self._events.items()
                if any(owner in owners for owner in self._records[event_type])
            }
