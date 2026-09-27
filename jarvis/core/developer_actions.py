"""Built-in development tools for the module_manager role."""

from dataclasses import replace
from pathlib import Path
from ..capabilities.api import action_definition
from .protocol import object_schema


def _path(value, context):
    path = Path(value).expanduser()
    root = (
        context.config.project_root
        if context.config is not None
        else context.capabilities.root.parent
    )
    return path if path.is_absolute() else root / path


def read_file(data, context):
    lines = _path(data["path"], context).read_text(encoding="utf-8").splitlines()
    start = data.get("start_line")
    start = 1 if start is None else start
    end = data.get("end_line")
    end = len(lines) if end is None else end
    if start < 1 or end < 0 or (end < start and lines):
        raise ValueError("Invalid inclusive 1-based line range")
    return {
        "content": "\n".join(
            f"{number}: {line}"
            for number, line in enumerate(lines, 1)
            if start <= number <= end
        )
    }


def write_file(data, context):
    path = _path(data["path"], context)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data["content"], encoding="utf-8")
    return {"written": True}


def edit_file(data, context):
    path = _path(data["path"], context)
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    start, end = data["start_line"], data["end_line"]
    if not 1 <= start <= end <= len(lines):
        raise ValueError("Invalid inclusive 1-based line range")
    content = data["content"]
    if content and end < len(lines) and not content.endswith("\n"):
        content += "\n"
    path.write_text(
        "".join(lines[: start - 1]) + content + "".join(lines[end:]), encoding="utf-8"
    )
    return {"edited": True}


def execute_command(data, context):
    import subprocess

    manager = context.agent_manager._manager
    if not data["command"].strip():
        raise ValueError("command must not be empty")
    agent = manager.require_agent(context.agent_id)
    if agent._stop.is_set():
        raise RuntimeError("Agent is stopped")
    cwd = _path(data.get("cwd") or ".", context)
    process = manager.processes.start(
        data["command"],
        shell=True,
        executable="/bin/bash",
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        owner=getattr(context, "metadata", {}).get("execution_owner")
        or manager.processes.reserve_call(context.agent_id, context.call_id),
    )
    try:
        stdout, stderr = process.communicate()
        return {"exit_code": process.returncode, "stdout": stdout, "stderr": stderr}
    finally:
        manager.processes.finish(process)


def register_developer_actions(registry):
    string = {"type": "string"}
    line = {"type": ["integer", "null"], "minimum": 1}
    definitions = {
        "read_file": (
            "Read a UTF-8 file with 1-based line numbers. Null start_line/end_line means the full file boundary.",
            {"path": string, "start_line": line, "end_line": line},
            {"content": string},
            read_file,
        ),
        "write_file": (
            "Create or fully replace a UTF-8 file, creating parent directories. No append.",
            {"path": string, "content": string},
            {"written": {"type": "boolean"}},
            write_file,
        ),
        "edit_file": (
            "Replace the inclusive 1-based line range with content. Invalid ranges fail without changing the file.",
            {
                "path": string,
                "start_line": {"type": "integer", "minimum": 1},
                "end_line": {"type": "integer", "minimum": 1},
                "content": string,
            },
            {"edited": {"type": "boolean"}},
            edit_file,
        ),
        "execute_command": (
            "Run a non-interactive bash command. Optional cwd is null or a directory. No execution timeout; returns exit_code/stdout/stderr on completion. Agent deletion cancels the command and its child processes.",
            {"command": string, "cwd": {"type": ["string", "null"]}},
            {"exit_code": {"type": "integer"}, "stdout": string, "stderr": string},
            execute_command,
        ),
    }
    for name, (description, arguments, result, run) in definitions.items():
        spec = action_definition(
            description, object_schema(arguments), object_schema(result), run
        )
        registry.register(replace(spec, id=name, owner="core:developer"))
