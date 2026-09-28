"""ChatGPT-authenticated Codex as a model backend for Jarvis turns.

Jarvis remains the sole owner of agent history and action dispatch. Each turn
uses an ephemeral Codex thread populated from that history; Codex tools do not
replace Jarvis actions. Only the existing model-client boundary is adapted.
"""

from __future__ import annotations

import json
import queue
import tempfile
import threading
import webbrowser
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from openai_codex import ApprovalMode, Codex, ImageInput, Sandbox, TextInput
from openai_codex.generated.v2_all import ReasoningEffort

from ..core.protocol import validate_json
from .console import logger
from .model_capabilities import ModelCapabilities


SUBSCRIPTION_MODALITIES = ModelCapabilities("ChatGPT subscription", ("text", "image"))
_QUOTA_CODES = frozenset({"usageLimitExceeded", "rateLimitExceeded"})
_UNSUPPORTED_SCHEMA_KEYS = frozenset(
    {
        "default",
        "x-jarvis-open-object",
        "minItems",
        "maxItems",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "pattern",
        "format",
        "uniqueItems",
        "additionalProperties",
    }
)


class SubscriptionQuotaExceeded(RuntimeError):
    """The Codex account has exhausted an applicable usage window."""


def _strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Translate Jarvis descriptions to Codex's strict per-turn output schema."""
    if schema.get("x-jarvis-open-object") or schema.get("additionalProperties") is True:
        return {
            "type": "string",
            "description": "JSON-encoded object; provide every member required by its Jarvis action contract.",
        }
    result = {
        key: deepcopy(value)
        for key, value in schema.items()
        if key not in _UNSUPPORTED_SCHEMA_KEYS
    }
    if isinstance(schema.get("properties"), dict):
        required = set(schema.get("required", ()))
        result["properties"] = {}
        for name, child in schema["properties"].items():
            translated = _strict_schema(child)
            if name not in required:
                if isinstance(translated.get("type"), str):
                    translated["type"] = [translated["type"], "null"]
                elif isinstance(translated.get("type"), list):
                    translated["type"] = list(
                        dict.fromkeys([*translated["type"], "null"])
                    )
                elif "anyOf" in translated:
                    translated["anyOf"].append({"type": "null"})
                if "enum" in translated and None not in translated["enum"]:
                    translated["enum"] = [*translated["enum"], None]
            result["properties"][name] = translated
        result["required"] = list(result["properties"])
        result["additionalProperties"] = False
    if isinstance(schema.get("items"), dict):
        result["items"] = _strict_schema(schema["items"])
    for keyword in ("anyOf", "oneOf", "allOf"):
        if keyword in schema:
            result[keyword] = [_strict_schema(child) for child in schema[keyword]]
    return result


def _action_schemas(schema: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Find the action-specific data contracts embedded in response_format."""
    found: dict[str, dict[str, Any]] = {}

    def visit(node: Any) -> None:
        if isinstance(node, list):
            for child in node:
                visit(child)
        elif isinstance(node, dict):
            properties = node.get("properties", {})
            action_id = properties.get("action_id", {})
            if (
                isinstance(action_id, dict)
                and len(action_id.get("enum", ())) == 1
                and "data" in properties
            ):
                found[action_id["enum"][0]] = properties["data"]
            for child in node.values():
                visit(child)

    visit(schema)
    return found


def _restore_object(value: Any, schema: dict[str, Any]) -> Any:
    if schema.get("x-jarvis-open-object") or schema.get("additionalProperties") is True:
        return json.loads(value) if isinstance(value, str) else value
    if isinstance(value, list):
        item_schema = schema.get("items", {})
        if "anyOf" in schema:
            for variant in schema["anyOf"]:
                if variant.get("type") == "array" and (
                    "maxItems" not in variant or len(value) <= variant["maxItems"]
                ):
                    return _restore_object(value, variant)
        return [_restore_object(item, item_schema) for item in value]
    if not isinstance(value, dict):
        return value
    if "anyOf" in schema:
        for variant in schema["anyOf"]:
            properties = variant.get("properties", {})
            discriminator = properties.get("action_id", {}).get("enum", ())
            if discriminator and value.get("action_id") in discriminator:
                return _restore_object(value, variant)
            if not discriminator and all(
                value.get(name) is not None for name in variant.get("required", ())
            ):
                return _restore_object(value, variant)
    properties = schema.get("properties", {})
    required = set(schema.get("required", ()))
    restored = {}
    for name, item in value.items():
        child = properties.get(name, {})
        nullable = "null" in (
            child.get("type")
            if isinstance(child.get("type"), list)
            else [child.get("type")]
        )
        if item is None and name not in required and not nullable:
            continue
        restored[name] = _restore_object(item, child)
    return restored


def _normalize_response(content: str, schema: dict[str, Any]) -> str:
    value = _restore_object(json.loads(content), schema)
    validate_json(value, schema, where="Codex response")
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _user_parts(content: Any) -> tuple[list[dict[str, Any]], list[Any]]:
    """Preserve text/images; make unsupported historical parts explicit."""
    from openai_codex import ImageInput, TextInput

    if isinstance(content, str):
        return ([{"type": "input_text", "text": content}], [TextInput(content)])
    response_parts: list[dict[str, Any]] = []
    turn_parts: list[Any] = []
    for part in content:
        kind = part.get("type")
        if kind == "text":
            text = part["text"]
            response_parts.append({"type": "input_text", "text": text})
            turn_parts.append(TextInput(text))
        elif kind == "image_url":
            image = part.get("image_url", {})
            url = image.get("url")
            if url:
                if image.get("name"):
                    marker = f"Image attachment: {image['name']}"
                    response_parts.append({"type": "input_text", "text": marker})
                    turn_parts.append(TextInput(marker))
                response_parts.append({"type": "input_image", "image_url": url})
                turn_parts.append(ImageInput(url))
        else:
            name = (
                (part.get("file") or {}).get("filename")
                or (part.get("input_audio") or {}).get("name")
                or (part.get("video_url") or {}).get("name")
                or "unnamed"
            )
            marker = f"[Attachment not available as native subscription input: {name}; type: {kind}]"
            response_parts.append({"type": "input_text", "text": marker})
            turn_parts.append(TextInput(marker))
    if not turn_parts:
        response_parts = [{"type": "input_text", "text": ""}]
        turn_parts = [TextInput("")]
    return response_parts, turn_parts


def _history_items(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for message in messages:
        if message["role"] == "assistant":
            result.append(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": message["content"]}],
                }
            )
        elif message["role"] == "user":
            parts, _ = _user_parts(message["content"])
            result.append({"type": "message", "role": "user", "content": parts})
    return result


def _quota_error(error: Any) -> bool:
    info = getattr(error, "codex_error_info", None)
    code = getattr(info, "root", info)
    value = getattr(code, "value", code)
    if isinstance(value, str) and value in _QUOTA_CODES:
        return True
    if isinstance(code, dict) and any(name in code for name in _QUOTA_CODES):
        return True
    message = getattr(error, "message", None) or str(error)
    if (
        "you’ve hit your usage limit" in message.lower()
        or "you've hit your usage limit" in message.lower()
    ):
        return True
    return False


class SubscriptionStream:
    """A cancelable stream compatible with the current Agent loop."""

    def __init__(self, router: "SubscriptionClient", options: dict[str, Any]):
        self.router = router
        self.options = options
        self._closed = threading.Event()
        self._queue: queue.Queue[Any] = queue.Queue()
        self._lock = threading.RLock()
        self._turn = None
        self._turn_finished = False
        self._api_stream = None
        threading.Thread(
            target=self._work, name="jarvis-subscription-turn", daemon=True
        ).start()

    def __iter__(self):
        return self

    def __next__(self):
        if self._closed.is_set():
            raise StopIteration
        item = self._queue.get()
        if isinstance(item, BaseException):
            raise item
        if item is None or self._closed.is_set():
            raise StopIteration
        return item

    def _publish(self, item: Any) -> None:
        if not self._closed.is_set():
            self._queue.put(item)

    def _work(self) -> None:
        try:
            content = self.router._generate(self.options, self)
            with self._lock:
                self._turn_finished = True
            if content is not None:
                self._publish(
                    SimpleNamespace(
                        choices=[
                            SimpleNamespace(
                                delta=SimpleNamespace(content=content, refusal=None)
                            )
                        ]
                    )
                )
        except BaseException as exc:
            with self._lock:
                self._turn_finished = True
            if isinstance(exc, SubscriptionQuotaExceeded) or _quota_error(exc):
                self._fallback(exc)
            else:
                self._publish(exc)
        finally:
            self._publish(None)

    def _fallback(self, reason: Exception) -> None:
        if self._closed.is_set():
            return
        try:
            self.router._switch_to_api(reason)
            if self._closed.is_set():
                return
            stream = self.router._api_request(self.options)
            with self._lock:
                self._api_stream = stream
            for chunk in stream:
                if self._closed.is_set():
                    break
                self._publish(chunk)
        except BaseException as exc:
            self._publish(exc)

    def set_turn(self, turn: Any) -> None:
        with self._lock:
            self._turn = turn
            closed = self._closed.is_set()
        if closed:
            turn.interrupt()

    def close(self) -> None:
        self._closed.set()
        with self._lock:
            turn, api_stream, finished = (
                self._turn,
                self._api_stream,
                self._turn_finished,
            )
        if turn is not None and not finished:
            try:
                turn.interrupt()
            except Exception:
                logger.exception("Could not interrupt Codex turn")
        if api_stream is not None:
            close_api = getattr(api_stream, "close", None)
            if close_api is not None:
                close_api()
        self._queue.put(None)


class SubscriptionClient:
    """Expose ChatGPT-authenticated Codex behind the existing model interface."""

    def __init__(
        self,
        *,
        api_client: Any,
        api_model: str,
        subscription_model: str | None,
        api_capabilities: ModelCapabilities,
        project_root: Path,
        on_fallback=None,
        codex: Any = None,
    ):
        self.api_client = api_client
        self.api_model = api_model
        self.subscription_model = subscription_model
        self.api_capabilities = api_capabilities
        self.project_root = project_root
        self.on_fallback = on_fallback
        self.codex = codex or Codex()
        self.chat = SimpleNamespace(completions=self)
        self._lock = threading.RLock()
        self._api_only = False

    def ensure_chatgpt_login(self) -> None:
        account = self.codex.account().model_dump(by_alias=True).get("account") or {}
        if account.get("type") == "chatgpt":
            return
        handle = self.codex.login_chatgpt()
        if not handle.auth_url or not webbrowser.open(handle.auth_url):
            raise RuntimeError("Could not open the ChatGPT login URL in a browser")
        completion = handle.wait()
        if not completion.success:
            raise RuntimeError(f"ChatGPT login failed: {completion.error}")
        account = self.codex.account().model_dump(by_alias=True).get("account") or {}
        if account.get("type") != "chatgpt":
            raise RuntimeError(
                "Codex did not establish ChatGPT subscription authentication"
            )

    def create(self, **kwargs):
        with self._lock:
            if self._api_only:
                return self._api_request(kwargs)
        return SubscriptionStream(self, kwargs)

    def _switch_to_api(self, error: Exception) -> None:
        with self._lock:
            if self._api_only:
                return
            self._api_only = True
        logger.warning("ChatGPT subscription limit reached; using API: %s", error)
        if self.on_fallback is not None:
            self.on_fallback(self.api_capabilities)

    def _api_request(self, options: dict[str, Any]):
        messages = deepcopy(options["messages"])
        system = messages[0]
        system["content"] = system["content"].replace(
            SUBSCRIPTION_MODALITIES.prompt_block(),
            self.api_capabilities.prompt_block(),
            1,
        )
        for message in messages[1:]:
            if message.get("role") != "user" or not isinstance(
                message.get("content"), list
            ):
                continue
            content = []
            for part in message["content"]:
                kind = part.get("type")
                modality = {
                    "image_url": "image",
                    "input_audio": "audio",
                    "video_url": "video",
                    "file": "file",
                }.get(kind)
                if modality is None or self.api_capabilities.supports(modality):
                    content.append(part)
            message["content"] = content
        return self.api_client.chat.completions.create(
            **{**options, "model": self.api_model, "messages": messages}
        )

    @staticmethod
    def _codex_effort(value: str | None) -> ReasoningEffort | None:
        if value is None:
            return None
        try:
            return ReasoningEffort(value)
        except ValueError as exc:
            raise ValueError(f"Unsupported Codex reasoning effort: {value}") from exc

    def _generate(
        self, options: dict[str, Any], stream: SubscriptionStream
    ) -> str | None:
        messages = options["messages"]
        schema = options["response_format"]["json_schema"]["schema"]
        if (
            not messages
            or messages[0]["role"] != "system"
            or messages[-1]["role"] != "user"
        ):
            raise ValueError(
                "Codex turns require a system prompt and a final user event"
            )
        with tempfile.TemporaryDirectory(prefix="jarvis-codex-turn-") as cwd:
            thread = self.codex.thread_start(
                base_instructions=messages[0]["content"],
                developer_instructions=(
                    "Return only the JSON output required by the per-turn schema. "
                    "Do not call Codex tools or inspect the filesystem. Jarvis dispatches actions. "
                    "If a schema field describes a JSON-encoded object, serialize that object's contents "
                    "as a JSON string; Jarvis reconstructs it before execution."
                ),
                model=self.subscription_model,
                model_provider="openai",
                cwd=cwd,
                ephemeral=True,
                sandbox=Sandbox.read_only,
                approval_mode=ApprovalMode.deny_all,
            )
            if stream._closed.is_set():
                return None
            try:
                old = _history_items(messages[1:-1])
                if old:
                    self.codex._client._request_raw(
                        "thread/inject_items", {"threadId": thread.id, "items": old}
                    )
                if stream._closed.is_set():
                    return None
                _, latest = _user_parts(messages[-1]["content"])
                turn = thread.turn(
                    latest,
                    output_schema=_strict_schema(schema),
                    effort=self._codex_effort(options.get("reasoning_effort")),
                    approval_mode=ApprovalMode.deny_all,
                    sandbox=Sandbox.read_only,
                )
                stream.set_turn(turn)
                answer = None
                for event in turn.stream():
                    if stream._closed.is_set():
                        return None
                    if event.method == "item/completed":
                        item = getattr(event.payload.item, "root", event.payload.item)
                        if getattr(item, "type", None) == "agentMessage" and getattr(
                            getattr(item, "phase", None), "value", None
                        ) in {"final_answer", None}:
                            answer = item.text
                    elif event.method == "turn/completed":
                        completed = event.payload.turn
                        status = getattr(completed.status, "value", completed.status)
                        if status == "failed":
                            error = completed.error
                            message = error.message if error else "Codex turn failed"
                            if error is not None and _quota_error(error):
                                raise SubscriptionQuotaExceeded(message)
                            raise RuntimeError(message)
                        if status != "completed":
                            raise RuntimeError(f"Codex turn ended with status {status}")
                if answer is None:
                    raise RuntimeError("Codex returned no final assistant message")
                try:
                    return _normalize_response(answer, schema)
                except (ValueError, TypeError) as exc:
                    # Let Agent record structure_error and request a corrected
                    # response with a fresh call_id, without a model-API retry.
                    logger.info("Codex response violated Jarvis schema: %s", exc)
                    return answer
            finally:
                try:
                    self.codex._client._request_raw(
                        "thread/unsubscribe", {"threadId": thread.id}
                    )
                except Exception:
                    logger.exception("Could not release an ephemeral Codex thread")

    def close(self) -> None:
        self.codex.close()
        self.api_client.close()
