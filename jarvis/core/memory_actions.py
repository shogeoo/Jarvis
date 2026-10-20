"""The seven actions of the indivisible, read-only memory capability."""

from dataclasses import replace
import base64
from ..capabilities.api import action_definition, PENDING
from .protocol import object_schema, InputPart


def run(data, context):
    manager = context.agent_manager._manager
    if "memory" not in manager.require_agent(context.agent_id).modules():
        raise ValueError("The memory capability is not assigned to this agent")
    name = context.action_id.split(".", 1)[1]
    if name != "get_file":
        with manager.reranker.cancellation(context.stop_event):
            return getattr(manager.archive_reader, name)(**data)
    filename, native = manager.archive_reader.get_file(**data)
    kind = native["type"]
    inner = native[kind]
    if kind == "input_audio":
        mime, payload, modality = "audio/" + inner["format"], inner["data"], "audio"
    else:
        url = inner.get("url") or inner["file_data"]
        header, payload = url.split(",", 1)
        mime = header[5:].split(";")[0]
        modality = {"image_url": "image", "video_url": "video", "file": "file"}[kind]
    context.complete(
        {"file_id": data["file_id"], "filename": filename},
        parts=(InputPart(modality, mime, payload, filename),),
    )
    return PENDING


def register_archive_actions(registry):
    string = {"type": "string"}
    identity = {"agent_id": string, "session_id": string}
    search = {
        "query": {"type": "string", "minLength": 1},
        "limit": {"type": "integer", "minimum": 1, "default": 3},
    }
    definitions = {
        "search": (
            "Rank individual summaries of one instance across completed sessions by topic. Scores are relevance signals, not factual probabilities.",
            {"agent_id": string, **search},
            ["agent_id", "query"],
            {"matches": {}},
        ),
        "list_sessions": (
            "List completed sessions containing the specified instance, newest first.",
            {"agent_id": string},
            ["agent_id"],
            {"sessions": {}},
        ),
        "get_summary": (
            "Read the individual or complete system session summary from a completed archive.",
            {
                **identity,
                "scope": {
                    "type": "string",
                    "enum": ["instance", "session"],
                    "default": "instance",
                },
            },
            list(identity),
            {"summary": {}},
        ),
        "get_context_info": (
            "Get original message count and full serialized character count, including available thinking.",
            identity,
            list(identity),
            {"messages": {}, "characters": {}},
        ),
        "search_in_context": (
            "Rank overlapping token-aligned context fragments without thinking. Return inclusive zero-based original message ranges; read them separately with get_context.",
            {**identity, **search},
            [*identity, "query"],
            {"matches": {}},
        ),
        "get_context": (
            "Read full serialized history including thinking. Skip messages at each end; total counts only returned messages.",
            {
                **identity,
                "skip_first": {"type": "integer", "minimum": 0, "default": 0},
                "skip_last": {"type": "integer", "minimum": 0, "default": 0},
            },
            list(identity),
            {"context": {}, "total": {}},
        ),
        "get_file": (
            "Receive the original archived attachment as native media. The file is identified jointly by agent_id, session_id and file_id.",
            {**identity, "file_id": string},
            [*identity, "file_id"],
            {"file_id": {}, "filename": {}},
        ),
    }
    for name, (description, args, required, result) in definitions.items():
        registry.register(
            replace(
                action_definition(
                    description,
                    object_schema(args, required=required),
                    object_schema(result),
                    run,
                ),
                id="memory." + name,
                owner="core:module:memory",
            )
        )
