"""Capability lookup and assignment through global IDs, never public kind."""

from dataclasses import replace
from ..capabilities.api import action_definition
from .agent_actions import OPEN, STRING
from .protocol import object_schema


def run(data, context):
    manager = context.agent_manager._manager
    capabilities = context.capabilities
    name = context.action_id
    if name == "list_capabilities":
        if manager.require_agent(context.agent_id).preset != "capability_manager":
            raise ValueError("Only capability_manager may list all capabilities")
        return {
            "capabilities": [
                item
                for items in capabilities.list_existing().values()
                for item in items
            ]
        }
    if name == "list_assigned_capabilities":
        agent = manager.require_agent(context.agent_id)
        return {
            "capabilities": [
                {
                    "id": identifier,
                    "description": capabilities.capability_info(identifier)[
                        "description"
                    ],
                }
                for identifier in dict.fromkeys(manager.assigned_ids(agent))
            ]
        }
    if name == "list_available_capabilities":
        available = capabilities.list_available(
            manager.require_agent(context.agent_id).capabilities_snapshot()
        )
        return {
            "capabilities": [item for items in available.values() for item in items]
        }
    capability_id = data["capability_id"]
    kind = capabilities.resolve_kind(capability_id)
    if name == "capability_info":
        return {
            "capability_id": capability_id,
            "info": capabilities.capability_info(capability_id),
        }
    if name == "toggle_capability":
        if manager.require_agent(context.agent_id).preset != "capability_manager":
            raise ValueError("Only capability_manager may globally pause capabilities")
        if capabilities.is_system(capability_id):
            raise ValueError("Cannot pause a system capability")
        return {
            "capability_id": capability_id,
            **capabilities.toggle_global_state(kind, capability_id),
        }
    enabled = name == "enable_capability"
    if not enabled:
        if capabilities.is_system(capability_id):
            raise ValueError("Cannot disable a system capability")
        agent = manager.require_agent(context.agent_id)
        if agent.agent_id != "main" and capability_id in manager.presets.load(agent.preset).capabilities:
            raise ValueError(
                "Cannot remove a mandatory preset capability; edit the preset instead"
            )
    operation = getattr(manager, ("enable_" if enabled else "disable_") + kind)
    operation(context.agent_id, capability_id)
    return {"capability_id": capability_id, "enabled": enabled}


def register_capability_actions(registry):
    for name in (
        "list_capabilities",
        "list_assigned_capabilities",
        "list_available_capabilities",
        "capability_info",
        "toggle_capability",
        "enable_capability",
        "disable_capability",
    ):
        lists = {"capabilities": {}}
        if name.startswith("list_"):
            arguments, result = {}, lists
        elif name == "capability_info":
            arguments, result = (
                {"capability_id": STRING},
                {"capability_id": {}, "info": OPEN},
            )
        elif name == "toggle_capability":
            arguments, result = (
                {"capability_id": STRING},
                {"capability_id": {}, "state": {}, "affected_agent_ids": {}},
            )
        else:
            arguments, result = (
                {"capability_id": STRING},
                {"capability_id": {}, "enabled": {}},
            )
        from .descriptions import read_descriptions

        description = read_descriptions()["actions"][name]["description"]
        registry.register(
            replace(
                action_definition(
                    description, object_schema(arguments), object_schema(result), run
                ),
                id=name,
                owner="core:assigned",
            )
        )
