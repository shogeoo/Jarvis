"""Потокобезопасные реестры действий и событий."""

from __future__ import annotations

import threading
from dataclasses import replace
from typing import Any, Iterable

from ..capabilities.api import ActionDefinition, EventDefinition
from .protocol import validate_json, validate_data_schema, validate_result_schema, validate_catalog_text


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
        for name, schema in (("аргументов", spec.data_schema),):
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
            validate_data_schema(schema, where=f"схема {name} действия {spec.id}")
        validate_result_schema(spec.result_schema)
        validate_catalog_text({"description": spec.description, "data_schema": spec.data_schema, "result_schema": spec.result_schema})
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
                spec.data_schema != base.data_schema
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
        self, *, modules: set[str], actions: set[str], primary: bool = False, developer: bool = False
    ) -> dict[str, ActionDefinition]:
        owners = {
            "core",
            *(f"module:{name}" for name in modules),
            *(f"action:{name}" for name in actions),
        }
        if primary:
            owners.add("core:speech")
            owners.add("core:primary")
            owners.add("core:reply")
        if developer:
            owners.add("core:developer")
        with self._lock:
            return {
                name: spec
                for name, spec in self._actions.items()
                if any(owner in owners for owner in spec.owner.split("|"))
                or (name in actions and spec.owner.startswith("core"))
            }

    def validate(self, action_id: str, data: dict[str, Any]) -> ActionDefinition:
        spec = self.require(action_id)
        validate_json(data, spec.data_schema, where=f"аргументы {action_id}")
        return spec

    @staticmethod
    def catalog(specs: dict[str, ActionDefinition]) -> list[dict[str, Any]]:
        return [
            {
                "action_id": spec.id,
                "description": spec.description,
                "data_schema": spec.data_schema,
                "result_schema": spec.result_schema,
            }
            for spec in specs.values()
        ]


class EventRegistry:
    """Схемы событий, общие для всех областей модулей."""

    def __init__(self):
        self._lock = threading.RLock()
        self._records: dict[str, dict[str, EventDefinition]] = {}
        self._events: dict[str, EventDefinition] = {}

    @staticmethod
    def _validate_spec(spec: EventDefinition) -> None:
        if not spec.event_id:
            raise ValueError("У события должен быть непустой type")
        if spec.data_schema.get("type") != "object":
            raise ValueError(f"Схема события {spec.event_id} должна быть объектом")
        validate_data_schema(spec.data_schema, where=f"схма события {spec.event_id}")
        validate_catalog_text({"description": spec.description, "data_schema": spec.data_schema})

    def register(self, spec: EventDefinition, *, owner: str = "builtin") -> None:
        self._validate_spec(spec)
        with self._lock:
            existing = self._records.get(spec.event_id, {})
            if any(item.data_schema != spec.data_schema for item in existing.values()):
                raise ValueError(f"Разные схемы для события {spec.event_id}")
            self._records.setdefault(spec.event_id, {})[owner] = spec
            self._rebuild(spec.event_id)

    def replace_owner(self, specs: Iterable[EventDefinition], *, owner: str) -> None:
        specs = list(specs)
        names = [spec.event_id for spec in specs]
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
                    old.append(records[owner].event_id)
                    del records[owner]
            for event_id in old:
                self._rebuild(event_id)
            try:
                for spec in specs:
                    existing = self._records.get(spec.event_id, {})
                    if any(
                        item.data_schema != spec.data_schema
                        for item in existing.values()
                    ):
                        raise ValueError(f"Разные схемы для события {spec.event_id}")
                    self._records.setdefault(spec.event_id, {})[owner] = spec
                    self._rebuild(spec.event_id)
            except Exception:
                self._records = previous_records
                self._events = previous_events
                raise

    def unregister_owner(self, owner: str) -> None:
        with self._lock:
            affected = []
            for event_id, records in self._records.items():
                if owner in records:
                    del records[owner]
                    affected.append(event_id)
            for event_id in affected:
                self._rebuild(event_id)

    def _rebuild(self, event_id: str) -> None:
        records = self._records.get(event_id, {})
        if not records:
            self._records.pop(event_id, None)
            self._events.pop(event_id, None)
            return
        self._events[event_id] = next(iter(records.values()))

    def get(self, event_id: str) -> EventDefinition | None:
        with self._lock:
            return self._events.get(event_id)

    def validate(self, event_id: str, data: dict[str, Any]) -> None:
        spec = self.get(event_id)
        if spec is None:
            raise ValueError(f"Событие не зарегистрировано: {event_id}")
        validate_json(data, spec.data_schema, where=f"данные события {event_id}")

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
            owners.add("core:primary")
        with self._lock:
            return {
                event_id: definition
                for event_id, definition in self._events.items()
                if any(owner in owners for owner in self._records[event_id])
            }
