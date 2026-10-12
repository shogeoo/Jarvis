import json
import tempfile
import unittest
from pathlib import Path

from jarvis.presets import PresetStore


class PresetStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "presets"
        self.root.mkdir()
        main = self.root / "main"
        main.mkdir()
        (main / "person.txt").write_text("Jarvis\n", encoding="utf-8")
        (main / "capabilities.json").write_text(
            json.dumps(
                {"modules": ["agents"], "actions": [], "handlers": []}, indent=2
            ) + "\n",
            encoding="utf-8",
        )
        (main / "preset.json").write_text(
            '{"protected": true}\n', encoding="utf-8"
        )
        (self.root.parent / "capabilities/modules/agents").mkdir(parents=True, exist_ok=True)
        (self.root.parent / "capabilities/handlers/tick").mkdir(parents=True, exist_ok=True)
        self.store = PresetStore(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def test_main_person_prompt_cannot_be_replaced_or_deleted(self):
        with self.assertRaises(ValueError):
            self.store.create(
                "main",
                "changed",
                {"modules": ["agents"], "actions": [], "handlers": []},
            )
        self.store.edit("main", changes={"person": "changed"})
        self.assertEqual(self.store.load("main").person_prompt, "changed")
        self.store.delete("main")
        self.assertFalse((self.root / "main").exists())

    def test_duplicate_capabilities_are_rejected(self):
        path = self.root / "main" / "capabilities.json"
        capabilities = json.loads(path.read_text(encoding="utf-8"))
        capabilities["actions"] = ["say", "say"]
        path.write_text(json.dumps(capabilities), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.store.load("main")

    def test_capability_can_be_added_without_duplicates(self):
        self.store.add_capability("main", "action", "say")
        self.store.add_capability("main", "action", "say")
        self.store.add_capability("main", "handler", "tick")
        preset = self.store.load("main")
        self.assertIn("agents", preset.capabilities)
        self.assertEqual(preset.actions, ("say",))
        self.assertIn("tick", preset.capabilities)


if __name__ == "__main__":
    unittest.main()
