import json
import tempfile
import unittest
from pathlib import Path

from jarvis.presets import PresetStore


class PresetStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        main = self.root / "main"
        main.mkdir()
        (main / "personprompt.txt").write_text("Jarvis\n", encoding="utf-8")
        (main / "capabilities.json").write_text(
            json.dumps(
                {"modules": ["agents"], "actions": [], "handlers": []},
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        (main / "preset.json").write_text(
            '{"protected": true}\n', encoding="utf-8"
        )
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
        with self.assertRaises(ValueError):
            self.store.delete("main")
        self.assertEqual(self.store.load("main").person_prompt, "Jarvis")

    def test_duplicate_capabilities_are_rejected(self):
        (self.root / "main" / "capabilities.json").write_text(
            json.dumps(
                {"modules": ["agents", "agents"], "actions": [], "handlers": []}
            ),
            encoding="utf-8",
        )
        with self.assertRaises(ValueError):
            self.store.load("main")

    def test_capability_can_be_added_without_duplicates(self):
        self.store.add_capability("main", "action", "say")
        self.store.add_capability("main", "action", "say")
        self.store.add_capability("main", "handler", "tick")
        preset = self.store.load("main")
        self.assertEqual(preset.modules, ("agents",))
        self.assertEqual(preset.actions, ("say",))
        self.assertEqual(preset.handlers, ("tick",))


if __name__ == "__main__":
    unittest.main()
