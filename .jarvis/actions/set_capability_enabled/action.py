from jarvis.capabilities import action_definition
from jarvis.core.protocol import object_schema


def run(data, context):
    result = context.agent_manager.set_global_capability(
        kind=data["kind"], capability_id=data["id"], enabled=data["enabled"]
    )
    return {"kind": data["kind"], "id": data["id"], "enabled": data["enabled"], "affected": result}


def create_action():
    open_object = {"type": "object", "x-jarvis-open-object": True, "additionalProperties": True}
    return action_definition(
        "Globally enable or disable a capability through CapabilityManager. For reload: disable it, wait for this action_result, edit and validate files, then enable it in a later model response. Never disable and enable the same capability in one batch.",
        object_schema(
            {
                "kind": {"type": "string", "enum": ["module", "action", "handler"]},
                "id": {"type": "string"},
                "enabled": {"type": "boolean"},
            }
        ),
        object_schema(
            {
                "kind": {"type": "string"},
                "id": {"type": "string"},
                "enabled": {"type": "boolean"},
                "affected": open_object,
            }
        ),
        run,
    )
