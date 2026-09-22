from jarvis.capabilities import action_definition
from jarvis.core.protocol import object_schema


def run(data, context):
    snapshot = context.agent_manager.agent_snapshot(context.agent_id)
    return {
        "modules": sorted(snapshot["modules"]),
        "actions": sorted(snapshot["actions"]),
        "handlers": sorted(snapshot["handlers"]),
    }


def create_action():
    string_list = {"type": "array", "items": {"type": "string"}}
    return action_definition(
        "Показать точный набор capabilities текущего живого экземпляра; "
        "preset и другие экземпляры не учитываются.",
        object_schema({}),
        object_schema(
            {
                "modules": string_list,
                "actions": string_list,
                "handlers": string_list,
            }
        ),
        run,
    )
