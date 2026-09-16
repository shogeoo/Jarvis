import json
import unittest

from jarvis.protocol import Event, parse_actions, response_format, validate_json


class ProtocolTests(unittest.TestCase):
    def test_event_is_one_compact_model_value(self):
        event = Event(type="speech", data={"text": "Привет"})
        self.assertEqual(
            json.loads(event.model_content()),
            {"type": "speech", "data": {"text": "Привет"}},
        )

    def test_no_action_cannot_be_combined(self):
        with self.assertRaises(ValueError):
            parse_actions(
                {
                    "actions": [
                        {"type": "no_action", "data": {}},
                        {"type": "speech", "data": {"text": "x"}},
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
                "speech": {
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
                        {"type": "no_action", "data": {}},
                        {"type": "speech", "data": {"text": "x"}},
                    ]
                },
                schema,
            )

    def test_strict_action_schema_rejects_unknown_arguments(self):
        schema = response_format(
            {
                "speech": {
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
                        {"type": "speech", "data": {"text": "x", "extra": 1}}
                    ]
                },
                schema,
            )


if __name__ == "__main__":
    unittest.main()
