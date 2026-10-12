"""RPC facade for instance state and universal communication."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..core.protocol import Event

if TYPE_CHECKING:
    from .manager import CapabilityManager


class _AgentApi:
    """JSON-совместимый фасад над AgentManager для RPC из единиц."""

    def __init__(self, capabilities: CapabilityManager):
        self._capabilities = capabilities

    @property
    def _manager(self):
        manager = self._capabilities._manager()
        if manager is None:
            raise RuntimeError("Менеджер агентов не запущен")
        return manager

    def spawn(self, parent_id: str, preset_id: str) -> dict[str, Any]:
        return self._manager.spawn(parent_id=parent_id, preset_id=preset_id)

    def delete(self, agent_id: str) -> dict[str, Any]:
        return self._manager.delete(agent_id=agent_id)

    def interrupt(
        self, agent_id: str, requester_id: str | None = None
    ) -> dict[str, Any]:
        return self._manager.interrupt(agent_id=agent_id, requester_id=requester_id)

    def list_agents(self) -> list[dict[str, Any]]:
        return self._manager.list_agents()

    def agent_snapshot(self, agent_id: str) -> dict[str, list[str]]:
        snapshot = self._manager.require_agent(agent_id).capabilities_snapshot()
        return {key: sorted(value) for key, value in snapshot.items()}

    def known_snapshot(self, agent_id: str) -> dict[str, list[str]]:
        snapshot = self._manager.require_agent(agent_id).known_snapshot()
        return {key: sorted(value) for key, value in snapshot.items()}

    def toggle_capability(self, capability_id: str) -> dict[str, Any]:
        return self._capabilities.toggle_global_state(
            self._capabilities.resolve_kind(capability_id), capability_id
        )

    def deliver_message(
        self,
        sender_id: str,
        target_id: str,
        text: str,
        action_id: str,
        module_id: str | None,
    ) -> dict[str, Any]:
        manager = self._manager
        sender = manager.require_agent(sender_id)
        target = manager.require_agent(target_id)
        delivered = manager.bus.publish(
            Event(
                event_id="message_from_agent",
                data={
                    "from_agent_id": sender.agent_id,
                    "text": text,
                },
                source=f"action:{action_id}",
                target=target.agent_id,
                module_id=module_id,
            )
        )
        return {"delivered": delivered, "agent_id": target.agent_id}

    def enable(self, agent_id: str, capability_id: str):
        kind = self._capabilities.resolve_kind(capability_id)
        getattr(self._manager, "enable_" + kind)(agent_id, capability_id)
        return {"enabled": True, "capability_id": capability_id}

    def disable(self, agent_id: str, capability_id: str):
        kind = self._capabilities.resolve_kind(capability_id)
        getattr(self._manager, "disable_" + kind)(agent_id, capability_id)
        return {"enabled": False, "capability_id": capability_id}

    def get_agent(self, agent_id):
        return self._manager.get_agent(agent_id)

    def edit_agent(self, agent_id, changes):
        return self._manager.edit_agent(agent_id, changes)

    def presets_list(self):
        from ..core.preset_actions import get_preset

        return [get_preset(preset) for preset in self._manager.presets.list()]
