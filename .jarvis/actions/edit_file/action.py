from jarvis.capabilities import action_definition
from jarvis.capabilities.scope import scope_path
from jarvis.core.protocol import object_schema


def run(data, context):
    path = scope_path(context.capabilities.root, data["path"])
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    start, end = data["start_line"], data["end_line"]
    if start < 1 or end < start or end > len(lines):
        raise ValueError("Line range is outside the file")
    replacement = data["content"]
    if replacement and not replacement.endswith("\n"):
        replacement += "\n"
    lines[start - 1:end] = replacement.splitlines(keepends=True) if replacement else []
    path.write_text("".join(lines), encoding="utf-8")
    return {"path": str(path.relative_to(context.capabilities.root)), "edited": True}


def create_action():
    return action_definition(
        "Replace an inclusive 1-based line range in a UTF-8 text file.",
        object_schema(
            {
                "path": {"type": "string"},
                "start_line": {"type": "integer"},
                "end_line": {"type": "integer"},
                "content": {"type": "string"},
            }
        ),
        object_schema({"path": {"type": "string"}, "edited": {"type": "boolean"}}),
        run,
    )
