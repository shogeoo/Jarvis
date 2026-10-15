"""Загрузка, диспетчеризация и полная выгрузка capabilities.

Каждая единица (``actions/<id>``, ``handlers/<id>``, ``modules/<id>``) —
самодостаточный каталог со своим ``.venv``. Ядро не импортирует код единиц:
оно запускает unit-host из окружения единицы, получает JSON-каталог и
общается с ним по JSON Lines. Управляющие вызовы к живому состоянию ядра
приходят от единиц отдельными RPC-сообщениями.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .api import (
    ModuleDefinition,
)
from .hosts import LoadedModule, UnitHost

_ENTRYPOINTS = {"action": "action.py", "handler": "handler.py", "module": "module.py"}
_DIRS = {"action": "actions", "handler": "handlers", "module": "modules"}
# Точка в ID корневой единицы запрещена: она означает единицу модуля.
from .identifiers import ID_PATTERN as _NAME


class CapabilityDiscovery:
    def is_system(self, capability_id):
        spec = self.actions.get(capability_id)
        return capability_id in self.builtin_modules() or capability_id in self.builtin_handlers() or (spec is not None and spec.owner.startswith("core"))

    def builtin_handlers(self):
        return {"speech_detected"} if self.events.get("speech_detected") is not None else set()

    # --- пути и обнаружение ------------------------------------------
    @staticmethod
    def validate_module_id(module_id: str) -> None:
        if not isinstance(module_id, str) or not _NAME.fullmatch(module_id):
            raise ValueError(f"Некорректный module_id: {module_id!r}")

    @staticmethod
    def validate_unit_id(unit_id: str, *, kind: str) -> None:
        if not isinstance(unit_id, str) or not _NAME.fullmatch(unit_id):
            raise ValueError(f"Некорректный {kind}: {unit_id!r}")

    def _unit_path(self, kind: str, unit_id: str) -> Path:
        if kind == "module":
            self.validate_module_id(unit_id)
            return self.modules_dir / unit_id
        self.validate_unit_id(unit_id, kind=kind)
        return (self.actions_dir if kind == "action" else self.handlers_dir) / unit_id

    def action_path(self, action_id: str) -> Path:
        return self._unit_path("action", action_id)

    def handler_path(self, handler_id: str) -> Path:
        return self._unit_path("handler", handler_id)

    def module_path(self, module_id: str) -> Path:
        return self._unit_path("module", module_id)

    @staticmethod
    def _rel(kind: str, unit_id: str) -> str:
        return f"capabilities/{_DIRS[kind]}/{unit_id}"

    def discover_modules(self) -> list[Path]:
        return self._discover_units(self.modules_dir, "module")

    def discover_actions(self) -> list[Path]:
        return self._discover_units(self.actions_dir, "action")

    def discover_handlers(self) -> list[Path]:
        return self._discover_units(self.handlers_dir, "handler")

    @staticmethod
    def _discover_units(directory: Path, kind: str) -> list[Path]:
        if not directory.exists():
            return []
        entry = _ENTRYPOINTS[kind]
        return sorted(
            path
            for path in directory.iterdir()
            if path.is_dir()
            and not path.name.startswith(".")
            and (path / entry).is_file()
        )

    def existing_modules(self) -> set[str]:
        return {path.name for path in self.discover_modules()} | self.builtin_modules()

    def builtin_modules(self) -> set[str]:
        return {
            spec.owner.removeprefix("core:module:")
            for spec in self.actions.all().values()
            if spec.owner.startswith("core:module:")
        }

    def builtin_module_catalog(self, module_id):
        from ..core.descriptions import apply_descriptions, read_descriptions

        actions, _ = apply_descriptions(
            {
                name: spec
                for name, spec in self.actions.all().items()
                if spec.owner == f"core:module:{module_id}"
            },
            {},
        )
        description = (
            read_descriptions()["modules"]
            .get(module_id, {})
            .get("description", "Develop capabilities.")
        )
        return {
            "module_id": module_id,
            "description": description,
            "actions": self.actions.catalog(actions),
            "events": [],
        }

    def resolve_kind(self, capability_id: str) -> str:
        self.validate_unit_id(capability_id, kind="capability")
        candidates = []
        if capability_id in self.builtin_modules():
            candidates.append("module")
        if capability_id in self.builtin_handlers():
            candidates.append("handler")
        spec = self.actions.get(capability_id)
        if spec is not None and spec.owner.startswith("core"):
            candidates.append("action")
        for kind in ("action", "handler", "module"):
            if (self._unit_path(kind, capability_id) / _ENTRYPOINTS[kind]).is_file():
                candidates.append(kind)
        if len(candidates) != 1:
            raise ValueError(
                f"Capability ID must identify exactly one unit: {capability_id!r}; found {candidates}"
            )
        return candidates[0]

    def partition(self, capability_ids):
        snapshot = {"modules": [], "actions": [], "handlers": []}
        keys = {"module": "modules", "action": "actions", "handler": "handlers"}
        for capability_id in capability_ids:
            snapshot[keys[self.resolve_kind(capability_id)]].append(capability_id)
        return snapshot

    def existing_actions(self) -> set[str]:
        return {path.name for path in self.discover_actions()}

    def existing_handlers(self) -> set[str]:
        return {path.name for path in self.discover_handlers()}

    def loaded_modules(self) -> set[str]:
        with self._lock:
            return set(self._modules) | self.builtin_modules()

    def loaded_actions(self) -> set[str]:
        with self._lock:
            return set(self._actions) | {name for name, spec in self.actions.all().items() if spec.owner.startswith("core") and "." not in name}

    def loaded_handlers(self) -> set[str]:
        with self._lock:
            return set(self._handlers) | self.builtin_handlers()

    def loaded_snapshot(self) -> dict[str, set[str]]:
        return {
            "modules": self.loaded_modules(),
            "actions": self.loaded_actions(),
            "handlers": self.loaded_handlers(),
        }

    def missing(self, snapshot: dict[str, set[str]]) -> set[str]:
        loaded = self.loaded_snapshot()
        missing: set[str] = set()
        for module_id in snapshot.get("modules", ()):
            if module_id not in loaded["modules"]:
                missing.add(f"module:{module_id}")
        for action_id in snapshot.get("actions", ()):
            if action_id not in loaded["actions"]:
                missing.add(f"action:{action_id}")
        for handler_id in snapshot.get("handlers", ()):
            if handler_id not in loaded["handlers"]:
                missing.add(f"handler:{handler_id}")
        return missing

    def list_existing(self) -> dict[str, Any]:
        loaded = self.loaded_snapshot()
        global_paused = self.global_paused()
        modules = [
            {
                "id": path.name,
                "description": self._describe("module", path.name)["description"],
                "loaded": path.name in loaded["modules"],
                "globally_running": path.name not in global_paused["modules"],
                "globally_paused": path.name in global_paused["modules"],
            }
            for path in self.discover_modules()
        ]
        actions = [
            {
                "id": path.name,
                "description": self._describe("action", path.name)["actions"][0][
                    "description"
                ],
                "loaded": path.name in loaded["actions"],
                "globally_running": path.name not in global_paused["actions"],
                "globally_paused": path.name in global_paused["actions"],
            }
            for path in self.discover_actions()
        ]
        handlers = [
            {
                "id": path.name,
                "description": self._describe("handler", path.name)["handlers"][0][
                    "description"
                ],
                "loaded": path.name in loaded["handlers"],
                "globally_running": path.name not in global_paused["handlers"],
                "globally_paused": path.name in global_paused["handlers"],
            }
            for path in self.discover_handlers()
        ]
        for identifier in sorted(self.builtin_modules()):
            modules.append({"id": identifier, "description": self.builtin_module_catalog(identifier)["description"], "loaded": True, "globally_running": True, "globally_paused": False})
        for identifier, spec in self.actions.all().items():
            if spec.owner.startswith("core") and "." not in identifier:
                actions.append({"id": identifier, "description": self.capability_info(identifier)["description"], "loaded": True, "globally_running": True, "globally_paused": False})
        for identifier in sorted(self.builtin_handlers()):
            handlers.append({"id": identifier, "description": self.capability_info(identifier)["description"], "loaded": True, "globally_running": True, "globally_paused": False})
        return {"modules": modules, "actions": actions, "handlers": handlers}

    def capability_info(self, capability_id: str) -> dict[str, Any]:
        kind = self.resolve_kind(capability_id)
        if capability_id in self.builtin_handlers():
            from ..core.descriptions import apply_descriptions

            _, events = apply_descriptions({}, {capability_id: self.events.get(capability_id)})
            spec = events[capability_id]
            return {"event_id": spec.event_id, "description": spec.description, "data_schema": spec.data_schema}
        if capability_id in self.builtin_modules():
            return self.builtin_module_catalog(capability_id)
        spec = self.actions.get(capability_id)
        if kind == "action" and spec is not None and spec.owner.startswith("core"):
            from ..core.descriptions import apply_descriptions

            actions, _ = apply_descriptions({capability_id: spec}, {})
            return self.actions.catalog(actions)[0]
        if kind == "module":
            catalog = self._describe("module", capability_id)
            return self._module_summary(
                LoadedModule(
                    capability_id,
                    ModuleDefinition(catalog["description"], (), ()),
                    self.module_path(capability_id),
                    UnitHost(
                        f"module:{capability_id}",
                        "module",
                        capability_id,
                        capability_id,
                        self.module_path(capability_id),
                        catalog,
                    ),
                )
            )
        if kind == "action":
            item = self._describe("action", capability_id)["actions"][0]
            return self._action_summary(
                self._proxy_action(
                    item, f"action:{capability_id}", self.action_path(capability_id)
                ),
                self.action_path(capability_id),
            )
        if kind == "handler":
            return self._describe("handler", capability_id)["handlers"][0]["event"]
        raise ValueError(f"Неизвестный вид capability: {kind!r}")

    def event_id_for_handler(self, handler_id: str) -> str:
        """Resolve a saved legacy handler envelope using its own declaration."""
        if handler_id in self.builtin_handlers():
            return handler_id
        with self._lock:
            loaded = self._handlers.get(handler_id)
            if loaded is not None:
                return loaded.definition.event.event_id
        if handler_id == "core:speech":
            return "speech_detected"
        if "." in handler_id:
            catalog = self._describe("module", handler_id.split(".", 1)[0])
        else:
            catalog = self._describe("handler", handler_id)
        return next(
            item["event"]["event_id"]
            for item in catalog["handlers"]
            if item["id"] == handler_id
        )

    def list_available(self, known: dict[str, set[str]]) -> dict[str, Any]:
        result = {"modules": [], "actions": [], "handlers": []}
        for kind, paths, key in (
            ("module", self.discover_modules(), "modules"),
            ("action", self.discover_actions(), "actions"),
            ("handler", self.discover_handlers(), "handlers"),
        ):
            for path in paths:
                if path.name in known[key]:
                    continue
                catalog = self._describe(kind, path.name)
                result[key].append(
                    {"id": path.name, "description": catalog["description"]}
                )
        return result

    def module_action_ids(self, module_id: str) -> set[str]:
        if module_id in self.builtin_modules():
            return {
                spec.id
                for spec in self.actions.all().values()
                if spec.owner == f"core:module:{module_id}"
            }
        with self._lock:
            runtime = self._modules.get(module_id)
        if runtime is not None:
            return set(runtime.action_ids)
        return {item["id"] for item in self._describe("module", module_id)["actions"]}

    def _loaded_description(self, kind: str, unit_id: str) -> str:
        with self._lock:
            if kind == "module":
                runtime = self._modules.get(unit_id)
                return runtime.host.catalog.get("description", "") if runtime else ""
            table = self._actions if kind == "action" else self._handlers
            loaded = table.get(unit_id)
            return loaded.definition.description if loaded else ""
