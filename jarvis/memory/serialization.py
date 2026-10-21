"""Compact, loss-preserving readable history with original message coordinates."""

from __future__ import annotations
import json
import math
from copy import deepcopy
from ..infrastructure.context import KINDS

REASONING = {"thinking", "reasoning", "reasoning_content", "reasoning_details"}


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def interpretation(part, *, agent_id=None, session_id=None):
    modality = KINDS.get(part.get("type"), "file")
    identity = {
        key: part[key]
        for key in ("agent_id", "session_id", "file_id", "filename")
        if key in part
    }
    identity.update({key: value for key, value in (("agent_id", agent_id), ("session_id", session_id)) if value is not None})
    return (
        f"<{modality}_interpretation_start>"
        + compact(identity)
        + part.get("text_representation", "")
        + f"<{modality}_interpretation_end>"
    )


def _without_reasoning(value):
    if isinstance(value, dict):
        return {
            key: _without_reasoning(item)
            for key, item in value.items()
            if key not in REASONING
        }
    if isinstance(value, list):
        return [_without_reasoning(item) for item in value]
    return value


def _text(value, include_thinking):
    if not isinstance(value, str):
        return compact(value if include_thinking else _without_reasoning(value))
    try:
        obj = json.loads(value)
    except (ValueError, TypeError):
        return value
    return compact(obj if include_thinking else _without_reasoning(obj))


def serialize_message(message, *, include_thinking=True, agent_id=None, session_id=None):
    agent = message.get("role") == "assistant"
    start, end = ("agent", "agent") if agent else ("event", "event")
    blocks = []
    if include_thinking and agent:
        for key in sorted(REASONING):
            if message.get(key):
                value = message[key]
                blocks.append(
                    "<thinking_start>"
                    + (value if isinstance(value, str) else compact(value))
                    + "<thinking_end>"
                )
    content = message.get("content")
    if isinstance(content, list):
        for part in content:
            if part.get("type") in KINDS:
                blocks.append(interpretation(part, agent_id=agent_id, session_id=session_id))
            elif part.get("type") in {"thinking", "reasoning"}:
                if include_thinking:
                    blocks.append(
                        "<thinking_start>"
                        + str(part.get("thinking", part.get("text", "")))
                        + "<thinking_end>"
                    )
            elif part.get("type") in {"text", "input_text", "output_text"}:
                blocks.append(_text(part.get("text", ""), include_thinking))
            else:
                blocks.append(_text(part, include_thinking))
    elif content is not None:
        # Some providers include thinking in the structured assistant value.
        try:
            value = json.loads(content)
        except (ValueError, TypeError):
            value = None
        if agent and isinstance(value, dict):
            for key in sorted(REASONING):
                if include_thinking and value.get(key):
                    thought = value[key]
                    blocks.append(
                        "<thinking_start>"
                        + (thought if isinstance(thought, str) else compact(thought))
                        + "<thinking_end>"
                    )
            blocks.append(compact(_without_reasoning(value)))
        else:
            blocks.append(_text(content, include_thinking))
    return f"<{start}_start>" + "".join(blocks) + f"<{end}_end>"


def serialize_context(context, *, include_thinking=True, agent_id=None, session_id=None):
    return "".join(
        serialize_message(m, include_thinking=include_thinking, agent_id=agent_id, session_id=session_id) for m in context
    )


def chunks(context, tokenizer, *, max_tokens=30000, agent_id=None, session_id=None):
    """About 30 overlapping token windows; retain inclusive source message indexes."""
    tokens, ranges = [], []
    for i, message in enumerate(context):
        encoded = tokenizer.encode(
            serialize_message(message, include_thinking=False, agent_id=agent_id, session_id=session_id), add_special_tokens=False
        )
        ranges.append((len(tokens), len(tokens) + len(encoded), i))
        tokens.extend(encoded)
    if not tokens:
        return []
    size = min(max_tokens, max(128, math.ceil(len(tokens) / (1 + 29 * 0.85))))
    if size < 1:
        raise ValueError("No token capacity for context candidates")
    result, begin = [], 0
    while begin < len(tokens):
        end = min(len(tokens), begin + size)
        if end < len(tokens):
            # Prefer nearby message boundaries without exceeding the model window.
            boundaries = [b for _, b, _ in ranges if begin + size * 0.8 <= b <= end]
            if boundaries:
                end = max(boundaries)
        owners = [i for a, b, i in ranges if a < end and b > begin]
        result.append(
            {
                "start": min(owners),
                "end": max(owners),
                "text": tokenizer.decode(tokens[begin:end], skip_special_tokens=False),
            }
        )
        if end == len(tokens):
            break
        begin = max(begin + 1, end - max(1, round((end - begin) * 0.15)))
    return result


def api_messages(context, directory, *, document_parts=None, include_thinking=False):
    """Restore native media without sending archive metadata to the provider."""
    from ..infrastructure.context import hydrate_part

    result = []
    for message in context:
        if message.get("role") == "system":
            continue
        clean = {
            key: deepcopy(message[key])
            for key in ("role", "content", "name", "tool_call_id", "tool_calls")
            if key in message
        }
        if isinstance(clean.get("content"), list):
            parts = []
            for part in clean["content"]:
                if (
                    document_parts is not None
                    and part.get("type") == "file"
                    and part.get("file_id")
                ):
                    parts.extend(document_parts(part))
                elif part.get("type") in KINDS:
                    parts.append(hydrate_part(part, directory))
                elif part.get("type") not in {"thinking", "reasoning"}:
                    parts.append(deepcopy(part))
            clean["content"] = parts
        if include_thinking and message.get("role") == "assistant":
            thoughts = [
                message[k] if isinstance(message[k], str) else compact(message[k])
                for k in sorted(REASONING)
                if message.get(k)
            ]
            if thoughts:
                content = clean.get("content")
                parts = (
                    [{"type": "text", "text": content}]
                    if isinstance(content, str)
                    else list(content or [])
                )
                clean["content"] = [
                    {
                        "type": "text",
                        "text": "<thinking_start>"
                        + "".join(thoughts)
                        + "<thinking_end>",
                    },
                    *parts,
                ]
        result.append(clean)
    return result


def summary_context(context):
    """Session-ending protocol is excluded only from the summarizer's input."""
    result = []
    shutdown_calls = set()
    for message in context:
        copy = deepcopy(message)
        content = copy.get("content")
        if copy.get("role") == "assistant" and isinstance(content, str):
            try:
                value = json.loads(content)
            except ValueError:
                value = None
            if isinstance(value, dict) and isinstance(value.get("actions"), list) and any(a.get("action_id") == "shutdown" for a in value["actions"]):
                shutdown_calls.update(a.get("call_id") for a in value["actions"] if a.get("action_id") == "shutdown")
                kept = [a for a in value["actions"] if a.get("action_id") != "shutdown"]
                if not kept:
                    continue
                if len(kept) != len(value["actions"]):
                    value["actions"] = kept
                    copy["content"] = compact(value)
        if isinstance(content, str):
            try:
                event = json.loads(content)
            except ValueError:
                event = None
            if isinstance(event, dict) and event.get("event_id") == "call_result" and event.get("call_id") in shutdown_calls:
                continue
        result.append(copy)
    return result
