"""Direct ChatGPT subscription model transport; Jarvis owns the agent loop."""

from __future__ import annotations

import json
import queue
import threading
from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import requests

from ..core.protocol import validate_json
from .console import logger
from .model_capabilities import ModelCapabilities
from .subscription_auth import SubscriptionAuth


SUBSCRIPTION_MODALITIES = ModelCapabilities("ChatGPT subscription", ("text", "image"))
_RESPONSES_URL = "https://chatgpt.com/backend-api/codex/responses"
_QUOTA_CODES = frozenset(
    {
        "usage_limit_exceeded",
        "rate_limit_exceeded",
        "usageLimitExceeded",
        "rateLimitExceeded",
    }
)
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
    """ChatGPT account has exhausted its subscription allowance."""


def _strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Convert Jarvis optional and open fields to strict Responses JSON Schema."""
    if schema.get("x-jarvis-open-object") or schema.get("additionalProperties") is True:
        return {
            "type": "string",
            "description": "JSON-encoded object; Jarvis decodes it after generation.",
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
    validate_json(value, schema, where="subscription response")
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _user_parts(content: Any) -> list[dict[str, Any]]:
    """Translate Jarvis context parts to direct Responses content items."""
    if isinstance(content, str):
        return [{"type": "input_text", "text": content}]
    parts = []
    for part in content:
        kind = part.get("type")
        if kind == "text":
            parts.append({"type": "input_text", "text": part["text"]})
        elif kind == "image_url":
            image = part.get("image_url") or {}
            if image.get("name"):
                parts.append(
                    {"type": "input_text", "text": "Image attachment: " + image["name"]}
                )
            if image.get("url"):
                parts.append({"type": "input_image", "image_url": image["url"]})
        else:
            name = (
                (part.get("file") or {}).get("filename")
                or (part.get("input_audio") or {}).get("name")
                or (part.get("video_url") or {}).get("name")
                or "unnamed"
            )
            parts.append(
                {
                    "type": "input_text",
                    "text": f"[Attachment not available as native subscription input: {name}; type: {kind}]",
                }
            )
    return parts or [{"type": "input_text", "text": ""}]


def _history_items(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for message in messages:
        if message["role"] == "assistant":
            result.append(
                {
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": message["content"]}],
                }
            )
        elif message["role"] in {"system", "user"}:
            result.append(
                {"role": message["role"], "content": _user_parts(message["content"])}
            )
    return result


def _quota_error(error: Any) -> bool:
    code = getattr(error, "code", None)
    if isinstance(error, dict):
        nested = error.get("error")
        code = error.get("code") or (
            nested.get("code") if isinstance(nested, dict) else None
        )
    if isinstance(code, str) and code in _QUOTA_CODES:
        return True
    message = getattr(error, "message", None) or str(error)
    lowered = message.lower()
    return (
        "you’ve hit your usage limit" in lowered
        or "you've hit your usage limit" in lowered
    )


class SubscriptionStream:
    """Cancelable streaming adapter for the existing Agent model-client loop."""

    def __init__(self, router: "SubscriptionClient", options: dict[str, Any]):
        self.router = router
        self.options = options
        self._closed = threading.Event()
        self._queue: queue.Queue[Any] = queue.Queue()
        self._lock = threading.RLock()
        self._response = None
        self._api_stream = None
        threading.Thread(
            target=self._work, name="jarvis-subscription-request", daemon=True
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

    def set_response(self, response) -> None:
        with self._lock:
            self._response = response
            closed = self._closed.is_set()
        if closed:
            response.close()

    def close(self) -> None:
        self._closed.set()
        with self._lock:
            response, api_stream = self._response, self._api_stream
        if response is not None:
            response.close()
        if api_stream is not None:
            close_api = getattr(api_stream, "close", None)
            if close_api is not None:
                close_api()
        self._queue.put(None)


class SubscriptionClient:
    """Model-only client for ChatGPT subscription and API fallback."""

    def __init__(
        self,
        *,
        api_client: Any,
        api_model: str,
        subscription_model: str,
        api_capabilities: ModelCapabilities,
        auth: SubscriptionAuth,
        on_fallback=None,
        api_capabilities_loader=None,
        session=None,
    ):
        self.api_client = api_client
        self.api_model = api_model
        self.subscription_model = subscription_model
        self.api_capabilities = api_capabilities
        self.auth = auth
        self.on_fallback = on_fallback
        self.api_capabilities_loader = api_capabilities_loader
        self.session = session or requests.Session()
        self.chat = SimpleNamespace(completions=self)
        self._lock = threading.RLock()
        self._api_only = False

    def ensure_chatgpt_login(self, announce) -> None:
        self.auth.ensure_login(announce)

    def create(self, **kwargs):
        with self._lock:
            if self._api_only:
                return self._api_request(kwargs)
        return SubscriptionStream(self, kwargs)

    def _switch_to_api(self, error: Exception) -> None:
        with self._lock:
            if self._api_only:
                return
            if self.api_capabilities_loader is not None:
                self.api_capabilities = self.api_capabilities_loader()
            self._api_only = True
        logger.warning("ChatGPT subscription limit reached; using API: %s", error)
        if self.on_fallback is not None:
            self.on_fallback(self.api_capabilities)

    def _api_request(self, options: dict[str, Any]):
        messages = deepcopy(options["messages"])
        messages[0]["content"] = messages[0]["content"].replace(
            ModelCapabilities(self.subscription_model, SUBSCRIPTION_MODALITIES.input_modalities).prompt_block(),
            self.api_capabilities.prompt_block(),
            1,
        )
        messages[0]["content"] = messages[0]["content"].replace(
            "Model ID: " + self.subscription_model,
            "Model ID: " + self.api_model,
            1,
        )
        for message in messages[1:]:
            if message.get("role") != "user" or not isinstance(
                message.get("content"), list
            ):
                continue
            message["content"] = [
                part
                for part in message["content"]
                if self.api_capabilities.supports(
                    {
                        "image_url": "image",
                        "input_audio": "audio",
                        "video_url": "video",
                        "file": "file",
                    }.get(part.get("type"), "text")
                )
            ]
        return self.api_client.chat.completions.create(
            **{**options, "model": self.api_model, "messages": messages}
        )

    def _generate(
        self, options: dict[str, Any], stream: SubscriptionStream
    ) -> str | None:
        schema = options["response_format"]["json_schema"]["schema"]
        messages = options["messages"]
        if not messages or messages[0].get("role") != "system":
            raise ValueError(
                "Subscription request must start with Jarvis system prompt"
            )
        body: dict[str, Any] = {
            "model": self.subscription_model,
            "input": _history_items(messages),
            "store": False,
            "stream": True,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "agent_actions",
                    "strict": True,
                    "schema": _strict_schema(schema),
                }
            },
        }
        if options.get("reasoning_effort"):
            body["reasoning"] = {"effort": options["reasoning_effort"]}
        for attempt in range(2):
            credentials = self.auth.credentials(force_refresh=attempt > 0)
            headers = {
                "Authorization": "Bearer " + credentials["access_token"],
                "ChatGPT-Account-Id": credentials["account_id"],
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "originator": "jarvis",
            }
            response = self.session.post(
                _RESPONSES_URL,
                json=body,
                headers=headers,
                stream=True,
                timeout=(15, 90),
            )
            stream.set_response(response)
            if response.status_code == 401 and attempt == 0:
                response.close()
                continue
            if response.status_code == 429:
                message = response.text[:500]
                response.close()
                raise SubscriptionQuotaExceeded(message)
            if response.status_code >= 400:
                message = response.text[:1000]
                response.close()
                raise RuntimeError(
                    f"ChatGPT subscription request failed ({response.status_code}): {message}"
                )
            break
        content = ""
        completed = False
        try:
            for raw in response.iter_lines():
                if stream._closed.is_set():
                    return None
                line = (
                    raw.decode("utf-8", errors="replace")
                    if isinstance(raw, bytes)
                    else raw
                )
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                event = json.loads(payload)
                kind = event.get("type")
                if kind == "response.output_text.delta":
                    content += event.get("delta") or ""
                elif kind in {"response.failed", "error"}:
                    error = (
                        event.get("error")
                        or (event.get("response") or {}).get("error")
                        or event
                    )
                    if _quota_error(error):
                        raise SubscriptionQuotaExceeded(str(error))
                    raise RuntimeError(
                        "ChatGPT subscription generation failed: " + str(error)
                    )
                elif kind == "response.completed":
                    completed = True
                    break
        finally:
            response.close()
        if not completed:
            raise RuntimeError(
                "ChatGPT subscription stream ended without response.completed"
            )
        try:
            return _normalize_response(content, schema)
        except (ValueError, TypeError) as exc:
            # Let Agent record structure_error and ask the model again with a new call_id.
            logger.info("Subscription response violated Jarvis schema: %s", exc)
            return content

    def close(self) -> None:
        self.session.close()
        self.auth.close()
        self.api_client.close()
