"""Загрузка, диспетчеризация и полная выгрузка capabilities.

Каждая единица (``actions/<id>``, ``handlers/<id>``, ``modules/<id>``) —
самодостаточный каталог со своим ``.venv``. Ядро не импортирует код единиц:
оно запускает unit-host из окружения единицы, получает JSON-каталог и
общается с ним по JSON Lines. Управляющие вызовы к живому состоянию ядра
приходят от единиц отдельными RPC-сообщениями.
"""

from __future__ import annotations

import json
import os
from typing import Any


class CapabilityState:
    def is_globally_paused(self, kind: str, capability_id: str) -> bool:
        key = {"module": "modules", "action": "actions", "handler": "handlers"}.get(
            kind
        )
        if key is None:
            raise ValueError(f"Неизвестный вид capability: {kind!r}")
        with self._lock:
            return capability_id in self._global_paused[key]

    def global_paused(self) -> dict[str, set[str]]:
        with self._lock:
            return {key: set(value) for key, value in self._global_paused.items()}

    def runnable_snapshot(self, snapshot: dict[str, set[str]]) -> dict[str, set[str]]:
        paused = self.global_paused()
        system_actions = {
            name
            for name, spec in self.actions.all().items()
            if spec.owner.startswith("core")
        }
        return {
            "modules": set(snapshot.get("modules", ())) - paused["modules"],
            "actions": set(snapshot.get("actions", ()))
            - paused["actions"]
            - system_actions,
            "handlers": set(snapshot.get("handlers", ())) - paused["handlers"],
        }

    def _save_global_state(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self._global_state_path.with_suffix(".tmp")
        state = {
            "paused": {key: sorted(value) for key, value in self._global_paused.items()}
        }
        temporary.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(temporary, self._global_state_path)

    def toggle_global_state(self, kind: str, capability_id: str) -> dict[str, Any]:
        with self._global_toggle_lock:
            return self._toggle_global_state_locked(kind, capability_id)

    def _toggle_global_state_locked(
        self, kind: str, capability_id: str
    ) -> dict[str, Any]:
        if self._global_state_error is not None:
            raise ValueError(
                "Cannot update unreadable capability state: " + self._global_state_error
            )
        kind_to_key = {"module": "modules", "action": "actions", "handler": "handlers"}
        key = kind_to_key.get(kind)
        if key is None:
            raise ValueError(f"Неизвестный вид capability: {kind!r}")
        self.validate(capability_id)
        manager = self._manager()
        if manager is None:
            raise RuntimeError("Менеджер агентов не запущен")
        affected = manager.agents_assigned_and_enabled(kind, capability_id)
        agent_ids = [agent.agent_id for agent in affected]
        if not self.is_globally_paused(kind, capability_id):
            with self._lock:
                self._global_paused[key].add(capability_id)
                self._save_global_state()
            if kind == "module":
                action_ids = self.module_action_ids(capability_id)
            elif kind == "action":
                action_ids = {capability_id}
            else:
                action_ids = set()
            if action_ids:
                for agent in manager.agents_snapshot():
                    for pending in manager.results.for_capability(
                        action_ids, {agent.agent_id}
                    ):
                        manager.pause_call(agent.agent_id, pending.call_id)
            if kind == "module":
                self.unload_module(capability_id)
            elif kind == "action":
                self.unload_action(capability_id)
            else:
                self.unload_handler(capability_id)
            return {"state": "paused", "affected_agent_ids": agent_ids}

        active_snapshots = [agent.capabilities_snapshot() for agent in affected]
        combined = {
            key_name: set().union(
                *(snapshot[key_name] for snapshot in active_snapshots)
            )
            if active_snapshots
            else set()
            for key_name in ("modules", "actions", "handlers")
        }
        with self._lock:
            self._global_paused[key].discard(capability_id)
            self._save_global_state()
        try:
            runnable = self.runnable_snapshot(combined)
            self.load_snapshot(runnable, start_handlers=True)
        except Exception:
            with self._lock:
                self._global_paused[key].add(capability_id)
                self._save_global_state()
            raise
        return {"state": "running", "affected_agent_ids": agent_ids}
