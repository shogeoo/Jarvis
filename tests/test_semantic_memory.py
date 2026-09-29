"""Semantic memory and the system-only primary-agent memory actions."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import fixtures
from jarvis.core.prompts import agent_system_prompt
from jarvis.core.registry import ActionRegistry, EventRegistry
from jarvis.core.runtime import register_core_protocol
from jarvis.infrastructure.context import MemoryStore
from jarvis.infrastructure.model_capabilities import ModelCapabilities
from jarvis.infrastructure.semantic_memory import SemanticMemory
from jarvis.infrastructure.runtime_layout import ensure_runtime_layout
import test_runtime as runtime_tests


class SemanticMemoryTests(unittest.TestCase):
    def test_write_edit_delete_survive_reopen_and_keep_monotonic_ids(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / ".jarvis/memory/main/semantic.json"
            memory = SemanticMemory(path)
            self.assertEqual(memory.snapshot(), {"entries": []})
            first = memory.write("Georgiy uses Arch Linux and Windows dual boot.")
            second = memory.write("Georgiy prefers Russian.")
            self.assertEqual((first, second), ("mem_000001", "mem_000002"))
            memory.edit(first, "Georgiy uses Arch Linux and Windows in dual boot.")
            memory.delete(second)
            self.assertEqual(
                SemanticMemory(path).snapshot(),
                {
                    "entries": [
                        {
                            "id": first,
                            "content": "Georgiy uses Arch Linux and Windows in dual boot.",
                        }
                    ]
                },
            )
            self.assertEqual(
                SemanticMemory(path).write("Another confirmed fact."), "mem_000002"
            )

    def test_unknown_id_or_empty_content_preserves_existing_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            memory = SemanticMemory(Path(temporary) / "memory/semantic.json")
            memory.write("Known fact")
            original = memory.path.read_bytes()
            for operation in (
                lambda: memory.edit("missing", "new"),
                lambda: memory.delete("missing"),
                lambda: memory.write(" "),
            ):
                with self.assertRaises(ValueError):
                    operation()
                self.assertEqual(memory.path.read_bytes(), original)

    def test_runtime_layout_creates_missing_semantic_file_without_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / ".jarvis"
            ensure_runtime_layout(root)
            path = root / "memory/main/semantic.json"
            self.assertEqual(json.loads(path.read_text()), {"entries": []})
            path.write_text('{"entries":[{"id":"mem_000001","content":"Existing"}]}')
            ensure_runtime_layout(root)
            self.assertEqual(
                json.loads(path.read_text())["entries"][0]["content"], "Existing"
            )
            self.assertFalse(MemoryStore(root / "memory").has_existing_state())

    def test_memory_actions_are_core_primary_only_and_prompt_rebuilds(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = fixtures.write_jarvis_root(Path(temporary) / ".jarvis")
            actions, events = ActionRegistry(), EventRegistry()
            register_core_protocol(actions, events)
            identifiers = {"memory_write", "memory_edit", "memory_delete"}
            primary = actions.for_capabilities(
                modules=set(), actions=set(), primary=True
            )
            other = actions.for_capabilities(modules=set(), actions=set())
            self.assertTrue(identifiers <= primary.keys())
            self.assertTrue(identifiers.isdisjoint(other))
            memory = SemanticMemory(root / "memory/main/semantic.json")
            context = SimpleNamespace(
                agent_manager=SimpleNamespace(
                    _manager=SimpleNamespace(semantic_memory=memory)
                )
            )
            before = agent_system_prompt(
                "Jarvis",
                "Environment",
                primary,
                {},
                {},
                model_capabilities=ModelCapabilities("gpt-6-luna", ("text", "image")),
                semantic_memory=memory.snapshot(),
            )
            identifier = primary["memory_write"].run(
                {"content": "Georgiy uses dual boot."}, context
            )["id"]
            after = agent_system_prompt(
                "Jarvis",
                "Environment",
                primary,
                {},
                {},
                model_capabilities=ModelCapabilities("gpt-6-luna", ("text", "image")),
                semantic_memory=memory.snapshot(),
            )
            self.assertNotIn("Georgiy uses dual boot.", before)
            self.assertIn('"content": "Georgiy uses dual boot."', after)
            self.assertIn('"id": "mem_000001"', after)
            self.assertIn("MODEL INFO:\nModel ID: gpt-6-luna", after)
            self.assertEqual(
                primary["memory_edit"].run(
                    {"id": identifier, "content": "Georgiy prefers Russian."}, context
                ),
                {"status": "updated"},
            )
            self.assertEqual(
                primary["memory_delete"].run({"id": identifier}, context),
                {"status": "deleted"},
            )
            self.assertEqual(memory.snapshot(), {"entries": []})
            without_memory = agent_system_prompt(
                "Other",
                "Environment",
                other,
                {},
                {},
                model_capabilities=ModelCapabilities("gpt-6-luna", ("text",)),
            )
            self.assertNotIn("MEMORY:", without_memory)

    def test_next_inference_reads_memory_written_by_previous_action(self):
        fixture = runtime_tests.RuntimeTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.addCleanup(fixture.tearDown)
        client = runtime_tests._Client(
            [
                '{"actions":[{"action_id":"memory_write","call_id":"write-1","data":{"content":"Georgiy uses dual boot."}}]}',
                '{"actions":[{"action_id":"no_action","call_id":"wait-1","data":{}}]}',
            ]
        )
        manager = fixture.manager(client)
        agent = manager.spawn_root(name="main", preset="main")
        fixture.publish(agent, "remember")
        self.assertTrue(
            runtime_tests._wait(lambda: len(client.chat.completions.calls) >= 2)
        )
        self.assertNotIn(
            "Georgiy uses dual boot.",
            client.chat.completions.calls[0]["messages"][0]["content"],
        )
        self.assertIn(
            "Georgiy uses dual boot.",
            client.chat.completions.calls[1]["messages"][0]["content"],
        )
        self.assertEqual(
            json.loads((fixture.root / "memory/main/semantic.json").read_text())[
                "entries"
            ][0]["id"],
            "mem_000001",
        )


if __name__ == "__main__":
    unittest.main()
