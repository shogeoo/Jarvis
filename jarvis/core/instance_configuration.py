"""Неизменяемое event-thinking-action ядро Jarvis."""

from __future__ import annotations

from typing import Any

from ..infrastructure.model_capabilities import ModelCapabilities

PRIMARY_AGENT_NAME = "Jarvis"
from .agent import Agent


class InstanceConfiguration:
    def default_model(self):
        if self.config is not None and self.config.subscription_enabled:
            return self.config.subscription_model
        return self.model

    def capabilities_for_model(self, model):
        if (
            self.config is not None
            and self.config.subscription_enabled
            and "/" not in model
        ):
            return ModelCapabilities(model, ("text", "image"))
        modalities = (
            self.model_capabilities.input_modalities
            if self.model_capabilities is not None
            else ("text", "image", "audio", "video", "file")
        )
        return ModelCapabilities(model, modalities)

    def refresh_presets(self, preset_id):
        for agent in self.agents_snapshot():
            if agent.preset == preset_id:
                agent._refresh_configuration()


    @staticmethod
    def describe(agent: Agent) -> dict[str, Any]:
        return {
            "agent_id": agent.agent_id,
            "preset_id": agent.preset,
            "parent_id": agent.parent_id,
            "state": agent.state,
            "capabilities": [
                value
                for values in agent.capabilities_snapshot_lists().values()
                for value in values
            ],
        }

    def list_agents(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                self.describe(agent)
                for agent in sorted(
                    self.agents.values(), key=lambda item: item.agent_id
                )
            ]

    def get_agent(self, agent_id):
        agent = self.require_agent(agent_id)
        agent._refresh_configuration()
        preset = self.presets.load(agent.preset)
        return {
            "agent_id": agent_id,
            "preset_id": agent.preset,
            "parent_id": agent.parent_id,
            "state": agent.state,
            "preset": {
                "person": preset.person_prompt,
                "model": preset.model,
                "capabilities": list(preset.capabilities),
            },
            "additions": {
                "additional_instructions": agent.additional_instructions,
                "model": agent.model_override,
                "capabilities": list(agent._additional_capabilities),
            },
            "effective": {
                "person": preset.person_prompt,
                "additional_instructions": agent.additional_instructions,
                "model": agent.effective_model(),
                "capabilities": list(
                    dict.fromkeys(
                        [*preset.capabilities, *agent._additional_capabilities]
                    )
                ),
            },
        }

    def edit_agent(self, agent_id, changes):
        agent = self.require_agent(agent_id)
        if not isinstance(changes, dict) or set(changes) - {
            "model",
            "additional_instructions",
            "capabilities_add",
            "capabilities_remove",
        }:
            raise ValueError("Invalid individual changes")
        additions = list(agent._additional_capabilities)
        added = self.presets._normalize(changes.get("capabilities_add", []))
        removed = self.presets._normalize(changes.get("capabilities_remove", []))
        self.capabilities.partition(added)
        if set(removed) & set(self.presets.load(agent.preset).capabilities):
            raise ValueError("Cannot remove a mandatory preset capability")
        instructions = changes.get(
            "additional_instructions", agent.additional_instructions
        )
        model = changes.get("model", agent.model_override)
        if not isinstance(instructions, str) or (
            model is not None and (not isinstance(model, str) or not model.strip())
        ):
            raise ValueError("Invalid instructions or model override")
        additions = list(dict.fromkeys([*additions, *added]))
        additions = [value for value in additions if value not in removed]
        previous = agent._additional_capabilities
        agent._additional_capabilities = additions
        try:
            agent._refresh_configuration()
        except Exception:
            agent._additional_capabilities = previous
            raise
        agent.additional_instructions, agent.model_override = instructions, model
        self.persist_agent(agent)
        return {"agent_id": agent_id, "updated": True}


    def enable_module(self, agent_id: str, module_id: str) -> dict[str, Any]:
        agent = self.require_agent(agent_id)
        if self.capabilities.is_globally_paused("module", module_id):
            agent.enable_module(module_id)
            return {
                "agent_id": agent_id,
                "module_id": module_id,
                "enabled": True,
                "globally_paused": True,
            }
        self.capabilities.load_module(module_id, start_handlers=False)
        agent.enable_module(module_id)
        try:
            if module_id not in self.capabilities.builtin_modules():
                self.capabilities.start_module(module_id)
        except Exception:
            agent.disable_module(module_id)
            if not self.agents_with_module(module_id):
                self.capabilities.unload_module(module_id)
            raise
        return {"agent_id": agent_id, "module_id": module_id, "enabled": True}

    def disable_module(self, agent_id: str, module_id: str) -> dict[str, Any]:
        agent = self.require_agent(agent_id)
        if module_id in self.presets.load(agent.preset).capabilities:
            raise ValueError("Cannot remove a mandatory preset capability")
        for pending in self.results.for_capability(
            self.capabilities.module_action_ids(module_id), {agent_id}
        ):
            self.disable_call(agent_id, pending.call_id)
        agent.disable_module(module_id)
        if not self.agents_with_module(module_id):
            self.capabilities.unload_module(module_id)
        return {"agent_id": agent_id, "module_id": module_id, "enabled": False}

    def enable_action(self, agent_id: str, action_id: str) -> dict[str, Any]:
        agent = self.require_agent(agent_id)
        spec = self.actions.get(action_id)
        if spec is not None and spec.owner.startswith("core"):
            agent.enable_action(action_id)
            return {"agent_id": agent_id, "action_id": action_id, "enabled": True}
        if self.capabilities.is_globally_paused("action", action_id):
            agent.enable_action(action_id)
            return {
                "agent_id": agent_id,
                "action_id": action_id,
                "enabled": True,
                "globally_paused": True,
            }
        self.capabilities.load_action(action_id, start_handlers=False)
        agent.enable_action(action_id)
        try:
            self.capabilities.start_action(action_id)
        except Exception:
            agent.disable_action(action_id)
            if not self.agents_with_action(action_id):
                self.capabilities.unload_action(action_id)
            raise
        return {"agent_id": agent_id, "action_id": action_id, "enabled": True}

    def disable_action(self, agent_id: str, action_id: str) -> dict[str, Any]:
        agent = self.require_agent(agent_id)
        if action_id in self.presets.load(agent.preset).capabilities:
            raise ValueError("Cannot remove a mandatory preset capability")
        for pending in self.results.for_capability({action_id}, {agent_id}):
            self.disable_call(agent_id, pending.call_id)
        agent.disable_action(action_id)
        if not self.agents_with_action(action_id):
            self.capabilities.unload_action(action_id)
        return {"agent_id": agent_id, "action_id": action_id, "enabled": False}

    def enable_handler(self, agent_id: str, handler_id: str) -> dict[str, Any]:
        agent = self.require_agent(agent_id)
        if self.capabilities.is_globally_paused("handler", handler_id):
            agent.enable_handler(handler_id)
            return {
                "agent_id": agent_id,
                "handler_id": handler_id,
                "enabled": True,
                "globally_paused": True,
            }
        self.capabilities.load_handler(handler_id, start_handlers=False)
        agent.enable_handler(handler_id)
        try:
            self.capabilities.start_handler(handler_id)
        except Exception:
            agent.disable_handler(handler_id)
            if not self.agents_with_handler(handler_id):
                self.capabilities.unload_handler(handler_id)
            raise
        return {"agent_id": agent_id, "handler_id": handler_id, "enabled": True}

    def disable_handler(self, agent_id: str, handler_id: str) -> dict[str, Any]:
        agent = self.require_agent(agent_id)
        agent.disable_handler(handler_id)
        if not self.agents_with_handler(handler_id):
            self.capabilities.unload_handler(handler_id)
        return {"agent_id": agent_id, "handler_id": handler_id, "enabled": False}

    def agents_assigned_and_enabled(self, kind: str, capability_id: str) -> list[Agent]:
        result = []
        for agent in self.agents_snapshot():
            snapshot = agent.capabilities_snapshot()
            if capability_id in snapshot.get(
                {"module": "modules", "action": "actions", "handler": "handlers"}[kind],
                set(),
            ):
                result.append(agent)
        return result

    def toggle_capability(self, kind: str, capability_id: str) -> dict[str, Any]:
        return self.capabilities.toggle_global_state(
            kind=kind, capability_id=capability_id
        )
