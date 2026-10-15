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
    ActionDefinition,
    HandlerDefinition,
    ModuleDefinition,
)
from .hosts import LoadedModule, UnitHost


class CapabilityCatalog:
    # --- сводки и каталог --------------------------------------------
    def catalog(
        self, snapshot: dict[str, set[str]], *, describe_unloaded: bool = False
    ) -> dict[str, Any]:
        with self._lock:
            actions = []
            for action_id in sorted(snapshot.get("actions", ())):
                runtime = self._actions.get(action_id)
                if runtime is None and describe_unloaded:
                    item = self._describe("action", action_id)["actions"][0]
                    actions.append(
                        self._action_summary(
                            self._proxy_action(
                                item, f"action:{action_id}", self.action_path(action_id)
                            ),
                            self.action_path(action_id),
                        )
                    )
                    continue
                if runtime is None or runtime.module_id is not None:
                    continue
                actions.append(self._action_summary(runtime.definition, runtime.path))
            handlers = []
            events = []
            for handler_id in sorted(snapshot.get("handlers", ())):
                if handler_id in self.builtin_handlers():
                    events.append(self.capability_info(handler_id))
                    continue
                runtime = self._handlers.get(handler_id)
                if runtime is None and describe_unloaded:
                    item = self._describe("handler", handler_id)["handlers"][0]
                    definition = self._proxy_handler(
                        item, f"handler:{handler_id}", self.handler_path(handler_id)
                    )
                    handlers.append(
                        self._handler_summary(definition, self.handler_path(handler_id))
                    )
                    event = definition.event
                    events.append(
                        {
                            "event_id": event.event_id,
                            "description": event.description,
                            "data_schema": event.data_schema,
                        }
                    )
                    continue
                if runtime is None or runtime.module_id is not None:
                    continue
                handlers.append(self._handler_summary(runtime.definition, runtime.path))
                event = runtime.definition.event
                events.append(
                    {
                        "event_id": event.event_id,
                        "description": event.description,
                        "data_schema": event.data_schema,
                    }
                )
            modules = []
            for module_id in snapshot.get("modules", ()):
                if module_id in self.builtin_modules():
                    modules.append(self.builtin_module_catalog(module_id))
                    continue
                runtime = self._modules.get(module_id)
                if runtime is None:
                    if not describe_unloaded:
                        continue
                    catalog = self._describe("module", module_id)
                    modules.append(
                        self._module_summary(
                            LoadedModule(
                                module_id=module_id,
                                definition=ModuleDefinition(
                                    catalog["description"], (), ()
                                ),
                                path=self.module_path(module_id),
                                host=UnitHost(
                                    f"module:{module_id}",
                                    "module",
                                    module_id,
                                    module_id,
                                    self.module_path(module_id),
                                    catalog,
                                ),
                            )
                        )
                    )
                    continue
                modules.append(self._module_summary(runtime))
            return {
                "storage_root": str(self.root.resolve()),
                "actions": actions,
                "handlers": handlers,
                "events": events,
                "modules": modules,
            }

    @staticmethod
    def _action_summary(definition: ActionDefinition, path: Path) -> dict[str, Any]:
        return {
            "action_id": definition.id,
            "description": definition.description,
            "data_schema": definition.data_schema,
            "result_schema": definition.result_schema,
        }

    @staticmethod
    def _handler_summary(definition: HandlerDefinition, path: Path) -> dict[str, Any]:
        return {
            "id": definition.id,
            "description": definition.description,
            "path": str(path),
            "events": [
                {
                    "event_id": definition.event.event_id,
                    "description": definition.event.description,
                    "data_schema": definition.event.data_schema,
                }
            ],
        }

    def _module_summary(self, runtime: LoadedModule) -> dict[str, Any]:
        catalog = runtime.host.catalog
        return {
            "module_id": runtime.module_id,
            "description": runtime.definition.description,
            "actions": [
                {
                    "action_id": item["id"],
                    "description": item["description"],
                    "data_schema": item["data_schema"],
                    "result_schema": item["result_schema"],
                }
                for item in catalog["actions"]
            ],
            "events": [
                {
                    "event_id": item["event"]["event_id"],
                    "description": item["event"]["description"],
                    "data_schema": item["event"]["data_schema"],
                }
                for item in catalog["handlers"]
            ],
        }
