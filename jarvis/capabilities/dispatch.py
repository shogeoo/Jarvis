"""Загрузка, диспетчеризация и полная выгрузка capabilities.

Каждая единица (``actions/<id>``, ``handlers/<id>``, ``modules/<id>``) —
самодостаточный каталог со своим ``.venv``. Ядро не импортирует код единиц:
оно запускает unit-host из окружения единицы, получает JSON-каталог и
общается с ним по JSON Lines. Управляющие вызовы к живому состоянию ядра
приходят от единиц отдельными RPC-сообщениями.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from typing import Any

from ..core.protocol import (
    ActionRequest,
    CallResult,
    arguments_with_defaults,
)
from ..infrastructure.config import DEFAULT_JARVIS_DIR
from .api import (
    PENDING,
    ActionContext,
    ActionDefinition,
)

DEFAULT_ROOT = DEFAULT_JARVIS_DIR




class CapabilityDispatch:
    # --- dispatch -----------------------------------------------------
    def dispatch(
        self, *, action: ActionRequest, spec: ActionDefinition, agent: Any
    ) -> None:
        action = replace(
            action, data=arguments_with_defaults(action.data, spec.data_schema)
        )
        if not spec.owner.startswith("core") and not agent.is_enabled_action(
            action.action_id
        ):
            self._manager().deliver_result(
                CallResult(
                    call_id=action.call_id,
                    agent_id=agent.agent_id,
                    data={
                        "status": "disabled",
                        "info": "Action is currently disabled.",
                    },
                )
            )
            return
        agent_modules = agent.modules() if hasattr(agent, "modules") else set()
        if self.is_globally_paused("action", action.action_id) or any(
            self.is_globally_paused("module", module_id)
            and action.action_id in self.module_action_ids(module_id)
            for module_id in agent_modules
        ):
            self._manager().deliver_result(
                CallResult(
                    call_id=action.call_id,
                    agent_id=agent.agent_id,
                    data={"status": "paused", "info": "Capability is globally paused."},
                )
            )
            return
        with self._lock:
            runtime = self._actions.get(action.action_id)
            core = False
            if runtime is None:
                if not spec.owner.startswith("core"):
                    raise RuntimeError(f"Действие {action.action_id} выключено")
                core = True
            elif not runtime.host.started:
                raise RuntimeError(f"Действие {action.action_id} выключено")
            manager = self._manager()
            if manager is None:
                raise RuntimeError("Менеджер агентов не запущен")
            manager.results.begin(
                agent_id=agent.agent_id,
                call_id=action.call_id,
                action_id=action.action_id,
                result_schema=spec.result_schema,
            )
        if core:
            processes = getattr(manager, "processes", None)
            owner = (
                processes.reserve_call(agent.agent_id, action.call_id)
                if processes is not None
                else None
            )
            threading.Thread(
                target=self._run_core_action,
                args=(spec, action, agent, owner),
                name=f"jarvis-core-{action.action_id}",
                daemon=True,
            ).start()
            return
        with self._lock:
            host = runtime.host
            message = {
                "kind": "action",
                "agent_id": agent.agent_id,
                "action_id": action.action_id,
                "call_id": action.call_id,
                "data": dict(action.data),
                "metadata": {"preset_id": agent.preset},
            }
        try:
            self._send_host(host, message)
        except Exception:
            manager.results.discard(agent.agent_id, action.call_id)
            raise

    def _run_core_action(
        self, spec: ActionDefinition, action: ActionRequest, agent: Any, owner=None
    ) -> None:
        """Исполнить захардкоженное действие ядра в собственном потоке."""

        manager = self._manager()

        def current_caller():
            return (
                not hasattr(manager, "agents")
                or manager.agents.get(agent.agent_id) is agent
            )

        def complete(data, parts=()):
            if current_caller():
                return self._complete_action(
                    agent.agent_id, action.call_id, data, parts
                )
            return False

        context = ActionContext(
            agent_id=agent.agent_id,
            call_id=action.call_id,
            action_id=action.action_id,
            capability_id=action.action_id,
            module_id=None,
            complete=complete,
            config=self.config,
            agent_manager=self._agent_api,
            capabilities=self,
            services=self.services,
            metadata={
                "preset": agent.preset,
                "execution_owner": owner,
            },
        )
        if not current_caller():
            return
        try:
            result = spec.run(dict(action.data), context)
        except Exception as exc:  # noqa: BLE001
            if current_caller():
                manager.fail_call(agent.agent_id, action.call_id, exc)
            return
        if result is PENDING:
            return
        complete(result)

    def _manager(self) -> Any:
        return getattr(self.event_bus, "manager", None)

    def model_input_modalities(self) -> list[str]:
        """Read the configured model's input modalities through capability RPC."""
        manager = self._manager()
        if manager is None or manager.model_capabilities is None:
            raise RuntimeError("Model input modalities are unavailable")
        return list(manager.model_capabilities.input_modalities)

    def _complete_action(
        self,
        agent_id: str | None,
        call_id: str | None,
        data: Any,
        parts: Any = (),
    ) -> bool:
        manager = self._manager()
        if manager is None or agent_id is None or call_id is None:
            self.debug.log(
                "call_result_dropped",
                agent_id=agent_id,
                call_id=call_id,
                reason="no_manager",
            )
            return False
        if not isinstance(data, dict):
            self.debug.log(
                "call_result_rejected",
                agent_id=agent_id,
                call_id=call_id,
                reason="invalid_data",
            )
            manager.fail_call(agent_id, call_id, "Некорректная структура результата")
            return False
        return manager.results.complete(
            manager,
            agent_id=agent_id,
            call_id=call_id,
            data=data,
            parts=tuple(parts),
        )

    def _discard_pending(self, agent_id: str | None, call_id: str | None) -> None:
        manager = self._manager()
        if manager is None or agent_id is None or call_id is None:
            return
        manager.results.discard(agent_id, call_id)

    def _report_error(
        self, kind: str, capability_id: str, exc: Exception | str
    ) -> None:
        manager = self._manager()
        if manager is not None:
            manager.report_capability_error(kind, capability_id, exc)
        else:
            self.debug.log(
                "capability_error", kind=kind, id=capability_id, error=str(exc)
            )
