"""Editable built-in descriptions preserve schemas and runtime behavior."""

import json
import tempfile
import unittest
from dataclasses import replace
from importlib.resources import files
from pathlib import Path

from jarvis.core.descriptions import apply_descriptions, read_descriptions
from jarvis.core.protocol import object_schema
from jarvis.core.registry import ActionRegistry, EventRegistry
from jarvis.core.runtime import AgentManager, register_core_protocol


class DescriptionTests(unittest.TestCase):
    def definitions(self):
        actions, events = ActionRegistry(), EventRegistry()
        register_core_protocol(actions, events)
        specs = actions.all()
        specs["create_automation"] = replace(specs["create_automation"], data_schema=AgentManager.automation_data_schema(None))
        specs["edit_automation"] = replace(specs["edit_automation"], data_schema=object_schema({
            "automation_id": {"type": "string"}, "automation": AgentManager.automation_data_schema(None),
        }))
        return specs, events.all()

    def test_all_builtin_text_is_exported_without_changing_current_definitions(self):
        actions, events = self.definitions()
        catalog = read_descriptions()
        self.assertEqual(set(catalog["actions"]), set(actions))
        self.assertEqual(set(catalog["handlers"]), set(events))
        self.assertEqual(catalog["modules"], {})
        actual_actions, actual_events = apply_descriptions(actions, events)
        self.assertEqual(actions, actual_actions)
        self.assertEqual(events, actual_events)

    def test_edits_change_descriptions_only(self):
        source = files("jarvis").joinpath("assets", "capability_descriptions.json")
        value = json.loads(source.read_text())
        speech = next(entry["speech"] for entry in value["actions"] if "speech" in entry)
        speech["description"] = "Edited speech instructions."
        speech["data_schema"]["properties"]["text"]["description"] = "Edited text description."
        speech["result_schema"]["properties"]["status"]["description"] = "Edited status description."
        actions, events = self.definitions()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "descriptions.json"
            path.write_text(json.dumps(value))
            updated, _ = apply_descriptions(actions, events, path=path)
        self.assertEqual(updated["speech"].description, "Edited speech instructions.")
        self.assertEqual(updated["speech"].data_schema["properties"]["text"]["description"], "Edited text description.")
        self.assertEqual(updated["speech"].result_schema["properties"]["status"]["description"], "Edited status description.")
        self.assertIs(updated["speech"].run, actions["speech"].run)
        self.assertEqual(updated["speech"].data_schema["required"], actions["speech"].data_schema["required"])
        self.assertEqual(updated["speech"].data_schema["properties"]["text"]["type"], "string")

    def test_file_cannot_override_schema_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "descriptions.json"
            path.write_text(json.dumps({"actions": [{"speech": {"description": "Speak.", "type": "object"}}], "handlers": [], "modules": []}))
            with self.assertRaises(ValueError):
                read_descriptions(path)
