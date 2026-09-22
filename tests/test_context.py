import tempfile
import unittest
from pathlib import Path

from jarvis.infrastructure.context import MemoryStore


class MemoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = MemoryStore(self.root)

    def record(self, **overrides):
        record = {
            "agent_id": "main",
            "name": "main",
            "preset": "main",
            "parent_id": None,
            "modules": ["alpha", "beta"],
            "actions": ["say", "echo.repeat"],
            "handlers": ["tick", "echo.monitor"],
            "messages": [
                {"role": "user", "content": '{"type":"x","data":{}}'},
                {"role": "assistant", "content": '{"actions":[]}'},
            ],
        }
        record.update(overrides)
        return record

    def test_round_trip_keeps_metadata_and_messages(self):
        self.store.save(self.record())
        loaded = self.store.load("main", "main")
        self.assertEqual(loaded["modules"], ["alpha", "beta"])
        self.assertEqual(loaded["actions"], ["echo.repeat", "say"])
        self.assertEqual(loaded["handlers"], ["echo.monitor", "tick"])
        self.assertEqual(
            loaded["messages"], self.record()["messages"]
        )
        self.assertTrue((self.root / "main" / "agent.json").is_file())
        self.assertTrue((self.root / "main" / "context.json").is_file())

    def test_subagent_lives_in_own_directory(self):
        self.store.save(self.record(agent_id="agent-001", parent_id="main"))
        loaded = self.store.load("main", "agent-001")
        self.assertEqual(loaded["parent_id"], "main")
        self.assertTrue((self.root / "main" / "agent-001" / "agent.json").is_file())
        self.store.delete("main", "agent-001")
        self.assertFalse((self.root / "main" / "agent-001").exists())

    def test_main_delete_keeps_subagents(self):
        self.store.save(self.record())
        self.store.save(self.record(agent_id="agent-001", parent_id="main"))
        self.store.delete("main", "main")
        self.assertFalse((self.root / "main" / "agent.json").exists())
        self.assertTrue((self.root / "main" / "agent-001" / "agent.json").exists())

    def test_system_messages_are_not_restored(self):
        self.store.save(
            self.record(
                messages=[
                    {"role": "system", "content": "ignored"},
                    {"role": "user", "content": "kept"},
                ],
            )
        )
        loaded = self.store.load("main", "main")
        self.assertEqual(
            loaded["messages"], [{"role": "user", "content": "kept"}]
        )

    def test_modalities_do_not_survive_restart(self):
        self.store.save(
            self.record(
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": '{"type":"screenshot","data":{}}',
                            },
                            {
                                "type": "image_url",
                                "image_url": {"url": "data:image/png;base64,AAAA"},
                            },
                        ],
                    }
                ],
            )
        )
        loaded = self.store.load("main", "main")
        self.assertEqual(
            loaded["messages"],
            [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": '{"type":"screenshot","data":{}}',
                        }
                    ],
                }
            ],
        )
        raw = (self.root / "main" / "context.json").read_text(encoding="utf-8")
        self.assertNotIn("base64", raw)

    def test_missing_files_are_ignored(self):
        self.assertEqual(self.store.load_all(), [])
        self.assertIsNone(self.store.load("main", "main"))

    def test_broken_files_are_ignored(self):
        broken = self.root / "main"
        broken.mkdir()
        (broken / "agent.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(self.store.load_all(), [])
        self.assertIsNone(self.store.load("main", "main"))

    def test_delete_removes_agent_directory(self):
        self.store.save(self.record(agent_id="agent-001"))
        self.store.delete("main", "agent-001")
        self.assertFalse((self.root / "main" / "agent-001").exists())
        self.store.delete("main", "agent-001")

    def test_invalid_agent_id_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.save(self.record(agent_id="../escape"))


if __name__ == "__main__":
    unittest.main()
