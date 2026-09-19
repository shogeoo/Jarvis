import copy
import json
import re
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


class _Message:
    refusal = None

    def __init__(self, content):
        self.content = content


class _Response:
    def __init__(self, content):
        self.choices = [type("Choice", (), {"message": _Message(content)})()]


class _Completions:
    def __init__(self, outputs, *, block_call=None):
        self.outputs = list(outputs)
        self.block_call = block_call
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        index = len(self.calls) - 1
        if self.block_call is not None:
            self.block_call(index)
        output = self.outputs[index] if index < len(self.outputs) else self.outputs[-1]
        return _Response(output)


class _Client:
    def __init__(self, outputs, *, block_call=None):
        completions = _Completions(outputs, block_call=block_call)
        self.chat = type("Chat", (), {"completions": completions})()


class _Modules:
    def __init__(self, names=("test",)):
        self.names = set(names)

    def loaded_names(self):
        return set(self.names)

    def catalog(self, module_ids):
        return [
            {
                "module_id": name,
                "description": name,
                "actions": [],
                "events": [],
            }
            for name in sorted(module_ids)
        ]


def _wait(predicate, timeout=2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        main = root / "main"
        main.mkdir(parents=True)
        (main / "personprompt.txt").write_text("main", encoding="utf-8")
        (main / "modules.json").write_text('["test"]\n', encoding="utf-8")
        self.presets = PresetStore(root)
        self.events = EventRegistry()
        self.actions = ActionRegistry()
        register_core_protocol(self.actions, self.events)
        self.events.register(
            EventDefinition(
                "test.input",
                "input",
                object_schema({"text": {"type": "string"}}),
            ),
            owner="module:test",
        )
        self.events.register(
            EventDefinition(
                "test.result",
                "result",
                object_schema({"name": {"type": "string"}}),
            ),
            owner="module:test",
        )
        self.bus = EventBus(self.events, debug=Debugger(enabled=False))
        self.managers = []

    def tearDown(self):
        for manager in self.managers:
            manager.shutdown()
        self.temporary.cleanup()

    def manager(self, client):
        manager = AgentManager(
            model="test",
            client=client,
            actions=self.actions,
            events=self.events,
            bus=self.bus,
            modules=_Modules(),
            presets=self.presets,
            environment_prompt="environment",
            debug=Debugger(enabled=False),
        )
        self.managers.append(manager)
        return manager

    def publish(self, agent, text):
        self.assertTrue(
            self.bus.publish(
                Event(
                    type="test.input",
                    data={"text": text},
                    target=agent.agent_id,
                    module_id="test",
                )
            )
        )

    def test_actions_are_sequential_and_module_events_form_next_batch(self):
        order = []

        def run(data, ctx):
            order.append(data["name"])
            ctx.emit("test.result", {"name": data["name"]})

        self.actions.register(
            ActionSpec(
                "test.run",
                "run",
                object_schema({"name": {"type": "string"}}),
                run,
                owner="module:test",
            )
        )
        client = _Client(
            [
                '{"actions":[{"type":"test.run","data":{"name":"one"}},'
                '{"type":"test.run","data":{"name":"two"}}]}',
                '{"actions":[{"type":"no_action","data":{}}]}',
            ]
        )
        manager = self.manager(client)
        agent = manager.spawn_root(name="main", preset="main")
        self.publish(agent, "start")

        self.assertTrue(_wait(lambda: len(client.chat.completions.calls) >= 2))
        self.assertEqual(order, ["one", "two"])
        messages = client.chat.completions.calls[1]["messages"]
        results = [
            json.loads(message["content"])
            for message in messages
            if message["role"] == "user"
            and json.loads(message["content"])["type"] == "test.result"
        ]
        self.assertEqual([item["data"]["name"] for item in results], ["one", "two"])

    def test_action_failure_does_not_stop_later_actions(self):
        order = []

        def fail(data, ctx):
            order.append("fail")
            raise RuntimeError("expected")

        def succeed(data, ctx):
            order.append("succeed")
            ctx.emit("test.result", {"name": "succeed"})

        schema = empty_object_schema()
        self.actions.register(ActionSpec("test.fail", "fail", schema, fail, owner="module:test"))
        self.actions.register(ActionSpec("test.succeed", "succeed", schema, succeed, owner="module:test"))
        client = _Client(
            [
                '{"actions":[{"type":"test.fail","data":{}},'
                '{"type":"test.succeed","data":{}}]}',
                '{"actions":[{"type":"no_action","data":{}}]}',
            ]
        )
        manager = self.manager(client)
        agent = manager.spawn_root(name="main", preset="main")
        self.publish(agent, "start")

        self.assertTrue(_wait(lambda: len(client.chat.completions.calls) >= 2))
        self.assertEqual(order, ["fail", "succeed"])

    def test_events_in_next_batch_keep_their_actual_arrival_order(self):
        action_started = threading.Event()
        finish_action = threading.Event()

        def delayed_result(data, ctx):
            action_started.set()
            finish_action.wait(timeout=2)
            ctx.emit("test.result", {"name": "action"})

        self.actions.register(
            ActionSpec(
                "test.delayed",
                "delayed",
                empty_object_schema(),
                delayed_result,
                owner="module:test",
            )
        )
        client = _Client(
            [
                '{"actions":[{"type":"test.delayed","data":{}}]}',
                '{"actions":[{"type":"no_action","data":{}}]}',
            ]
        )
        manager = self.manager(client)
        agent = manager.spawn_root(name="main", preset="main")
        self.publish(agent, "first")
        self.assertTrue(action_started.wait(timeout=2))
        self.publish(agent, "arrived-before-result")
        finish_action.set()

        self.assertTrue(_wait(lambda: len(client.chat.completions.calls) >= 2))
        values = [
            json.loads(message["content"])
            for message in client.chat.completions.calls[1]["messages"]
            if message["role"] == "user"
        ]
        tail = [value["type"] for value in values[-2:]]
        self.assertEqual(tail, ["test.input", "test.result"])

    def test_structure_correction_holds_external_events_out_of_context(self):
        correction_started = threading.Event()
        allow_correction = threading.Event()

        def block(index):
            if index == 1:
                correction_started.set()
                allow_correction.wait(timeout=2)

        client = _Client(
            [
                "not json",
                '{"actions":[{"type":"no_action","data":{}}]}',
                '{"actions":[{"type":"no_action","data":{}}]}',
            ],
            block_call=block,
        )
        manager = self.manager(client)
        agent = manager.spawn_root(name="main", preset="main")
        self.publish(agent, "first")
        self.assertTrue(correction_started.wait(timeout=2))
        self.publish(agent, "second")
        allow_correction.set()

        self.assertTrue(_wait(lambda: len(client.chat.completions.calls) >= 3))
        correction_messages = client.chat.completions.calls[1]["messages"]
        correction_values = [
            json.loads(message["content"])
            for message in correction_messages
            if message["role"] == "user"
        ]
        self.assertEqual(correction_values[-1]["type"], "structure_error")
        self.assertNotIn(
            "second",
            json.dumps(correction_values, ensure_ascii=False),
        )
        next_messages = client.chat.completions.calls[2]["messages"]
        self.assertIn("second", json.dumps(next_messages, ensure_ascii=False))

    def test_action_return_value_does_not_create_implicit_result_event(self):
        self.actions.register(
            ActionSpec(
                "test.silent",
                "silent",
                empty_object_schema(),
                lambda data, ctx: {"ignored": True},
                owner="module:test",
            )
        )
        client = _Client(['{"actions":[{"type":"test.silent","data":{}}]}'])
        manager = self.manager(client)
        agent = manager.spawn_root(name="main", preset="main")
        self.publish(agent, "start")

        self.assertTrue(
            _wait(
                lambda: len(client.chat.completions.calls) == 1
                and agent.state == "waiting"
            )
        )
        self.assertEqual(len(client.chat.completions.calls), 1)

    def test_main_uses_random_three_digit_agent_id(self):
        client = _Client(['{"actions":[{"type":"no_action","data":{}}]}'])
        manager = self.manager(client)
        agent = manager.spawn_root(name="main", preset="main")
        self.assertRegex(agent.agent_id, re.compile(r"^agent-\d{3}$"))
        self.assertEqual(agent.name, "main")

    def test_running_agent_refreshes_contract_without_losing_history(self):
        client = _Client(['{"actions":[{"type":"no_action","data":{}}]}'])
        manager = self.manager(client)
        agent = manager.spawn_root(name="main", preset="main")
        self.publish(agent, "before-update")
        self.assertTrue(_wait(lambda: len(client.chat.completions.calls) >= 1))

        self.actions.register(
            ActionSpec(
                "clock.now",
                "current time",
                empty_object_schema(),
                lambda data, ctx: None,
                owner="module:clock",
            )
        )
        manager.modules.names.add("clock")
        self.presets.add_module("main", "clock")
        self.publish(agent, "after-update")

        self.assertTrue(_wait(lambda: len(client.chat.completions.calls) >= 2))
        messages = client.chat.completions.calls[1]["messages"]
        self.assertIn("clock.now", messages[0]["content"])
        rendered = json.dumps(messages, ensure_ascii=False)
        self.assertIn("before-update", rendered)
        self.assertIn("after-update", rendered)


if __name__ == "__main__":
    unittest.main()
