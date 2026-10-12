"""Неизменяемое event-thinking-action ядро Jarvis."""

from __future__ import annotations

from ..capabilities.api import ActionDefinition, EventDefinition
from .protocol import (
    empty_object_schema,
    object_schema,
)
from .registry import ActionRegistry, EventRegistry


def register_core_protocol(actions: ActionRegistry, events: EventRegistry) -> None:
    """Зарегистрировать зарезервированные элементы протокола."""
    from .system_actions import register_system_actions

    register_system_actions(actions)

    actions.register(
        ActionDefinition(
            id="no_action",
            description=(
                "Finish the current cycle and wait for new events. Must be the only action in the response."
            ),
            data_schema=empty_object_schema(),
            result_schema=empty_object_schema(),
            run=lambda data, context: {},
            owner="core",
        )
    )
    events.register(
        EventDefinition(
            event_id="structure_error",
            description=(
                "The previous response violated the JSON protocol or available input schema. Return a corrected response with fresh call identifiers."
            ),
            data_schema=object_schema(
                {
                    "code": {"type": "string"},
                    "message": {"type": "string"},
                    "response": {"type": "string"},
                }
            ),
        ),
        owner="core",
    )
    events.register(
        EventDefinition(
            event_id="message_from_agent",
            description=(
                "A direct message from another agent. from_agent_id identifies the sender; text contains their message or task."
            ),
            data_schema=object_schema(
                {
                    "from_agent_id": {"type": "string"},
                    "text": {"type": "string"},
                }
            ),
        ),
        owner="core",
    )
    events.register(
        EventDefinition(
            event_id="system_started",
            description=(
                "The system has started. This event gives you an opportunity to take initiative: consider your personality, available capabilities and current context, then independently choose a useful action or begin an interaction. Initiative is welcome but no specific action is mandatory. datetime is the local startup time with timezone."
            ),
            data_schema=object_schema(
                {
                    "datetime": {
                        "type": "string",
                        "description": "Local startup time in ISO 8601 with timezone offset.",
                    }
                }
            ),
        ),
        owner="core:primary",
    )
    events.register(
        EventDefinition(
            event_id="capability_error",
            description=(
                "An unhandled capability failure. kind and id identify its source; error contains details and call_id identifies an affected invocation if any."
            ),
            data_schema=object_schema(
                {
                    "kind": {
                        "type": "string",
                        "enum": ["module", "action", "handler"],
                    },
                    "id": {"type": "string"},
                    "error": {"type": "string"},
                    "call_id": {"type": ["string", "null"]},
                }
            ),
        ),
        owner="core",
    )
    actions.register(
        ActionDefinition(
            id="speech",
            description=(
                "Speak text aloud. Returns successful after playback or interrupted if new user speech interrupted it."
            ),
            data_schema=object_schema({"text": {"type": "string"}}),
            result_schema=object_schema(
                {
                    "status": {
                        "type": "string",
                        "enum": ["successful", "interrupted"],
                    }
                }
            ),
            run=_speech_action_run,
            owner="core:speech",
        )
    )
    events.register(
        EventDefinition(
            event_id="speech_detected",
            description=(
                "User speech recognized from the microphone. text contains the recognized utterance."
            ),
            data_schema=object_schema({"text": {"type": "string"}}),
        ),
        owner="core:speech",
    )


def _speech_action_run(data, context):
    from ..speech import service

    return service.speak_result(data["text"])


def _capability_identity(action_id: str) -> tuple[str, str]:
    """Capability, отвечающая за упавший вызов.

    Вызов действия внутри модуля сообщает весь модуль: по определению,
    capability — это целый модуль, автономное действие или handler, а
    единица модуля сама по себе capability не является.
    """

    if "." in action_id:
        return "module", action_id.split(".", 1)[0]
    return "action", action_id
