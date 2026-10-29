import json
import tempfile
import unittest
from pathlib import Path
from jarvis.infrastructure.context import MemoryStore


class MemoryStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = MemoryStore(Path(temporary.name))

    def record(self, **changes):
        return {
            "agent_id": "main",
            "preset_id": "main",
            "parent_id": None,
            "additional_instructions": "Individual instructions",
            "capabilities_add": [],
            "messages": [{"role": "user", "content": "saved"}],
            **changes,
        }

    def test_live_instances_are_keyed_by_identity_and_metadata_is_separate(self):
        self.store.save(self.record())
        directory = self.store.instances / "main"
        self.assertTrue((directory / "semantic.json").exists())
        self.assertEqual(
            self.store.load("main", "main")["messages"], self.record()["messages"]
        )
        self.assertNotIn(
            "messages", json.loads((directory / "instance.json").read_text())
        )
        self.assertEqual(self.store.load_all()[0]["agent_id"], "main")

    def test_deletion_retains_history_and_excludes_instance_from_restore(self):
        self.store.save(
            self.record(agent_id="agent-00001", preset_id="worker", parent_id="main")
        )
        self.store.delete("worker", "agent-00001")
        self.assertEqual(self.store.load_all(), [])
        self.assertTrue(self.store.load("worker", "agent-00001")["deleted"])
        self.assertEqual(
            self.store.records(include_deleted=True)[0]["agent_id"], "agent-00001"
        )

    def test_ids_are_never_reused_after_deletion(self):
        first = self.store.allocate_agent_id()
        self.store.save(self.record(agent_id=first))
        self.store.delete("main", first)
        self.assertNotEqual(MemoryStore(self.store.root).allocate_agent_id(), first)

    def test_partial_main_context_is_preserved_for_restore(self):
        directory = self.store.instances / "main"
        directory.mkdir(parents=True)
        (directory / "context.json").write_text('[{"role":"user","content":"saved"}]')
        self.assertTrue(self.store.has_existing_state())
        self.assertEqual(
            self.store.load_all()[0]["messages"], [{"role": "user", "content": "saved"}]
        )

    def test_corrupt_metadata_is_not_silently_discarded(self):
        self.store.save(self.record())
        path = self.store.instances / "main/instance.json"
        path.write_text("{broken")
        with self.assertRaises(ValueError):
            self.store.load_all()
        self.assertEqual(path.read_text(), "{broken")

    def test_escaping_ids_are_rejected_and_json_is_pretty_printed(self):
        with self.assertRaises(ValueError):
            self.store.save(self.record(agent_id="../escape"))
        self.store.save(self.record())
        for filename in ["instance.json", "context.json", "semantic.json"]:
            path = self.store.instances / "main" / filename
            self.assertEqual(
                path.read_text(),
                json.dumps(json.loads(path.read_text()), ensure_ascii=False, indent=2)
                + "\n",
            )
