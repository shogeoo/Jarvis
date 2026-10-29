"""Indivisible agent management and universal agent messaging."""

from dataclasses import replace
from ..capabilities.api import action_definition
from .protocol import object_schema

STRING = {"type": "string"}
OPEN = {"type": "object", "additionalProperties": True, "x-jarvis-open-object": True}


def require_agent_management(context):
    manager = context.agent_manager._manager
    if "agents" not in manager.require_agent(context.agent_id).modules():
        raise ValueError("The agents capability is not assigned to this agent")
    return manager


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
    manager = require_agent_management(context)
    name = name.removeprefix("agents.")
    if name == "spawn":
        created = manager.spawn(
            parent_id=context.agent_id,
            preset_id=data["preset_id"],
            description=data.get("description"),
        )
        return {
            "spawned": True,
            "agent_id": created["agent_id"],
            "preset_id": data["preset_id"],
            "parent_id": context.agent_id,
        }
    if name == "list":
        return {"agents": manager.list_agents()}
    if name == "get":
        return manager.get_agent(data["agent_id"])
    if name == "edit":
        return manager.edit_agent(data["agent_id"], data["changes"])
    if name == "interrupt":
        return manager.interrupt(
            agent_id=data["agent_id"], requester_id=context.agent_id
        )
    return manager.delete(agent_id=data["agent_id"])


def register_agent_actions(registry):
    from .descriptions import read_descriptions

    catalog = read_descriptions()
    descriptions = {
        key.removeprefix("agents."): value["description"]
        for entry in catalog["modules"]["agents"]["actions"]
        for key, value in entry.items()
    }
    descriptions["send_message_to_agent"] = catalog["actions"]["send_message_to_agent"][
        "description"
    ]
    schemas = {
        "spawn": (
            {
                "preset_id": STRING,
                "description": {"type": ["string", "null"], "default": None},
            },
            {"spawned": {}, "agent_id": {}, "preset_id": {}, "parent_id": {}},
        ),
        "send_message_to_agent": (
            {"agent_id": STRING, "text": STRING},
            {"delivered": {}, "agent_id": {}},
        ),
        "list": ({}, {"agents": {}}),
        "get": (
            {"agent_id": STRING},
            {
                key: {}
                for key in (
                    "agent_id",
                    "preset_id",
                    "parent_id",
                    "state",
                    "description",
                    "person",
                    "additional_instructions",
                    "model",
                    "capabilities",
                )
            },
        ),
        "edit": (
            {
                "agent_id": STRING,
                "changes": {
                    **OPEN,
                    "description": "Partial update: description string or null, additional_instructions string, capabilities_add/remove arrays of IDs, model string or null. Omitted fields remain unchanged.",
                },
            },
            {"agent_id": {}, "updated": {}},
        ),
        "interrupt": ({"agent_id": STRING}, {"agent_id": {}, "state": {}}),
        "delete": ({"agent_id": STRING}, {"agent_id": {}, "deleted": {}}),
    }
    for name, (arguments, result) in schemas.items():
        registry.register(
            replace(
                action_definition(
                    descriptions[name],
                    object_schema(
                        arguments, required=["preset_id"] if name == "spawn" else None
                    ),
                    object_schema(result),
                    run,
                ),
                id=name if name == "send_message_to_agent" else "agents." + name,
                owner="core"
                if name == "send_message_to_agent"
                else "core:module:agents",
            )
        )
