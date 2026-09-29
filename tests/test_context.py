import json
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
                {"role": "user", "content": '{"event_id":"x","data":{}}'},
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
        self.assertEqual(loaded["messages"], self.record()["messages"])
        directory = self.root / "main"
        self.assertTrue((directory / "instance.json").is_file())
        self.assertTrue((directory / "context.json").is_file())

    def test_subagent_lives_in_own_directory(self):
        self.store.save(self.record(agent_id="agent-001", parent_id="main"))
        loaded = self.store.load("main", "agent-001")
        self.assertEqual(loaded["parent_id"], "main")
        self.assertTrue((self.root / "main" / "agent-001" / "instance.json").is_file())
        self.store.delete("main", "agent-001")
        self.assertFalse((self.root / "main" / "agent-001").exists())

    def test_main_delete_keeps_subagents(self):
        self.store.save(self.record())
        self.store.save(self.record(agent_id="agent-001", parent_id="main"))
        self.store.delete("main", "main")
        self.assertFalse((self.root / "main" / "instance.json").exists())
        self.assertTrue((self.root / "main" / "agent-001" / "instance.json").exists())

    def test_system_message_is_persisted_as_model_context(self):
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
            loaded["messages"],
            [
                {"role": "system", "content": "ignored"},
                {"role": "user", "content": "kept"},
            ],
        )

    def test_modalities_use_file_id_references_without_names_or_base64(self):
        record = self.record(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": '{"event_id":"image_notice","data":{}}',
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
        parts = self.root / "main" / "files"
        self.assertEqual(
            sorted(item.name for item in parts.iterdir()),
            ["file_000001.png", "file_000002.pdf"],
        )
        self.assertEqual((parts / "file_000002.pdf").read_bytes(), b"ABC")
        raw = (self.root / "main" / "context.json").read_text(encoding="utf-8")
        self.assertNotIn("base64", raw)
        self.assertNotIn('"name"', raw)
        self.assertIn('"file_id": "file_000001"', raw)

        loaded = self.store.load("main", "main")
        self.assertEqual(
            loaded["messages"][0]["content"][1]["image_url"]["url"],
            record["messages"][0]["content"][1]["image_url"]["url"],
        )
        self.assertEqual(
            loaded["messages"][0]["content"][2]["file"]["filename"], "file_000002.pdf"
        )

    def test_unnamed_parts_get_global_file_ids(self):
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
        self.assertTrue((self.root / "main" / "files" / "file_000001.png").is_file())

    def test_missing_files_are_ignored(self):
        self.assertEqual(self.store.load_all(), [])
        self.assertIsNone(self.store.load("main", "main"))

    def test_broken_files_are_ignored(self):
        broken = self.root / "main"
        broken.mkdir()
        (broken / "instance.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(self.store.load_all(), [])
        with self.assertRaises(ValueError):
            self.store.load("main", "main")

    def test_delete_removes_agent_directory(self):
        self.store.save(self.record(agent_id="agent-001"))
        self.store.delete("main", "agent-001")
        self.assertFalse((self.root / "main" / "agent-001").exists())
        self.store.delete("main", "agent-001")

    def test_invalid_agent_id_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.save(self.record(agent_id="../escape"))

    def test_failed_context_save_keeps_previous_files(self):
        original = self.record(
            name="old", messages=[{"role": "user", "content": "old"}]
        )
        self.store.save(original)
        previous = (self.root / "main" / "instance.json").read_bytes()
        real_write = self.store._write_json

        def fail_context(path, value):
            if path.name == "context.json":
                raise OSError("disk full")
            return real_write(path, value)

        with patch.object(self.store, "_write_json", side_effect=fail_context):
            with self.assertRaises(OSError):
                self.store.save(
                    self.record(
                        name="new", messages=[{"role": "user", "content": "new"}]
                    )
                )
        self.assertEqual((self.root / "main" / "instance.json").read_bytes(), previous)
        self.assertEqual(self.store.load("main", "main")["name"], "old")
        self.assertEqual(
            self.store.load("main", "main")["messages"][0]["content"], "old"
        )

    def test_failed_instance_save_rolls_back_context_too(self):
        self.store.save(
            self.record(name="old", messages=[{"role": "user", "content": "old"}])
        )
        directory = self.root / "main"
        original_instance = (directory / "instance.json").read_bytes()
        original_context = (directory / "context.json").read_bytes()
        actual_write = self.store._write_json

        def fail_instance(path, value):
            if path.name == "instance.json":
                raise OSError("disk full")
            return actual_write(path, value)

        with patch.object(self.store, "_write_json", side_effect=fail_instance):
            with self.assertRaises(OSError):
                self.store.save(
                    self.record(
                        name="new", messages=[{"role": "user", "content": "new"}]
                    )
                )
        loaded = self.store.load("main", "main")
        self.assertEqual(loaded["name"], "old")
        self.assertEqual(loaded["messages"][0]["content"], "old")
        self.assertEqual((directory / "instance.json").read_bytes(), original_instance)
        self.assertEqual((directory / "context.json").read_bytes(), original_context)

    def test_interrupted_save_journal_restores_previous_complete_state(self):
        self.store.save(
            self.record(name="old", messages=[{"role": "user", "content": "old"}])
        )
        directory = self.root / "main"
        old_instance = self.store._read_json(directory / "instance.json")
        old_context = self.store._read_json(directory / "context.json")
        self.store._write_json(
            directory / ".save-journal.json",
            {"instance": old_instance, "context": old_context},
        )
        self.store._write_json(
            directory / "instance.json", {**old_instance, "name": "new"}
        )
        self.store._write_json(
            directory / "context.json", [{"role": "user", "content": "new"}]
        )
        restored = MemoryStore(self.root).load("main", "main")
        self.assertEqual(restored["name"], "old")
        self.assertEqual(restored["messages"][0]["content"], "old")
        self.assertFalse((directory / ".save-journal.json").exists())

    def test_duplicate_original_attachment_names_get_distinct_global_ids(self):
        def message(payload):
            return {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{payload}",
                            "name": "cat.jpg",
                        },
                    }
                ],
            }

        record = self.record(
            messages=[message("QUFB"), message("QkJC"), message("Q0ND")]
        )
        self.store.save(record)
        current = self.root / "main"
        self.assertEqual(
            sorted(p.name for p in (current / "files").iterdir()),
            ["file_000001.jpg", "file_000002.jpg", "file_000003.jpg"],
        )
        self.assertEqual((current / "files" / "file_000001.jpg").read_bytes(), b"AAA")
        self.assertEqual((current / "files" / "file_000002.jpg").read_bytes(), b"BBB")
        record["messages"].append(message("RERE"))
        self.store.save(record)
        refs = [
            entry["content"][0]["image_url"]["file_id"]
            for entry in self.store._read_json(current / "context.json")
        ]
        self.assertEqual(
            refs, ["file_000001", "file_000002", "file_000003", "file_000004"]
        )

    def test_file_ids_are_global_across_agents_and_restarts(self):
        def record(agent_id, payload):
            return self.record(
                agent_id=agent_id,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/png;base64,{payload}",
                                    "name": "same.png",
                                },
                            }
                        ],
                    }
                ],
            )

        self.store.save(record("main", "QUFB"))
        other = MemoryStore(self.root)
        other.save(record("agent-001", "QkJC"))
        main_ref = self.store._read_json(self.root / "main/context.json")[0]["content"][
            0
        ]
        child_ref = self.store._read_json(self.root / "main/agent-001/context.json")[0][
            "content"
        ][0]
        self.assertEqual(
            main_ref, {"type": "image_url", "image_url": {"file_id": "file_000001"}}
        )
        self.assertEqual(
            child_ref, {"type": "image_url", "image_url": {"file_id": "file_000002"}}
        )
        self.assertEqual(
            json.loads((self.root / "file_index.json").read_text()), {"next_file_id": 3}
        )
        other.save({**record("agent-002", "Q0ND"), "preset": "module_manager"})
        third_ref = self.store._read_json(
            self.root / "module_manager/agent-002/context.json"
        )[0]["content"][0]
        self.assertEqual(
            third_ref, {"type": "image_url", "image_url": {"file_id": "file_000003"}}
        )

    def test_all_binary_modalities_use_only_file_id_references(self):
        message = {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,QUFB"},
                },
                {
                    "type": "input_audio",
                    "input_audio": {"data": "QkJC", "format": "ogg"},
                },
                {
                    "type": "video_url",
                    "video_url": {"url": "data:video/mp4;base64,Q0ND"},
                },
                {
                    "type": "file",
                    "file": {
                        "filename": "report.pdf",
                        "file_data": "data:application/pdf;base64,RERE",
                    },
                },
            ],
        }
        self.store.save(self.record(messages=[message]))
        saved = self.store._read_json(self.root / "main/context.json")[0]["content"]
        self.assertEqual(
            saved,
            [
                {"type": "image_url", "image_url": {"file_id": "file_000001"}},
                {"type": "input_audio", "input_audio": {"file_id": "file_000002"}},
                {"type": "video_url", "video_url": {"file_id": "file_000003"}},
                {"type": "file", "file": {"file_id": "file_000004"}},
            ],
        )
        restored = self.store.load("main", "main")["messages"][0]["content"]
        self.assertEqual(restored[1]["input_audio"]["data"], "QkJC")
        self.assertEqual(restored[2]["video_url"]["url"], "data:video/mp4;base64,Q0ND")
        self.assertEqual(
            restored[3]["file"]["file_data"], "data:application/pdf;base64,RERE"
        )

    def test_instance_contains_one_disabled_capabilities_list(self):
        self.store.save(
            self.record(
                disabled_modules=["telegram"],
                disabled_actions=["say"],
                disabled_handlers=["tick"],
            )
        )
        instance = self.store._read_json(self.root / "main/instance.json")
        self.assertEqual(
            instance["disabled_capabilities"],
            ["module:telegram", "action:say", "handler:tick"],
        )
        self.assertFalse(
            any(
                key.startswith("disabled_") and key != "disabled_capabilities"
                for key in instance
            )
        )
        self.assertEqual(
            self.store.load("main", "main")["disabled_modules"], ["telegram"]
        )

    def test_persisted_json_is_pretty_printed(self):
        self.store.save(self.record())
        for path in (self.root / "main/instance.json", self.root / "main/context.json"):
            value = json.loads(path.read_text())
            self.assertEqual(
                path.read_text(), json.dumps(value, ensure_ascii=False, indent=2) + "\n"
            )

    def test_only_flat_instance_and_context_files_are_retained(self):
        for index in range(4):
            self.store.save(self.record(name=f"version-{index}"))
        directory = self.root / "main"
        self.assertTrue((directory / "instance.json").is_file())
        self.assertTrue((directory / "context.json").is_file())
        self.assertEqual(
            sorted(path.name for path in directory.iterdir()),
            ["context.json", "files", "instance.json"],
        )
        self.assertEqual(self.store.load("main", "main")["name"], "version-3")


if __name__ == "__main__":
    unittest.main()
