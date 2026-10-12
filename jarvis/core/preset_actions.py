"""Main-only creation and live partial updates of preset bases."""

from dataclasses import replace
from ..capabilities.api import action_definition
from .agent_actions import require_main, OPEN, STRING
from .protocol import object_schema


def run(data, context):
    manager = require_main(context)
    name = context.action_id
    if name == "list_agent_presets":
        return {"presets": [get_preset(preset) for preset in manager.presets.list()]}
    preset_id = data["preset_id"]
    if name == "get_preset":
        return get_preset(manager.presets.load(preset_id))
    operation = name.split("_", 1)[0]
    success = {"create": "created", "edit": "edited", "remove": "removed"}[operation]
    try:
        if operation == "remove":
            manager.remove_preset(preset_id)
        else:
            if operation == "create":
                capabilities = data.get("capabilities") or []
                manager.capabilities.partition(capabilities)
                manager.presets.create(preset_id, data["person"], capabilities, data.get("model"))
            else:
                changes = data["changes"]
                if not isinstance(changes, dict):
                    raise ValueError("changes must be an object")
                for field in ("capabilities", "capabilities_add", "capabilities_remove"):
                    if field in changes:
                        manager.capabilities.partition(changes[field])
                manager.presets.edit(preset_id, changes=changes)
            manager.refresh_presets(preset_id)
    except Exception as exc:
        return {"status": "not_" + success, "preset_id": preset_id, "error": str(exc)}
    return {"status": success, "preset_id": preset_id, "error": None}


def get_preset(preset):
    return {"preset_id": preset.name, "person": preset.person_prompt, "model": preset.model, "capabilities": list(preset.capabilities)}


def register_preset_actions(registry):
    ids = {"type": "array", "items": STRING, "default": []}
    definitions = {
        "list_agent_presets": ({}, {"presets": {}}, None),
        "get_preset": ({"preset_id": STRING}, {key: {} for key in ("preset_id", "person", "model", "capabilities")}, None),
        "create_preset": ({"preset_id": STRING, "person": STRING, "model": {"type": ["string", "null"]}, "capabilities": ids}, {"status": {}, "preset_id": {}, "error": {}}, ["preset_id", "person"]),
        "edit_preset": ({"preset_id": STRING, "changes": {**OPEN, "description": "Partial update: person, model (null inherits Jarvis default), capabilities or capabilities_add/remove. Omitted fields remain unchanged."}}, {"status": {}, "preset_id": {}, "error": {}}, None),
        "remove_preset": ({"preset_id": STRING}, {"status": {}, "preset_id": {}, "error": {}}, None),
    }
    for name, (arguments, result, required) in definitions.items():
        description = name + ": manage the live mandatory preset base. Updates apply to existing instances without clearing history; removal recursively deletes all instances and descendants. Only main may perform this operation."
        registry.register(replace(action_definition(description, object_schema(arguments, required=required), object_schema(result), run), id=name, owner="core:primary"))
