import json
import unittest
from unittest.mock import patch

from jarvis.infrastructure.model_capabilities import ModelCapabilities
from jarvis.core.registry import ActionRegistry, EventRegistry
from jarvis.core.runtime import register_core_protocol
from jarvis.core.protocol import (
    CallResult,
    Event,
    InputPart,
    parse_actions,
    response_format,
    validate_json,
)


class ProtocolTests(unittest.TestCase):
    def test_core_actions_register_without_jsonschema_dependency(self):
        with patch("jarvis.core.protocol.Draft202012Validator", None):
            actions = ActionRegistry()
            register_core_protocol(actions, EventRegistry())
            self.assertIsNotNone(actions.get("create_automation"))

    def test_event_is_one_compact_model_value(self):
        event = Event(type="sample", data={"text": "Привет"})
        self.assertEqual(
            json.loads(event.model_content()),
            {"type": "sample", "data": {"text": "Привет"}},
        )

    def test_multimodal_event_is_one_message_and_filters_unsupported_parts(self):
        event = Event(
            type="telegram.message",
            data={"text": "Что на фото?"},
            parts=(InputPart("image", "image/jpeg", "aGVsbG8="),),
        )
        image_message = event.model_message(
            ModelCapabilities("test", ("text", "image"))
        )
        self.assertEqual(image_message["role"], "user")
        self.assertEqual(len(image_message["content"]), 2)
        self.assertEqual(image_message["content"][1]["type"], "image_url")
        text_message = event.model_message(ModelCapabilities("test", ("text",)))
        self.assertIsInstance(text_message["content"], str)

    def test_call_result_is_a_strict_model_value_with_external_action_id(self):
        result = CallResult(
            call_id="say-1", data={"spoken": True}, agent_id="main"
        )
        self.assertEqual(
            json.loads(result.model_content()),
            {
                "type": "call_result",
                "call_id": "say-1",
                "data": {"spoken": True},
            },
        )
        self.assertEqual(result.model_message()["role"], "user")
        self.assertNotIn("agent_id", json.loads(result.model_content()))

    def test_no_action_cannot_be_combined(self):
        with self.assertRaises(ValueError):
            parse_actions(
                {
                    "actions": [
                        {"action_id": "no_action", "call_id": "one", "data": {}},
                        {"action_id": "sample", "call_id": "two", "data": {}},
                    ]
                }
            )

        schema = response_format(
            {
                "no_action": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
                "sample": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                    "additionalProperties": False,
                },
            }
        )["json_schema"]["schema"]
        with self.assertRaises(ValueError):
            validate_json(
                {
                    "actions": [
                        {"action_id": "no_action", "call_id": "one", "data": {}},
                        {"action_id": "sample", "call_id": "two", "data": {}},
                    ]
                },
                schema,
            )

    def test_duplicate_action_id_is_a_structure_error(self):
        with self.assertRaisesRegex(ValueError, "call_id повторяется"):
            parse_actions(
                {
                    "actions": [
                        {
                            "action_id": "sample",
                            "call_id": "say-1",
                            "data": {"text": "one"},
                        },
                        {
                            "action_id": "sample",
                            "call_id": "say-1",
                            "data": {"text": "two"},
                        },
                    ]
                }
            )

    def test_strict_action_schema_rejects_unknown_arguments(self):
        schema = response_format(
            {
                "sample": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                    "additionalProperties": False,
                }
            }
        )["json_schema"]["schema"]
        with self.assertRaises(ValueError):
            validate_json(
                {
                    "actions": [
                        {
                            "action_id": "sample",
                            "call_id": "say-1",
                            "data": {"text": "x", "extra": 1},
                        }
                    ]
                },
                schema,
            )


if __name__ == "__main__":
    unittest.main()
