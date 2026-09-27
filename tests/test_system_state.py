import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jarvis.capabilities.manager import CapabilityManager
from jarvis.core.protocol import Event
from jarvis.core.registry import ActionRegistry, EventRegistry
from jarvis.core.runtime import AgentManager, EventBus, register_core_protocol
from jarvis.infrastructure.automations import AutomationStore
from jarvis.infrastructure.context import MemoryStore
from jarvis.infrastructure.debug import Debugger
from jarvis.infrastructure.runtime_layout import ensure_runtime_layout
from jarvis.presets import PresetStore
from test_runtime import _Client, _Response, _wait
import fixtures


class SystemStateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = fixtures.write_jarvis_root(Path(temporary.name))
        self.actions, self.events = ActionRegistry(), EventRegistry()
        register_core_protocol(self.actions, self.events)
        self.bus = EventBus(self.events, debug=Debugger(enabled=False))
        self.capabilities = CapabilityManager(self.bus, self.actions, self.events, root=self.root)
        self.memory = MemoryStore(self.root / "memory")
        self.manager = AgentManager(model="test", client=_Client([]), actions=self.actions, events=self.events, bus=self.bus, capabilities=self.capabilities, presets=PresetStore(self.root / "presets"), master_prompt="environment", memory=self.memory)
        self.addCleanup(self.manager.shutdown)
        self.main = self.manager.spawn_root(name="main", preset="main")

    def invoke(self, name, data):
        context = SimpleNamespace(agent_id="main", action_id=name, agent_manager=self.capabilities._agent_api, capabilities=self.capabilities, metadata={"preset": "main"})
        return self.actions.require(name).run(data, context)

    def test_preset_edit_preserves_existing_instance_and_restore_personality(self):
        child_id = self.manager.spawn(parent_id="main", name="old", preset="worker")["agent_id"]
        child = self.manager.require_agent(child_id)
        original = child.person_prompt
        result = self.invoke("edit_preset", {"preset_id": "worker", "person_prompt": "New personality.", "actions": [], "handlers": [], "modules": []})
        self.assertEqual(result["status"], "edited")
        self.assertEqual(child.person_prompt, original)
        self.assertIn("say", child.standalone_actions())
        new_id = self.manager.spawn(parent_id="main", name="new", preset="worker")["agent_id"]
        self.assertEqual(self.manager.require_agent(new_id).person_prompt, "New personality.")
        record = self.memory.load("worker", child_id)
        self.assertEqual(record["person_prompt"], original)
        self.manager.delete(agent_id=child_id)
        restored = self.manager._spawn_record(record, primary=False)
        self.assertEqual(restored.person_prompt, original)

    def test_remove_preset_recursively_deletes_agents_context_and_memory(self):
        parent_id = self.manager.spawn(parent_id="main", name="parent", preset="worker")["agent_id"]
        self.manager.presets.create("descendant", "Descendant", {"actions": [], "handlers": [], "modules": []})
        child_id = self.manager.spawn(parent_id=parent_id, name="child", preset="descendant")["agent_id"]
        parent = self.manager.require_agent(parent_id)
        result = self.invoke("remove_preset", {"preset_id": "worker"})
        self.assertEqual(result["status"], "removed")
        self.assertNotIn(parent_id, self.manager.agents)
        self.assertNotIn(child_id, self.manager.agents)
        self.assertEqual(parent.history, [])
        self.manager.persist_agent(parent)
        self.assertFalse(self.memory.agent_dir("worker", parent_id).exists())
        self.assertFalse(self.memory.agent_dir("descendant", child_id).exists())
        self.assertFalse((self.root / "presets" / "worker").exists())

    def test_messages_are_system_actions_for_every_agent(self):
        child_id = self.manager.spawn(parent_id="main", name="child", preset="worker")["agent_id"]
        specs = self.manager.require_agent(child_id)._contract()[0]
        self.assertIn("send_message_to_agent", specs)
        self.assertNotIn("delete_agent", specs)

    def test_capability_management_defaults_to_module_manager(self):
        self.manager.presets.create("module_manager", "Module manager", {"actions": [], "handlers": [], "modules": []})
        agent_id = self.manager.spawn(parent_id="main", name="modules", preset="module_manager")["agent_id"]
        specs = self.manager.require_agent(agent_id)._contract()[0]
        for action_id in ("list_capabilities", "capability_info", "toggle_capability"):
            self.assertIn(action_id, specs)
            self.assertNotIn(action_id, self.main._contract()[0])

    def test_concurrent_automation_creations_and_edit_preserve_other_rules(self):
        store = self.manager.automations
        def create(index):
            store.create({"event": {"type": "tick", "data": {"index": index}}, "actions": [{"action_id": "speech", "data": {"text": str(index)}}]})
        threads = [threading.Thread(target=create, args=(index,)) for index in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        rules = store.identified()
        self.assertEqual(len(rules), 20)
        self.assertEqual(len({item["automation_id"] for item in rules}), 20)
        identifier = rules[0]["automation_id"]
        replacement = {"event": {"type": "new", "data": {}}, "actions": [{"action_id": "speech", "data": {"text": "edited"}}]}
        store.edit(identifier, replacement)
        self.assertEqual(len(store.list()), 20)
        store.remove_id(identifier)
        self.assertEqual(len(store.list()), 19)

    def test_corrupted_startup_state_is_reported_and_preserved(self):
        global_file = self.root / "capability_state.json"
        disabled = self.root / "presets" / "main" / "disabled_capabilities.json"
        global_file.write_text("broken-global", encoding="utf-8")
        disabled.write_text("broken-disabled", encoding="utf-8")
        with patch("jarvis.infrastructure.runtime_layout.logger") as log:
            ensure_runtime_layout(self.root)
        self.assertTrue(log.error.called)
        self.assertEqual(global_file.read_text(), "broken-global")
        self.assertEqual(disabled.read_text(), "broken-disabled")

    def test_interrupt_closes_stream_and_ignores_late_chunks(self):
        opened, closed = threading.Event(), threading.Event()
        class Stream:
            def __iter__(self):
                opened.set()
                closed.wait(3)
                yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content='{"actions":[{"action_id":"no_action","call_id":"late","data":{}}]}', refusal=None))])
            def close(self):
                closed.set()
        child_id = self.manager.spawn(parent_id="main", name="child", preset="worker")["agent_id"]
        child = self.manager.require_agent(child_id)
        counter = [0]
        def request(**kwargs):
            self.assertTrue(kwargs["stream"])
            counter[0] += 1
            return Stream() if counter[0] == 1 else _Response('{"actions":[{"action_id":"no_action","call_id":"fresh","data":{}}]}')
        self.manager.client.chat.completions.create = request
        self.bus.publish(Event("tick.event", {"text": "first"}, target=child_id, handler_id="tick"))
        self.assertTrue(opened.wait(2))
        self.manager.interrupt(agent_id=child_id, requester_id="main")
        self.assertTrue(closed.wait(1))
        self.bus.publish(Event("tick.event", {"text": "second"}, target=child_id, handler_id="tick"))
        self.assertTrue(_wait(lambda: "fresh" in str(child.history)))
        self.assertNotIn('"call_id":"late"', str(child.history))

    def test_delete_waits_for_inflight_save_then_removes_all_memory(self):
        child_id = self.manager.spawn(parent_id="main", name="child", preset="worker")["agent_id"]
        child = self.manager.require_agent(child_id)
        entered, release = threading.Event(), threading.Event()
        original = self.memory.save
        def slow_save(record):
            entered.set()
            release.wait(2)
            original(record)
        with patch.object(self.memory, "save", side_effect=slow_save):
            saver = threading.Thread(target=self.manager.persist_agent, args=(child,))
            saver.start()
            self.assertTrue(entered.wait(1))
            deleter = threading.Thread(target=lambda: self.manager.delete(agent_id=child_id))
            deleter.start()
            release.set()
            saver.join(2)
            deleter.join(2)
        self.assertFalse(saver.is_alive())
        self.assertFalse(deleter.is_alive())
        self.manager.persist_agent(child)
        self.assertFalse(self.memory.agent_dir("worker", child_id).exists())
        self.assertEqual(child.history, [])

    def test_stream_fragments_do_not_execute_before_complete_json(self):
        partial, release = threading.Event(), threading.Event()
        class Stream:
            def __iter__(self):
                yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content='{"actions":[{"action_id":"say",', refusal=None))])
                partial.set()
                release.wait(2)
                yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content='"call_id":"stream-call","data":{"text":"streamed"}}]}', refusal=None))])
            def close(self):
                release.set()
        counter = [0]
        def request(**kwargs):
            counter[0] += 1
            return Stream() if counter[0] == 1 else _Response('{"actions":[{"action_id":"no_action","call_id":"done","data":{}}]}')
        self.manager.client.chat.completions.create = request
        self.bus.publish(Event("tick.event", {"text": "start"}, target="main", handler_id="tick"))
        self.assertTrue(partial.wait(2))
        self.assertFalse(self.manager.results.has_pending("main", "stream-call"))
        self.assertNotIn('"text":"streamed"', str(self.main.history))
        release.set()
        self.assertTrue(_wait(lambda: '"call_id":"stream-call"' in str(self.main.history)))


if __name__ == "__main__":
    unittest.main()
