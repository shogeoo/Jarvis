import json
import tempfile
import unittest
from pathlib import Path

from jarvis.infrastructure.automations import AutomationStore


class AutomationStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "automations.json"
        self.store = AutomationStore(self.path)

    def test_call_result_trigger_ignores_only_the_variable_call_id(self):
        automation = {
            "call_result": {
                "event_id": "call_result",
                "call_id": "act-from-example",
                "data": {"status": "successful"},
            },
            "actions": [{"action_id": "speech", "data": {"text": "Done"}}],
        }
        self.store.append(automation)

        match = self.store.matching(
            {
                "event_id": "call_result",
                "call_id": "any-real-call-id",
                "data": {"status": "successful"},
            }
        )
        self.assertEqual(match, [automation])
        self.assertEqual(
            self.store.matching(
                {
                    "event_id": "call_result",
                    "call_id": "any-real-call-id",
                    "data": {"status": "successful", "extra": True},
                }
            ),
            [],
        )

    def test_event_trigger_requires_full_structural_equality(self):
        automation = {
            "event": {"event_id": "speech_detected", "data": {"text": "next"}},
            "actions": [{"action_id": "media_next", "data": {}}],
        }
        self.store.append(automation)
        self.assertEqual(self.store.matching(automation["event"]), [automation])
        self.assertEqual(
            self.store.matching(
                {"event_id": "speech_detected", "data": {"text": "next", "extra": 1}}
            ),
            [],
        )

    def test_all_matching_rules_are_returned_in_file_order(self):
        trigger = {"event_id": "tick.event", "data": {"text": "go"}}
        first = {
            "event": trigger,
            "actions": [{"action_id": "say", "data": {"text": "first"}}],
        }
        second = {
            "event": trigger,
            "actions": [{"action_id": "say", "data": {"text": "second"}}],
        }
        self.store.append(first)
        self.store.append(second)
        self.assertEqual(self.store.matching(trigger), [first, second])

    def test_json_value_types_are_part_of_exact_match(self):
        automation = {
            "event": {"event_id": "flag", "data": {"enabled": True}},
            "actions": [{"action_id": "say", "data": {"text": "yes"}}],
        }
        self.store.append(automation)
        self.assertEqual(
            self.store.matching({"event_id": "flag", "data": {"enabled": 1}}),
            [],
        )

    def test_automation_shape_rejects_multiple_triggers_and_action_call_ids(self):
        base = {
            "event": {"event_id": "tick.event", "data": {}},
            "actions": [{"action_id": "say", "data": {}}],
        }
        invalid = [
            {**base, "call_result": {"event_id": "call_result", "call_id": "x", "data": {}}},
            {
                "event": base["event"],
                "actions": [{"action_id": "say", "call_id": "x", "data": {}}],
            },
        ]
        for automation in invalid:
            with self.subTest(automation=automation), self.assertRaises(ValueError):
                AutomationStore.validate(automation)

    def test_append_persists_a_json_array(self):
        automation = {
            "event": {"event_id": "timer", "data": {"at": "noon"}},
            "actions": [{"action_id": "speech", "data": {"text": "Hi"}}],
        }
        self.store.append(automation)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), [automation])


if __name__ == "__main__":
    unittest.main()
