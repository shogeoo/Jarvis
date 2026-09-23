import tempfile
import unittest
from unittest.mock import patch
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
            "name": "Jarvis",
            "preset": "main",
            "parent_id": None,
            "modules": ["alpha", "beta"],
            "actions": ["say", "echo"],
            "handlers": ["tick", "monitor"],
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
        self.assertEqual(loaded["actions"], ["echo", "say"])
        self.assertEqual(loaded["handlers"], ["monitor", "tick"])
        self.assertEqual(
            loaded["messages"], self.record()["messages"]
        )
        current = self.store._current_dir(self.root / "main")
        self.assertTrue((current / "agent.json").is_file())
        self.assertTrue((current / "context.json").is_file())

    def test_subagent_lives_in_own_directory(self):
        self.store.save(self.record(agent_id="agent-001", parent_id="main"))
        loaded = self.store.load("main", "agent-001")
        self.assertEqual(loaded["parent_id"], "main")
        self.assertTrue((self.store._current_dir(self.root / "main" / "agent-001") / "agent.json").is_file())
        self.store.delete("main", "agent-001")
        self.assertFalse((self.root / "main" / "agent-001").exists())

    def test_main_delete_keeps_subagents(self):
        self.store.save(self.record())
        self.store.save(self.record(agent_id="agent-001", parent_id="main"))
        self.store.delete("main", "main")
        self.assertFalse((self.root / "main" / "current.json").exists())
        self.assertTrue((self.store._current_dir(self.root / "main" / "agent-001") / "agent.json").exists())

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

    def test_modalities_survive_restart_with_original_names(self):
        record = self.record(
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
                            "image_url": {
                                "url": "data:image/png;base64,AAAA",
                                "name": "2026-09-20-005028_jarvis.png",
                            },
                        },
                        {
                            "type": "file",
                            "file": {
                                "filename": "отчёт.pdf",
                                "file_data": "data:application/pdf;base64,QUJD",
                            },
                        },
                    ],
                }
            ]
        )
        self.store.save(record)
        parts = self.store._current_dir(self.root / "main") / "parts"
        self.assertEqual(
            sorted(item.name for item in parts.iterdir()),
            ["2026-09-20-005028_jarvis.png", "отчёт.pdf"],
        )
        self.assertEqual((parts / "отчёт.pdf").read_bytes(), b"ABC")
        raw = (self.store._current_dir(self.root / "main") / "context.json").read_text(encoding="utf-8")
        self.assertNotIn("base64", raw)
        self.assertIn('"file": "parts/2026-09-20-005028_jarvis.png"', raw)

        loaded = self.store.load("main", "main")
        self.assertEqual(loaded["messages"], record["messages"])

    def test_unnamed_parts_get_content_addressed_names(self):
        record = self.record(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64,AAAA"},
                        }
                    ],
                }
            ]
        )
        self.store.save(record)
        loaded = self.store.load("main", "main")
        self.assertEqual(loaded["messages"], record["messages"])
        self.assertTrue(self.store._current_dir(self.root / "main").joinpath("parts").is_dir())

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

    def test_failed_snapshot_keeps_previous_complete_generation(self):
        original = self.record(name="old", messages=[{"role": "user", "content": "old"}])
        self.store.save(original)
        previous = (self.root / "main" / "current.json").read_bytes()
        real_write = self.store._write_json
        def fail_context(path, value):
            if path.name == "context.json":
                raise OSError("disk full")
            return real_write(path, value)
        with patch.object(self.store, "_write_json", side_effect=fail_context):
            with self.assertRaises(OSError):
                self.store.save(self.record(name="new", messages=[{"role": "user", "content": "new"}]))
        self.assertEqual((self.root / "main" / "current.json").read_bytes(), previous)
        self.assertEqual(self.store.load("main", "main")["name"], "old")
        self.assertEqual(self.store.load("main", "main")["messages"][0]["content"], "old")

    def test_duplicate_original_attachment_names_keep_historical_bytes(self):
        def message(payload):
            return {"role": "user", "content": [{"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{payload}", "name": "cat.jpg"}}]}
        record = self.record(messages=[message("QUFB"), message("QkJC"), message("Q0ND")])
        self.store.save(record)
        current = self.store._current_dir(self.root / "main")
        self.assertEqual(sorted(p.name for p in (current / "parts").iterdir()), ["cat(1).jpg", "cat(2).jpg", "cat.jpg"])
        self.assertEqual((current / "parts" / "cat.jpg").read_bytes(), b"AAA")
        self.assertEqual((current / "parts" / "cat(1).jpg").read_bytes(), b"BBB")
        self.assertEqual(self.store.load("main", "main")["messages"], record["messages"])
        record["messages"].append(message("RERE"))
        self.store.save(record)
        current = self.store._current_dir(self.root / "main")
        refs = [entry["content"][0]["jarvis_part"]["file"] for entry in self.store._read_json(current / "context.json")]
        self.assertEqual(refs, ["parts/cat.jpg", "parts/cat(1).jpg", "parts/cat(2).jpg", "parts/cat(3).jpg"])


if __name__ == "__main__":
    unittest.main()
