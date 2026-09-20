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

    def test_round_trip_keeps_metadata_and_messages(self):
        record = {
            "agent_id": "main",
            "name": "main",
            "preset": "main",
            "parent_id": None,
            "modules": ["speech_output", "agents"],
            "messages": [
                {"role": "user", "content": '{"type":"x","data":{}}'},
                {"role": "assistant", "content": '{"actions":[]}'},
            ],
        }
        self.store.save(record)
        loaded = self.store.load("main")
        self.assertEqual(loaded["modules"], ["agents", "speech_output"])
        self.assertEqual(loaded["messages"], record["messages"])
        self.assertTrue((self.root / "main.json").is_file())

    def test_system_messages_are_not_restored(self):
        self.store.save(
            {
                "agent_id": "main",
                "messages": [
                    {"role": "system", "content": "ignored"},
                    {"role": "user", "content": "kept"},
                ],
            }
        )
        loaded = self.store.load("main")
        self.assertEqual(
            loaded["messages"], [{"role": "user", "content": "kept"}]
        )

    def test_broken_file_is_ignored(self):
        (self.root / "broken.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(self.store.load_all(), [])
        self.assertIsNone(self.store.load("broken"))

    def test_delete_removes_file(self):
        self.store.save({"agent_id": "agent-001", "messages": []})
        self.store.delete("agent-001")
        self.assertFalse((self.root / "agent-001.json").exists())
        self.store.delete("agent-001")

    def test_multimodal_content_is_preserved(self):
        record = {
            "agent_id": "main",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "shot"},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "data:image/png;base64,AAAA"
                            },
                        },
                    ],
                }
            ],
        }
        self.store.save(record)
        self.assertEqual(
            self.store.load("main")["messages"], record["messages"]
        )

    def test_invalid_agent_id_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.save({"agent_id": "../escape", "messages": []})


if __name__ == "__main__":
    unittest.main()
