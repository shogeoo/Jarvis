"""Shared contracts for standalone file tools and built-in development tools."""

import base64
import mimetypes
import inspect
import subprocess
from dataclasses import replace
from pathlib import Path

from .api import PENDING, action_definition, input_part
from ..core.protocol import object_schema

TOOL_NAMES = ("read_file", "write_file", "edit_file", "execute_command")


def resolve_path(value, base):
    path = Path(value).expanduser()
    return (path if path.is_absolute() else Path(base) / path).resolve()


def read_file(data, context, base):
    path = resolve_path(data["path"], base)
    raw = path.read_bytes()
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif raw.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    elif raw.startswith((b"GIF87a", b"GIF89a")):
        mime = "image/gif"
    elif raw.startswith(b"%PDF-"):
        mime = "application/pdf"
    modality = mime.split("/", 1)[0]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = None
    if (
        text is None
        or "\0" in text
        or modality in {"image", "audio", "video"}
        or mime == "application/pdf"
    ):
        result = {
            "path": str(path),
            "start_line": None,
            "end_line": None,
            "content": "No text",
        }
        part = input_part(
            modality if modality in {"image", "audio", "video"} else "file",
            mime,
            base64.b64encode(raw).decode("ascii"),
            name=path.name,
        )
        context.complete(result, parts=(part,))
        return PENDING
    lines = text.splitlines()
    start = 1 if data.get("start_line") is None else data["start_line"]
    end = len(lines) if data.get("end_line") is None else data["end_line"]
    if start < 1 or end < 0 or (end < start and lines):
        raise ValueError("Invalid inclusive 1-based line range")
    end = min(end, len(lines))
    return {
        "path": str(path),
        "start_line": start,
        "end_line": end,
        "content": "\n".join(
            f"{number}: {line}"
            for number, line in enumerate(lines, 1)
            if start <= number <= end
        ),
    }


def write_file(data, context, base):
    path = resolve_path(data["path"], base)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data["content"], encoding="utf-8")
    return {"path": str(path), "written": True}


def edit_file(data, context, base):
    path = resolve_path(data["path"], base)
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
    return {"path": str(path), "edited": True}


def execute_command(data, context, base, processes=None):
    if not data["command"].strip():
        raise ValueError("command must not be empty")
    kwargs = dict(
        shell=True,
        executable="/bin/bash",
        cwd=resolve_path(data.get("cwd") or ".", base),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if processes is None:
        process = subprocess.Popen(data["command"], **kwargs)
    else:
        owner = getattr(context, "metadata", {}).get(
            "execution_owner"
        ) or processes.reserve_call(context.agent_id, context.call_id)
        process = processes.start(data["command"], owner=owner, **kwargs)
    try:
        stdout, stderr = process.communicate()
        return {"exit_code": process.returncode, "stdout": stdout, "stderr": stderr}
    finally:
        if processes is not None:
            processes.finish(process)


def file_action(action_id, base_directory, *, process_manager=None):
    """Only ID and base binding differ; arguments, results and behavior match."""
    name = action_id.rsplit(".", 1)[-1]
    string = {"type": "string"}
    line = {"type": ["integer", "null"], "minimum": 1}
    specs = {
        "read_file": (
            "Read numbered UTF-8 text or native image/audio/video/file attachments. Optional lines are inclusive and 1-based. Relative paths use this tool's base directory.",
            {"path": string, "start_line": line, "end_line": line},
            ["path"],
            {"path": string, "start_line": line, "end_line": line, "content": string},
        ),
        "write_file": (
            "Create or fully replace a UTF-8 file, creating parent directories. Relative paths use this tool's base directory.",
            {"path": string, "content": string},
            ["path", "content"],
            {"path": string, "written": {"type": "boolean"}},
        ),
        "edit_file": (
            "Replace an inclusive 1-based line range; invalid ranges fail without editing. Relative paths use this tool's base directory.",
            {
                "path": string,
                "start_line": {"type": "integer", "minimum": 1},
                "end_line": {"type": "integer", "minimum": 1},
                "content": string,
            },
            ["path", "start_line", "end_line", "content"],
            {"path": string, "edited": {"type": "boolean"}},
        ),
        "execute_command": (
            "Run one non-interactive bash command without a result timeout. Every invocation starts a fresh shell; cwd, exports and shell state never carry between calls. Omitted cwd uses this tool's base directory. Return full exit_code, stdout and stderr.",
            {"command": string, "cwd": {"type": ["string", "null"]}},
            ["command"],
            {"exit_code": {"type": "integer"}, "stdout": string, "stderr": string},
        ),
    }
    description, arguments, required, result = specs[name]

    def run(data, context):
        base = base_directory(context) if callable(base_directory) else base_directory
        if name == "execute_command":
            processes = (
                process_manager(context) if process_manager is not None else None
            )
            return execute_command(data, context, base, processes)
        return {
            "read_file": read_file,
            "write_file": write_file,
            "edit_file": edit_file,
        }[name](data, context, base)

    source = inspect.currentframe().f_back.f_code.co_filename
    return replace(
        action_definition(
            description,
            object_schema(arguments, required=required),
            object_schema(result),
            run,
        ),
        id=action_id,
        source=source,
    )
