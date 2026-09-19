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
        (main / "modules.json").write_text('["agents"]\n', encoding="utf-8")
        (main / "preset.json").write_text(
            '{"protected": true}\n', encoding="utf-8"
        )
        self.store = PresetStore(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def test_main_person_prompt_cannot_be_replaced_or_deleted(self):
        with self.assertRaises(ValueError):
            self.store.create("main", "changed", ["agents"])
        with self.assertRaises(ValueError):
            self.store.delete("main")
        self.assertEqual(self.store.load("main").person_prompt, "Jarvis")

    def test_module_can_only_be_added_once(self):
        self.store.add_module("main", "clock")
        self.store.add_module("main", "clock")
        modules = json.loads(
            (self.root / "main" / "modules.json").read_text(encoding="utf-8")
        )
        self.assertEqual(modules, ["agents", "clock"])


if __name__ == "__main__":
    unittest.main()
