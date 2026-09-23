from jarvis.capabilities import action_definition
from jarvis.capabilities.scope import scope_path
from jarvis.core.protocol import object_schema


def run(data, context):
    path = scope_path(context.capabilities.root, data["path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data["content"], encoding="utf-8")
    return {"path": str(path.relative_to(context.capabilities.root)), "written": True}


def create_action():
    return action_definition(
        "Write a UTF-8 text file completely, creating parent directories when necessary.",
        object_schema({"path": {"type": "string"}, "content": {"type": "string"}}),
        object_schema({"path": {"type": "string"}, "written": {"type": "boolean"}}),
        run,
    )
