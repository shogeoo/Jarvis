from pathlib import Path

from jarvis.capabilities import action_definition
from jarvis.capabilities.scope import scope_path
from jarvis.core.protocol import object_schema


def run(data, context):
    path = scope_path(context.capabilities.root, data["path"])
    lines = path.read_text(encoding="utf-8").splitlines()
    start = data.get("start_line") or 1
    end = data.get("end_line") or len(lines)
    if not lines and "start_line" not in data and "end_line" not in data:
        return {"path": str(path.relative_to(context.capabilities.root)), "start_line": 1, "end_line": 0, "content": ""}
    if start < 1 or end < start or start > len(lines) + 1 or end > len(lines):
        raise ValueError("Line range is outside the file")
    return {
        "path": str(path.relative_to(context.capabilities.root)),
        "start_line": start,
        "end_line": end,
        "content": "\n".join(f"{index}: {lines[index - 1]}" for index in range(start, end + 1)),
    }


def create_action():
    integer = {"type": "integer"}
    return action_definition(
        "Read a UTF-8 text file. Returned content always includes 1-based line numbers for precise edit_file calls.",
        object_schema(
            {"path": {"type": "string"}, "start_line": {"type": ["integer", "null"]}, "end_line": {"type": ["integer", "null"]}},
        ),
        object_schema(
            {
                "path": {"type": "string"},
                "start_line": integer,
                "end_line": integer,
                "content": {"type": "string"},
            }
        ),
        run,
    )
