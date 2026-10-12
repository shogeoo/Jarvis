"""Main-only instance management and universal agent messaging."""

from dataclasses import replace
from ..capabilities.api import action_definition
from .protocol import object_schema

STRING = {"type": "string"}
OPEN = {"type": "object", "additionalProperties": True, "x-jarvis-open-object": True}


def require_main(context):
    if context.agent_id != "main":
        raise ValueError("Only main may manage agents and presets")
    return context.agent_manager._manager


def run(data, context):
    name = context.action_id
    if name == "send_message_to_agent":
        return context.agent_manager.deliver_message(
            sender_id=context.agent_id,
            target_id=data["agent_id"],
            text=data["text"],
            action_id=name,
            module_id=context.module_id,
        )
    manager = require_main(context)
    if name == "spawn_agent":
        created = manager.spawn(parent_id=context.agent_id, preset_id=data["preset_id"])
        return {
            "spawned": True,
            "agent_id": created["agent_id"],
            "preset_id": data["preset_id"],
            "parent_id": context.agent_id,
        }
    if name == "list_agents":
        return {"agents": manager.list_agents()}
    if name == "get_agent":
        return manager.get_agent(data["agent_id"])
    if name == "edit_agent":
        return manager.edit_agent(data["agent_id"], data["changes"])
    if name == "interrupt_agent":
        return manager.interrupt(
            agent_id=data["agent_id"], requester_id=context.agent_id
        )
    return manager.delete(agent_id=data["agent_id"])


def register_agent_actions(registry):
    descriptions = {
        "spawn_agent": "Create an independent agent from a preset. Send its task separately after the creation result. Delegate complex work; capability_manager may only develop capabilities.",
        "send_message_to_agent": "Send a message or task to another agent; every agent can communicate. The recipient receives message_from_agent.",
        "list_agents": "Inspect live agent instances before addressing or managing them.",
        "get_agent": "Inspect the current preset base, individual additions and computed effective configuration separately.",
        "edit_agent": "Change individual additions only: additional_instructions, capabilities_add, capabilities_remove and model (null clears the override). Mandatory preset capabilities cannot be removed. History is preserved.",
        "interrupt_agent": "Cancel the current generation; keep history, queued events and already running actions.",
        "delete_agent": "Delete an agent and all descendants, stop their executions and erase all instance data without late saves recreating it.",
    }
    schemas = {
        "spawn_agent": (
            {"preset_id": STRING},
            {"spawned": {}, "agent_id": {}, "preset_id": {}, "parent_id": {}},
        ),
        "send_message_to_agent": (
            {"agent_id": STRING, "text": STRING},
            {"delivered": {}, "agent_id": {}},
        ),
        "list_agents": ({}, {"agents": {}}),
        "get_agent": (
            {"agent_id": STRING},
            {
                key: {}
                for key in (
                    "agent_id",
                    "preset_id",
                    "parent_id",
                    "state",
                    "preset",
                    "additions",
                    "effective",
                )
            },
        ),
        "edit_agent": (
            {
                "agent_id": STRING,
                "changes": {
                    **OPEN,
                    "description": "Partial update: additional_instructions string, capabilities_add/remove arrays of IDs, model string or null. Omitted fields remain unchanged.",
                },
            },
            {"agent_id": {}, "updated": {}},
        ),
        "interrupt_agent": ({"agent_id": STRING}, {"agent_id": {}, "state": {}}),
        "delete_agent": ({"agent_id": STRING}, {"agent_id": {}, "deleted": {}}),
    }
    for name, (arguments, result) in schemas.items():
        registry.register(
            replace(
                action_definition(
                    descriptions[name],
                    object_schema(arguments),
                    object_schema(result),
                    run,
                ),
                id=name,
                owner="core" if name == "send_message_to_agent" else "core:primary",
            )
        )
