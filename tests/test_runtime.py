import copy
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from jarvis.core.protocol import Event, empty_object_schema, object_schema
from jarvis.core.registry import ActionRegistry, EventRegistry
from jarvis.core.runtime import AgentManager, EventBus, register_core_protocol
from jarvis.infrastructure.debug import Debugger
from jarvis.modules import ActionSpec, EventDefinition
from jarvis.presets import PresetStore


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


class _Modules:
    def __init__(self, actions):
        self.actions = actions
        self.names = {"test"}
        self.dispatched = []
        self.fail_types = set()

    def loaded_names(self):
        return set(self.names)

    def load_many(self, module_ids, *, start_handlers):
        self.names.update(module_ids)

    def start_many(self, module_ids):
        self.names.update(module_ids)

    def load(self, module_id, *, start_handlers=True):
        self.names.add(module_id)

    def unload(self, module_id):
        self.names.discard(module_id)

    def dispatch(self, *, action, spec, agent):
        self.dispatched.append(action)
        if action.type in self.fail_types:
            raise RuntimeError("dispatch failed")

    def catalog(self, module_ids):
        result = []
        for module_id in sorted(module_ids):
            actions = [
                {
                    "type": spec.type,
                    "description": spec.description,
                    "data_schema": spec.data_schema,
                }
                for spec in self.actions.all().values()
                if spec.owner == f"module:{module_id}"
            ]
            result.append(
                {
                    "module_id": module_id,
                    "description": module_id,
                    "actions": actions,
                    "events": [],
                }
            )
        return result


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
        root = Path(temporary.name)
        main = root / "main"
        main.mkdir()
        (main / "personprompt.txt").write_text("main", encoding="utf-8")
        (main / "modules.json").write_text('["test"]\n', encoding="utf-8")
        self.presets = PresetStore(root)
        self.actions = ActionRegistry()
        self.events = EventRegistry()
        register_core_protocol(self.actions, self.events)
        for event_type in ("test.input", "test.result"):
            field = "text" if event_type.endswith("input") else "name"
            self.events.register(
                EventDefinition(
                    event_type,
                    event_type,
                    object_schema({field: {"type": "string"}}),
                ),
                owner="module:test",
            )
        self.bus = EventBus(self.events, debug=Debugger(enabled=False))
        self.managers = []

    def tearDown(self):
        for manager in self.managers:
            manager.shutdown()

    def manager(self, client):
        modules = _Modules(self.actions)
        manager = AgentManager(
            model="test",
            client=client,
            actions=self.actions,
            events=self.events,
            bus=self.bus,
            modules=modules,
            presets=self.presets,
            environment_prompt="environment",
            debug=Debugger(enabled=False),
        )
        self.managers.append(manager)
        return manager

    def publish(self, agent, text):
        self.bus.publish(
            Event(
                type="test.input",
                data={"text": text},
                target=agent.agent_id,
                module_id="test",
            )
        )

    def test_actions_are_dispatched_in_array_order_without_waiting(self):
        self.actions.register(
            ActionSpec(
                "test.run",
                "run",
                object_schema({"name": {"type": "string"}}),
                lambda data, context: None,
                owner="module:test",
            )
        )
        client = _Client(
            [
                '{"actions":['
                '{"type":"test.run","data":{"name":"one"}},'
                '{"type":"test.run","data":{"name":"two"}}]}'
            ]
        )
        manager = self.manager(client)
        agent = manager.spawn_root(name="main", preset="main")
        self.publish(agent, "start")
        self.assertTrue(_wait(lambda: agent.state == "waiting" and len(manager.modules.dispatched) == 2))
        self.assertEqual([item.data["name"] for item in manager.modules.dispatched], ["one", "two"])
        self.assertEqual(len(client.chat.completions.calls), 1)

    def test_dispatch_error_does_not_stop_later_actions(self):
        schema = empty_object_schema()
        for name in ("test.fail", "test.next"):
            self.actions.register(
                ActionSpec(name, name, schema, lambda data, context: None, owner="module:test")
            )
        client = _Client(
            [
                '{"actions":['
                '{"type":"test.fail","data":{}},'
                '{"type":"test.next","data":{}}]}',
                '{"actions":[{"type":"no_action","data":{}}]}',
            ]
        )
        manager = self.manager(client)
        manager.modules.fail_types.add("test.fail")
        agent = manager.spawn_root(name="main", preset="main")
        self.publish(agent, "start")
        self.assertTrue(_wait(lambda: len(manager.modules.dispatched) == 2))
        self.assertEqual([item.type for item in manager.modules.dispatched], ["test.fail", "test.next"])
        self.assertTrue(_wait(lambda: len(client.chat.completions.calls) >= 2))
        values = [
            json.loads(message["content"])
            for message in client.chat.completions.calls[1]["messages"]
            if message["role"] == "user"
        ]
        error = next(item for item in values if item["type"] == "module_error")
        self.assertEqual(
            set(error["data"]),
            {"module_id", "error"},
        )

    def test_events_in_busy_batch_keep_arrival_order(self):
        thinking = threading.Event()
        release = threading.Event()

        def block(index):
            if index == 0:
                thinking.set()
                release.wait(2)

        client = _Client(
            ['{"actions":[{"type":"no_action","data":{}}]}'] * 2,
            block,
        )
        manager = self.manager(client)
        agent = manager.spawn_root(name="main", preset="main")
        self.publish(agent, "first")
        self.assertTrue(thinking.wait(2))
        self.publish(agent, "external")
        self.bus.publish(
            Event(
                type="test.result",
                data={"name": "result"},
                target=agent.agent_id,
                module_id="test",
            )
        )
        release.set()
        self.assertTrue(_wait(lambda: len(client.chat.completions.calls) >= 2))
        values = [
            json.loads(message["content"])
            for message in client.chat.completions.calls[1]["messages"]
            if message["role"] == "user"
        ]
        self.assertEqual([item["type"] for item in values[-2:]], ["test.input", "test.result"])

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
                '{"actions":[{"type":"no_action","data":{}}]}',
                '{"actions":[{"type":"no_action","data":{}}]}',
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

    def test_enabled_modules_belong_to_current_instance(self):
        client = _Client(['{"actions":[{"type":"no_action","data":{}}]}'])
        manager = self.manager(client)
        first = manager.spawn_root(name="main", preset="main")
        second_info = manager.spawn(parent_id=first.agent_id, name="other", preset="main")
        second = manager.require_agent(second_info["agent_id"])
        self.actions.register(
            ActionSpec("clock.now", "time", empty_object_schema(), lambda d, c: None, owner="module:clock")
        )
        manager.enable_module(first.agent_id, "clock")
        self.assertIn("clock", first.modules())
        self.assertNotIn("clock", second.modules())
        self.assertEqual(self.presets.load("main").modules, ("test",))

    def test_main_uses_reserved_main_agent_id(self):
        manager = self.manager(_Client(['{"actions":[{"type":"no_action","data":{}}]}']))
        agent = manager.spawn_root(name="main", preset="main")
        self.assertEqual(agent.agent_id, "main")


if __name__ == "__main__":
    unittest.main()
