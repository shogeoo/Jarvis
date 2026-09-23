from jarvis.capabilities import action_definition
from jarvis.core.protocol import object_schema


def run(data, context):
    return {
        "kind": data["kind"],
        "id": data["id"],
        "info": context.capabilities.capability_info(kind=data["kind"], capability_id=data["id"]),
    }


def create_action():
    open_object = {"type": "object", "x-jarvis-open-object": True, "additionalProperties": True}
    return action_definition(
        "Return the complete capability catalog object used in an agent system prompt.",
        object_schema(
            {"kind": {"type": "string", "enum": ["module", "action", "handler"]}, "id": {"type": "string"}}
        ),
        object_schema({"kind": {"type": "string"}, "id": {"type": "string"}, "info": open_object}),
        run,
    )
