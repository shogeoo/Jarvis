"""Preset management within the agents system module."""

from dataclasses import replace
from ..capabilities.api import action_definition
from .agent_actions import require_agent_management, OPEN, STRING
from .protocol import object_schema


def run(data, context):
    manager = require_agent_management(context)
    name = context.action_id.removeprefix("agents.")
    if name == "list_presets":
        return {
            "presets": [
                {"id": preset.name, "description": preset.description}
                for preset in manager.presets.list()
            ]
        }
    preset_id = data["preset_id"]
    if name == "get_preset":
        return get_preset(manager.presets.load(preset_id))
    operation = name.split("_", 1)[0]
    success = {"create": "created", "edit": "edited", "delete": "deleted"}[operation]
    try:
        if operation == "delete":
            manager.remove_preset(preset_id)
        else:
            if operation == "create":
                capabilities = data.get("capabilities") or []
                manager.capabilities.partition(capabilities)
                manager.presets.create(
                    preset_id,
                    data["person"],
                    capabilities,
                    data.get("model"),
                    description=data["description"],
                )
            else:
                changes = data["changes"]
                if not isinstance(changes, dict):
                    raise ValueError("changes must be an object")
                for field in (
                    "capabilities",
                    "capabilities_add",
                    "capabilities_remove",
                ):
                    if field in changes:
                        manager.capabilities.partition(changes[field])
                manager.presets.edit(preset_id, changes=changes)
            manager.refresh_presets(preset_id)
    except Exception as exc:
        return {"status": "not_" + success, "preset_id": preset_id, "error": str(exc)}
    return {"status": success, "preset_id": preset_id, "error": None}


def get_preset(preset):
    return {
        "id": preset.name,
        "description": preset.description,
        "person": preset.person_prompt,
        "model": preset.model,
        "capabilities": list(preset.capabilities),
    }


def register_preset_actions(registry):
    ids = {"type": "array", "items": STRING, "default": []}
    definitions = {
        "list_presets": ({}, {"presets": {}}, None),
        "get_preset": (
            {"preset_id": STRING},
            {
                key: {}
                for key in ("id", "description", "person", "model", "capabilities")
            },
            None,
        ),
        "create_preset": (
            {
                "preset_id": STRING,
                "person": STRING,
                "description": STRING,
                "model": {"type": ["string", "null"]},
                "capabilities": ids,
            },
            {"status": {}, "preset_id": {}, "error": {}},
            ["preset_id", "person", "description"],
        ),
        "edit_preset": (
            {
                "preset_id": STRING,
                "changes": {
                    **OPEN,
                    "description": "Partial update: person, model (null inherits Jarvis default), capabilities or capabilities_add/remove. Omitted fields remain unchanged.",
                },
            },
            {"status": {}, "preset_id": {}, "error": {}},
            None,
        ),
        "delete_preset": (
            {"preset_id": STRING},
            {"status": {}, "preset_id": {}, "error": {}},
            None,
        ),
    }
    for name, (arguments, result, required) in definitions.items():
        from .descriptions import read_descriptions

        description = next(
            entry["agents." + name]["description"]
            for entry in read_descriptions()["modules"]["agents"]["actions"]
            if "agents." + name in entry
        )
        registry.register(
            replace(
                action_definition(
                    description,
                    object_schema(arguments, required=required),
                    object_schema(result),
                    run,
                ),
                id="agents." + name,
                owner="core:module:agents",
            )
        )
