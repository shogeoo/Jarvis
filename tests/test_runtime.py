import copy
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from jarvis.capabilities.manager import CapabilityManager
from jarvis.core.protocol import Event
from jarvis.core.registry import ActionRegistry, EventRegistry
from jarvis.core.runtime import AgentManager, EventBus, register_core_protocol
from jarvis.infrastructure.context import MemoryStore
from jarvis.infrastructure.debug import Debugger
from jarvis.presets import PresetStore

import fixtures


class _Response:
    def __init__(self, content):
        message = type("Message", (), {"content": content, "refusal": None})()
        self.choices = [type("Choice", (), {"message": message})()]


class _Completions:
    def __init__(self, outputs, block=None):
        self.outputs = outputs
        self.block = block
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        index = len(self.calls) - 1
        if self.block:
            self.block(index)
        output = self.outputs[index] if index < len(self.outputs) else self.outputs[-1]
        return _Response(output)


class _Client:
    def __init__(self, outputs, block=None):
        completions = _Completions(outputs, block)
        self.chat = type("Chat", (), {"completions": completions})()


def _action_file(args_schema, result_schema, run_body):
    return (
        "from jarvis.capabilities import action_definition\n\n\n"
        f"def run(data, context):\n{run_body}\n\n\n"
        "def create_action():\n"
        "    return action_definition(\n"
        '        "test",\n'
        f"        {args_schema!r},\n"
        f"        {result_schema!r},\n"
        "        run,\n"
        "    )\n"
    )


EMPTY_SCHEMA = {
    "type": "object",
    "properties": {},
    "required": [],
    "additionalProperties": False,
}


def _no_action(action_id="done"):
    return (
        '{"actions":[{"action_id":'
        f"{json.dumps(action_id)},"
        '"type":"no_action","data":{}}]}'
    )


def _wait(predicate, timeout=2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = fixtures.write_jarvis_root(Path(temporary.name))
        self.presets = PresetStore(self.root / "presets")
        self.actions = ActionRegistry()
        self.events = EventRegistry()
        register_core_protocol(self.actions, self.events)
        self.bus = EventBus(self.events, debug=Debugger(enabled=False))
        self.managers = []

    def tearDown(self):
        for manager in self.managers:
            manager.shutdown()

    def manager(self, client, memory=None):
        capabilities = CapabilityManager(
            self.bus,
            self.actions,
            self.events,
            root=self.root,
            debug=Debugger(enabled=False),
        )
        manager = AgentManager(
            model="test",
            client=client,
            actions=self.actions,
            events=self.events,
            bus=self.bus,
            capabilities=capabilities,
            presets=self.presets,
            master_prompt="environment",
            debug=Debugger(enabled=False),
            memory=memory,
        )
        self.managers.append(manager)
        return manager

    def publish(self, agent, text):
        self.bus.publish(
            Event(
                type="tick.event",
                data={"text": text},
                target=agent.agent_id,
                handler_id="tick",
            )
        )

    def write_action(self, action_id, args_schema, result_schema, run_body):
        fixtures.write_action(
            self.root / "actions",
            action_id,
            _action_file(args_schema, result_schema, run_body),
        )

    def action_results(self, agent):
        return [
            json.loads(message["content"])
            for message in agent.history
            if message["role"] == "user"
            and '"action_result"' in message["content"]
        ]

    def test_actions_are_dispatched_in_array_order_with_results(self):
        client = _Client(
            [
                '{"actions":['
                '{"action_id":"say-1","type":"say","data":{"text":"one"}},'
                '{"action_id":"say-2","type":"say","data":{"text":"two"}}]}',
                _no_action("done-1"),
            ]
        )
        manager = self.manager(client)
        agent = manager.spawn_root(name="main", preset="main")
        self.publish(agent, "start")
        self.assertTrue(_wait(lambda: len(self.action_results(agent)) == 2))
        self.assertEqual(
            [item["data"]["text"] for item in self.action_results(agent)],
            ["one", "two"],
        )
        self.assertEqual(
            [item["action_id"] for item in self.action_results(agent)],
            ["say-1", "say-2"],
        )

    def test_dispatch_error_does_not_stop_later_actions(self):
        self.write_action(
            "fail",
            EMPTY_SCHEMA,
            EMPTY_SCHEMA,
            "    raise RuntimeError('dispatch failed')\n",
        )
        self.write_action(
            "next",
            EMPTY_SCHEMA,
            {
                "type": "object",
                "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"],
                "additionalProperties": False,
            },
            "    return {'ok': True}\n",
        )
        client = _Client(
            [
                '{"actions":['
                '{"action_id":"fail-1","type":"fail","data":{}},'
                '{"action_id":"next-1","type":"next","data":{}}]}',
                _no_action("done-1"),
            ]
        )
        manager = self.manager(client)
        agent = manager.spawn_root(name="main", preset="main")
        manager.enable_action(agent.agent_id, "fail")
        manager.enable_action(agent.agent_id, "next")
        self.publish(agent, "start")

        def snapshot():
            values = {}
            for call in client.chat.completions.calls:
                for message in call["messages"]:
                    if message["role"] != "user":
                        continue
                    item = json.loads(message["content"])
                    values[(item["type"], item.get("action_id"))] = item
            values = list(values.values())
            has_error = any(
                item["type"] == "capability_error" for item in values
            )
            has_result = any(
                item["type"] == "action_result"
                and item["action_id"] == "next-1"
                for item in values
            )
            return values if has_error and has_result else None

        deadline = time.time() + 5
        values = None
        while time.time() < deadline and values is None:
            values = snapshot()
            if values is None:
                time.sleep(0.01)
        self.assertIsNotNone(values)
        error = next(item for item in values if item["type"] == "capability_error")
        self.assertEqual(error["data"]["capability"], "action:fail")
        results = [item for item in values if item["type"] == "action_result"]
        self.assertEqual([item["action_id"] for item in results], ["next-1"])
        self.assertEqual(results[0]["data"], {"ok": True})

    def test_events_in_busy_batch_keep_arrival_order(self):
        thinking = threading.Event()
        release = threading.Event()

        def block(index):
            if index == 0:
                thinking.set()
                release.wait(2)

        client = _Client([_no_action("done-1"), _no_action("done-2")], block)
        manager = self.manager(client)
        agent = manager.spawn_root(name="main", preset="main")
        self.publish(agent, "first")
        self.assertTrue(thinking.wait(2))
        self.publish(agent, "external")
        release.set()
        self.assertTrue(_wait(lambda: len(client.chat.completions.calls) >= 2))
        values = [
            json.loads(message["content"])
            for message in client.chat.completions.calls[1]["messages"]
            if message["role"] == "user"
        ]
        self.assertEqual(
            [item["data"]["text"] for item in values[-2:]],
            ["first", "external"],
        )

    def test_structure_error_holds_external_events(self):
        correction = threading.Event()
        release = threading.Event()

        def block(index):
            if index == 1:
                correction.set()
                release.wait(2)

        client = _Client(
            [
                "not json",
                _no_action("done-1"),
                _no_action("done-2"),
            ],
            block,
        )
        manager = self.manager(client)
        agent = manager.spawn_root(name="main", preset="main")
        self.publish(agent, "first")
        self.assertTrue(correction.wait(2))
        self.publish(agent, "second")
        release.set()
        self.assertTrue(_wait(lambda: len(client.chat.completions.calls) >= 3))
        correction_messages = client.chat.completions.calls[1]["messages"]
        self.assertNotIn("second", json.dumps(correction_messages, ensure_ascii=False))
        self.assertEqual(
            json.loads(correction_messages[-1]["content"])["type"],
            "structure_error",
        )
        self.assertIn("second", json.dumps(client.chat.completions.calls[2]["messages"], ensure_ascii=False))

    def test_duplicate_action_id_is_a_structure_error(self):
        client = _Client(
            [
                '{"actions":['
                '{"action_id":"say-1","type":"say","data":{"text":"one"}},'
                '{"action_id":"say-1","type":"say","data":{"text":"two"}}]}',
                _no_action("done-1"),
            ]
        )
        manager = self.manager(client)
        agent = manager.spawn_root(name="main", preset="main")
        self.publish(agent, "start")
        self.assertTrue(_wait(lambda: len(client.chat.completions.calls) >= 2))
        values = [
            json.loads(message["content"])
            for message in client.chat.completions.calls[1]["messages"]
            if message["role"] == "user"
        ]
        self.assertEqual(values[-1]["type"], "structure_error")
        self.assertEqual(self.action_results(agent), [])

    def test_enabled_capabilities_belong_to_current_instance(self):
        self.write_action("clock", EMPTY_SCHEMA, EMPTY_SCHEMA, "    return {}\n")
        client = _Client([_no_action("done-1")])
        manager = self.manager(client)
        first = manager.spawn_root(name="main", preset="main")
        second_info = manager.spawn(parent_id=first.agent_id, name="other", preset="worker")
        second = manager.require_agent(second_info["agent_id"])
        manager.enable_action(first.agent_id, "clock")
        self.assertIn("clock", first.standalone_actions())
        self.assertNotIn("clock", second.standalone_actions())
        self.assertEqual(self.presets.load("main").actions, ("say",))

    def test_protected_preset_cannot_be_spawned(self):
        manager = self.manager(_Client([_no_action("done-1")]))
        agent = manager.spawn_root(name="main", preset="main")
        with self.assertRaisesRegex(ValueError, "защищён"):
            manager.spawn(parent_id=agent.agent_id, name="copy", preset="main")
        self.assertEqual(
            [item["agent_id"] for item in manager.list_agents()], ["main"]
        )

    def test_main_uses_reserved_main_agent_id(self):
        manager = self.manager(_Client([_no_action("done-1")]))
        agent = manager.spawn_root(name="main", preset="main")
        self.assertEqual(agent.agent_id, "main")

    def test_persisted_context_is_restored_on_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            memory = MemoryStore(Path(temporary))
            first = self.manager(_Client([_no_action("done-1")]), memory=memory)
            agent = first.spawn_root(name="main", preset="main")
            self.publish(agent, "remember me")
            self.assertTrue(_wait(lambda: len(agent.history) >= 3))
            record = memory.load("main", "main")
            self.assertEqual(
                [message["role"] for message in record["messages"]],
                ["user", "assistant"],
            )

            second = self.manager(_Client([_no_action("done-2")]), memory=memory)
            restored = second.restore(name="main", preset="main")
            self.assertEqual(restored.agent_id, "main")
            self.assertEqual(len(restored.history), 3)
            self.assertIn(
                "remember me",
                json.dumps(restored.history, ensure_ascii=False),
            )
            self.assertIn("environment", restored.history[0]["content"])

    def test_subagent_instances_are_recreated_from_memory(self):
        with tempfile.TemporaryDirectory() as temporary:
            memory = MemoryStore(Path(temporary))
            first = self.manager(_Client([_no_action("done-1")]), memory=memory)
            root = first.spawn_root(name="main", preset="main")
            child_info = first.spawn(
                parent_id=root.agent_id, name="worker", preset="worker"
            )
            child = first.require_agent(child_info["agent_id"])
            self.publish(child, "child task")
            self.assertTrue(_wait(lambda: len(child.history) >= 3))

            second = self.manager(_Client([_no_action("done-2")]), memory=memory)
            restored = second.restore(name="main", preset="main")
            self.assertEqual(restored.agent_id, "main")
            recreated = second.require_agent(child_info["agent_id"])
            self.assertEqual(recreated.parent_id, root.agent_id)
            self.assertEqual(recreated.name, "worker")
            self.assertEqual(recreated.preset, "worker")
            self.assertIn(
                "child task",
                json.dumps(recreated.history, ensure_ascii=False),
            )

    def test_delete_removes_agent_memory(self):
        with tempfile.TemporaryDirectory() as temporary:
            memory = MemoryStore(Path(temporary))
            manager = self.manager(_Client([_no_action("done-1")]), memory=memory)
            root = manager.spawn_root(name="main", preset="main")
            child_info = manager.spawn(
                parent_id=root.agent_id, name="worker", preset="worker"
            )
            self.assertIsNotNone(memory.load("worker", child_info["agent_id"]))
            manager.delete(agent_id=child_info["agent_id"], reason="test")
            self.assertIsNone(memory.load("worker", child_info["agent_id"]))

    def test_enabled_capabilities_are_persisted(self):
        fixtures.write_echo_module(self.root)
        with tempfile.TemporaryDirectory() as temporary:
            memory = MemoryStore(Path(temporary))
            manager = self.manager(_Client([_no_action("done-1")]), memory=memory)
            root = manager.spawn_root(name="main", preset="main")
            manager.enable_module(root.agent_id, "echo")
            record = memory.load("main", "main")
            self.assertIn("echo", record["modules"])
            self.assertIn("say", record["actions"])


if __name__ == "__main__":
    unittest.main()
