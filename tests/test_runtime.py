import copy
import importlib.util
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from jarvis.capabilities.manager import CapabilityManager
from jarvis.core.protocol import ActionRequest, CallResult, Event
from jarvis.core.registry import ActionRegistry, EventRegistry
from jarvis.core.runtime import AgentManager, EventBus, register_core_protocol
from jarvis.infrastructure.automations import AutomationStore
from jarvis.infrastructure.context import MemoryStore
from jarvis.infrastructure.debug import Debugger
from jarvis.presets import PresetStore

import fixtures


class _Response:
    def __init__(self, content):
        message = type("Message", (), {"content": content, "refusal": None})()
        self.choices = [type("Choice", (), {"message": message})()]

    def __iter__(self):
        yield SimpleNamespace(choices=[SimpleNamespace(delta=self.choices[0].message)])

    def close(self):
        pass


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
        output = self.outputs[index] if index < len(self.outputs) else _no_action(f"fallback-{index}")
        return _Response(output)


class _Client:
    def __init__(self, outputs, block=None):
        completions = _Completions(outputs, block)
        self.chat = type("Chat", (), {"completions": completions})()


def _action_file(data_schema, result_schema, run_body):
    return (
        "from jarvis.capabilities import action_definition\n\n\n"
        f"def run(data, context):\n{run_body}\n\n\n"
        "def create_action():\n"
        "    return action_definition(\n"
        '        "test",\n'
        f"        {data_schema!r},\n"
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
        '{"actions":[{"action_id":"no_action","call_id":'
        f"{json.dumps(action_id)},"
        '"data":{}}]}'
    )


def _wait(predicate, timeout=2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class RuntimeTests(unittest.TestCase):
    def install_project_action(self, action_id):
        self.actions.require(action_id)

    def enable_for_main(self, *action_ids):
        path = self.root / "presets" / "main" / "capabilities.json"
        capabilities = json.loads(path.read_text(encoding="utf-8"))
        capabilities["actions"] = sorted(
            set(capabilities["actions"]) | set(action_ids)
        )
        path.write_text(
            json.dumps(capabilities, indent=2) + "\n", encoding="utf-8"
        )

    def load_installed_action(self, action_id):
        if self.actions.get(action_id) is not None:
            return SimpleNamespace(run=self.actions.require(action_id).run)
        path = self.root / "actions" / action_id / "action.py"
        spec = importlib.util.spec_from_file_location(
            f"installed_{action_id}_action", path
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

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
                event_id="tick.event",
                data={"text": text},
                target=agent.agent_id,
                handler_id="tick",
            )
        )

    def write_action(self, action_id, data_schema, result_schema, run_body):
        fixtures.write_action(
            self.root / "actions",
            action_id,
            _action_file(data_schema, result_schema, run_body),
        )

    def call_results(self, agent):
        return [
            json.loads(message["content"])
            for message in agent.history
            if message["role"] == "user"
            and '"call_result"' in message["content"]
        ]

    def test_actions_are_dispatched_in_array_order_with_results(self):
        client = _Client(
            [
                '{"actions":['
                '{"action_id":"say","call_id":"say-1","data":{"text":"one"}},'
                '{"action_id":"say","call_id":"say-2","data":{"text":"two"}}]}',
                _no_action("done-1"),
            ]
        )
        manager = self.manager(client)
        agent = manager.spawn_root(name="main", preset="main")
        self.publish(agent, "start")
        self.assertTrue(_wait(lambda: len(self.call_results(agent)) == 2))
        self.assertEqual(
            [item["data"]["text"] for item in self.call_results(agent)],
            ["one", "two"],
        )
        self.assertEqual(
            [item["call_id"] for item in self.call_results(agent)],
            ["say-1", "say-2"],
        )

    def test_automation_dispatches_as_an_assistant_action_into_agent_context(self):
        client = _Client([_no_action("after-automation")])
        manager = self.manager(client)
        agent = manager.spawn_root(name="main", preset="main")
        AutomationStore(self.root / "automations.json").append(
            {
                "event": {"event_id": "tick.event", "data": {"text": "go"}},
                "actions": [{"action_id": "say", "data": {"text": "automated"}}],
            }
        )

        self.publish(agent, "go")
        self.assertTrue(_wait(lambda: len(self.call_results(agent)) == 1))
        assistant_actions = [
            json.loads(message["content"])
            for message in agent.history
            if message["role"] == "assistant"
            and "auto_act-" in message["content"]
        ]
        self.assertEqual(
            assistant_actions,
            [{"actions": [{"action_id": "say", "call_id": "auto_act-1", "data": {"text": "automated"}}]}],
        )
        self.assertEqual(self.call_results(agent)[0]["call_id"], "auto_act-1")
        self.publish(agent, "go")
        self.assertTrue(_wait(lambda: len(self.call_results(agent)) == 2))
        self.assertEqual(
            [item["call_id"] for item in self.call_results(agent)],
            ["auto_act-1", "auto_act-2"],
        )
        self.assertEqual(client.chat.completions.calls, [])

    def test_automated_result_does_not_request_model_after_restore(self):
        memory = MemoryStore(self.root / "memory")
        client = _Client([_no_action("unexpected")])
        manager = self.manager(client, memory=memory)
        agent = manager.spawn_root(name="main", preset="main")
        agent._automated_call_ids.add("auto_act-42")
        manager.persist_agent(agent)
        record = memory.load("main", "main")
        self.assertEqual(record["automated_call_ids"], ["auto_act-42"])
        manager.shutdown()
        restored_manager = self.manager(client, memory=memory)
        restored = restored_manager.restore(name="main", preset="main")
        restored.enqueue_result(CallResult(call_id="auto_act-42", agent_id="main", data={"spoken": True, "text": "done"}))
        self.assertTrue(_wait(lambda: len(self.call_results(restored)) == 1))
        self.assertTrue(_wait(lambda: restored.state == "waiting"))
        self.assertEqual(client.chat.completions.calls, [])

    def test_automatic_result_and_normal_event_batch_requests_model_once(self):
        client = _Client([_no_action("batch-done")])
        manager = self.manager(client)
        agent = manager.spawn_root(name="main", preset="main")
        agent._automated_call_ids.add("auto_act-1")
        agent._turn([
            CallResult(call_id="auto_act-1", agent_id="main", data={"spoken": True}),
            Event("tick.event", {"text": "normal"}, handler_id="tick"),
        ], agent._generation)
        self.assertEqual(len(client.chat.completions.calls), 1)
        self.assertIn("auto_act-1", str(client.chat.completions.calls[0]["messages"]))

    def test_normal_call_with_auto_prefix_is_not_an_automation(self):
        client = _Client([_no_action("normal-done")])
        manager = self.manager(client)
        agent = manager.spawn_root(name="main", preset="main")
        agent.enqueue_result(CallResult(call_id="auto_act-99", agent_id="main", data={"spoken": True}))
        self.assertTrue(_wait(lambda: len(client.chat.completions.calls) == 1))

    def test_call_result_automation_ignores_real_call_id_and_uses_normal_dispatch(self):
        client = _Client([_no_action("after-call-result-automation")])
        manager = self.manager(client)
        agent = manager.spawn_root(name="main", preset="main")
        AutomationStore(self.root / "automations.json").append(
            {
                "call_result": {
                    "event_id": "call_result",
                    "call_id": "example-id-not-used-at-runtime",
                    "data": {"spoken": True, "text": "origin"},
                },
                "actions": [{"action_id": "say", "data": {"text": "follow-up"}}],
            }
        )

        manager.capabilities.dispatch(
            action=ActionRequest(
                "say", {"text": "origin"}, "a-real-unrelated-call-id"
            ),
            spec=self.actions.require("say"),
            agent=agent,
        )
        self.assertTrue(_wait(lambda: len(self.call_results(agent)) == 2))
        self.assertEqual(
            [result["call_id"] for result in self.call_results(agent)],
            ["a-real-unrelated-call-id", "auto_act-1"],
        )
        follow_up = next(
            json.loads(message["content"])
            for message in agent.history
            if message["role"] == "assistant" and "auto_act-1" in message["content"]
        )
        self.assertEqual(follow_up["actions"][0]["data"], {"text": "follow-up"})
        self.assertEqual(client.chat.completions.calls, [])

    def test_management_actions_return_their_declared_result_shapes(self):
        client = _Client([_no_action("after-management")])
        self.install_project_action("create_automation")
        self.install_project_action("create_preset")
        self.enable_for_main("create_automation", "create_preset")
        manager = self.manager(client)
        agent = manager.spawn_root(name="main", preset="main")
        contract = agent._contract()[0]
        manager.capabilities.dispatch(
            action=ActionRequest(
                "create_automation",
                {
                    "event": {"event_id": "tick.event", "data": {"text": "saved"}},
                    "actions": [{"action_id": "say", "data": {"text": "ok"}}],
                },
                "create-automation-call",
            ),
            spec=contract["create_automation"],
            agent=agent,
        )
        manager.capabilities.dispatch(
            action=ActionRequest(
                "create_preset",
                {
                    "preset_id": "empty-preset",
                    "person_prompt": "An empty preset.",
                    "actions": ["missing-action"],
                    "handlers": [],
                    "modules": [],
                },
                "create-preset-call",
            ),
            spec=contract["create_preset"],
            agent=agent,
        )
        manager.capabilities.dispatch(
            action=ActionRequest(
                "create_preset",
                {
                    "preset_id": "empty-preset",
                    "person_prompt": "An empty preset.",
                    "actions": [],
                    "handlers": [],
                    "modules": [],
                },
                "create-preset-success-call",
            ),
            spec=contract["create_preset"],
            agent=agent,
        )
        self.assertTrue(_wait(lambda: len(self.call_results(agent)) == 3))
        results = {item["call_id"]: item["data"] for item in self.call_results(agent)}
        self.assertEqual(results["create-automation-call"]["status"], "created")
        self.assertIsNone(results["create-automation-call"]["error"])
        self.assertEqual(results["create-preset-call"]["status"], "not_created")
        self.assertIn("missing-action", results["create-preset-call"]["error"])
        self.assertEqual(
            results["create-preset-success-call"],
            {"status": "created", "preset_id": "empty-preset", "error": None},
        )
        self.assertTrue((self.root / "presets" / "empty-preset").is_dir())
        stored = AutomationStore(self.root / "automations.json").list()
        stored = [{key: value for key, value in rule.items() if key != "automation_id"} for rule in stored]
        self.assertEqual(
            stored,
            [
                {
                    "event": {"event_id": "tick.event", "data": {"text": "saved"}},
                    "actions": [{"action_id": "say", "data": {"text": "ok"}}],
                }
            ],
        )

    def test_primary_only_management_actions_are_not_in_subagent_prompt(self):
        client = _Client([_no_action("main-ready"), _no_action("worker-ready")])
        self.install_project_action("create_automation")
        self.install_project_action("create_preset")
        self.enable_for_main("create_automation", "create_preset")
        manager = self.manager(client)
        main = manager.spawn_root(name="main", preset="main")
        worker = manager.spawn(parent_id=main.agent_id, name="worker", preset="worker")
        main_prompt = main.history[0]["content"]
        worker_prompt = manager.require_agent(worker["agent_id"]).history[0]["content"]
        for action_id in ("create_automation", "create_preset"):
            self.assertIn(f'"action_id": "{action_id}"', main_prompt)
            self.assertNotIn(f'"action_id": "{action_id}"', worker_prompt)

    def test_create_preset_uses_existing_capability_ids_and_refuses_overwrite(self):
        self.install_project_action("create_preset")
        manager = self.manager(_Client([]))
        manager.spawn_root(name="main", preset="main")
        module = self.load_installed_action("create_preset")
        context = SimpleNamespace(
            agent_id="main",
            action_id="create_preset",
            agent_manager=manager.capabilities._agent_api,
            metadata={"preset": "main"},
            capabilities=manager.capabilities,
            config=SimpleNamespace(jarvis_dir=self.root),
        )
        created = module.run(
            {
                "preset_id": "researcher",
                "person_prompt": "Research carefully.",
                "actions": ["say"],
                "handlers": [],
                "modules": [],
            },
            context,
        )
        self.assertEqual(
            created,
            {"status": "created", "preset_id": "researcher", "error": None},
        )
        preset = self.presets.load("researcher")
        self.assertEqual(preset.person_prompt, "Research carefully.")
        self.assertEqual(preset.actions, ("say",))
        self.assertFalse(preset.protected)
        refused = module.run(
            {
                "preset_id": "researcher",
                "person_prompt": "Do not replace me.",
                "actions": [],
                "handlers": [],
                "modules": [],
            },
            context,
        )
        self.assertEqual(refused["status"], "not_created")
        self.assertIn("существует", refused["error"])
        self.assertEqual(
            self.presets.load("researcher").person_prompt,
            "Research carefully.",
        )

    def test_delete_agent_removes_memory_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            memory = MemoryStore(Path(temporary))
            manager = self.manager(_Client([_no_action("done")]), memory=memory)
            main = manager.spawn_root(name="main", preset="main")
            created = manager.spawn(parent_id=main.agent_id, name="worker", preset="worker")
            worker_id = created["agent_id"]
            manager.persist_agent(manager.require_agent(worker_id))
            worker_dir = Path(temporary) / "worker" / worker_id
            self.assertTrue(worker_dir.is_dir())
            self.assertEqual(
                manager.delete(agent_id=worker_id),
                {"agent_id": worker_id, "deleted": True},
            )
            self.assertFalse(worker_dir.exists())

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
                '{"action_id":"fail","call_id":"fail-1","data":{}},'
                '{"action_id":"next","call_id":"next-1","data":{}}]}',
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
                    if "event_id" in item:
                        values[(item["event_id"], item.get("call_id"))] = item
            values = list(values.values())
            has_error = any(
                item["event_id"] == "capability_error" for item in values
            )
            has_result = any(
                item["event_id"] == "call_result"
                and item["call_id"] == "next-1"
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
        error = next(item for item in values if item["event_id"] == "capability_error")
        self.assertEqual(error["data"]["kind"], "action")
        self.assertEqual(error["data"]["id"], "fail")
        results = [item for item in values if item["event_id"] == "call_result"]
        self.assertEqual([item["call_id"] for item in results], ["next-1"])
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
            json.loads(correction_messages[-1]["content"])["event_id"],
            "structure_error",
        )
        self.assertIn("second", json.dumps(client.chat.completions.calls[2]["messages"], ensure_ascii=False))

    def test_duplicate_action_id_is_a_structure_error(self):
        client = _Client(
            [
                '{"actions":['
                '{"action_id":"say","call_id":"say-1","data":{"text":"one"}},'
                '{"action_id":"say","call_id":"say-1","data":{"text":"two"}}]}',
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
        self.assertEqual(values[-1]["event_id"], "structure_error")
        self.assertEqual(self.call_results(agent), [])

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
            manager.delete(agent_id=child_info["agent_id"])
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

    def test_call_id_cannot_be_reused_after_completion_or_restore(self):
        outputs = [
            '{"actions":[{"action_id":"say","call_id":"used-1","data":{"text":"one"}}]}',
            '{"actions":[{"action_id":"say","call_id":"used-1","data":{"text":"again"}}]}',
            _no_action("fresh-1"),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            memory = MemoryStore(Path(temporary))
            client = _Client(outputs)
            manager = self.manager(client, memory=memory)
            agent = manager.spawn_root(name="main", preset="main")
            self.publish(agent, "start")
            self.assertTrue(_wait(lambda: len(client.chat.completions.calls) >= 3))
            self.assertEqual(len(self.call_results(agent)), 1)
            self.assertIn("call_id уже использован", agent.history[-2]["content"])

            restarted = self.manager(_Client([outputs[1], _no_action("fresh-2")]), memory=memory)
            restored = restarted.restore(name="main", preset="main")
            self.publish(restored, "again")
            self.assertTrue(_wait(lambda: len(restarted.client.chat.completions.calls) >= 2))
            self.assertEqual(len(self.call_results(restored)), 1)

    def test_transient_model_failure_retries_without_duplicate_input(self):
        class Flaky:
            def __init__(self):
                self.calls = []
            def create(self, **kwargs):
                self.calls.append(copy.deepcopy(kwargs))
                if len(self.calls) == 1:
                    raise ConnectionError("temporary")
                return _Response(_no_action("after-retry"))
        flaky = Flaky()
        client = type("Client", (), {"chat": type("Chat", (), {"completions": flaky})()})()
        manager = self.manager(client)
        agent = manager.spawn_root(name="main", preset="main")
        self.publish(agent, "hello")
        self.assertTrue(_wait(lambda: len(flaky.calls) == 2))
        self.assertEqual(len([m for m in agent.history if m["role"] == "user"]), 1)
        self.assertEqual(flaky.calls[0]["messages"], flaky.calls[1]["messages"])

    def test_disabled_action_is_hidden_but_an_accepted_call_returns_disabled_result(self):
        manager = self.manager(_Client([_no_action("done")]))
        agent = manager.spawn_root(name="main", preset="main")
        spec = manager.actions.require("say")
        manager.disable_action(agent.agent_id, "say")
        self.assertIn("say", agent.known_snapshot()["actions"])
        self.assertNotIn("say", agent.capabilities_snapshot()["actions"])
        specs, _ = agent._contract()
        self.assertNotIn("say", specs)
        from jarvis.core.protocol import ActionRequest
        manager.capabilities.dispatch(
            action=ActionRequest("say", {"text": "ignored"}, "disabled-1"),
            spec=spec, agent=agent,
        )
        self.assertTrue(_wait(lambda: any(r["call_id"] == "disabled-1" for r in self.call_results(agent))))
        result = next(r for r in self.call_results(agent) if r["call_id"] == "disabled-1")
        self.assertEqual(result["data"], {"status": "disabled", "info": "Action is currently disabled."})

    def test_interrupt_ignores_late_response_and_accepts_new_event(self):
        entered, release = threading.Event(), threading.Event()
        def block(index):
            if index == 0:
                entered.set()
                release.wait(3)
        client = _Client([_no_action("old"), _no_action("new")], block)
        manager = self.manager(client)
        root = manager.spawn_root(name="main", preset="main")
        child = manager.spawn(parent_id="main", name="worker", preset="worker")
        agent = manager.require_agent(child["agent_id"])
        self.publish(agent, "first")
        self.assertTrue(entered.wait(2))
        manager.interrupt(agent_id=agent.agent_id)
        self.publish(agent, "second")
        self.assertTrue(_wait(lambda: len(client.chat.completions.calls) >= 2))
        release.set()
        self.assertTrue(_wait(lambda: any("new" in str(m.get("content")) for m in agent.history)))
        self.assertFalse(any('"call_id":"old"' in str(m.get("content")) for m in agent.history))
        self.assertIn(agent.agent_id, manager.agents)

    def test_protected_agent_and_recursive_delete(self):
        manager = self.manager(_Client([_no_action("done")]))
        root = manager.spawn_root(name="main", preset="main")
        with self.assertRaisesRegex(ValueError, "Защищённый"):
            manager.delete(agent_id="main")
        with self.assertRaisesRegex(ValueError, "Защищённый"):
            manager.interrupt(agent_id="main", requester_id="another")
        parent = manager.spawn(parent_id="main", name="parent", preset="worker")["agent_id"]
        child = manager.spawn(parent_id=parent, name="child", preset="worker")["agent_id"]
        grandchild = manager.spawn(parent_id=child, name="grandchild", preset="worker")["agent_id"]
        manager.delete(agent_id=parent)
        self.assertEqual(set(manager.agents), {"main"})
        self.assertNotIn(child, manager.bus._agents)
        self.assertNotIn(grandchild, manager.bus._agents)

    def test_non_main_protected_preset_is_singleton_and_undeletable(self):
        special = self.root / "presets" / "special"
        special.mkdir()
        (special / "personprompt.txt").write_text("special", encoding="utf-8")
        (special / "capabilities.json").write_text(
            json.dumps({"modules": [], "actions": [], "handlers": []}),
            encoding="utf-8",
        )
        (special / "preset.json").write_text('{"protected": true}', encoding="utf-8")
        manager = self.manager(_Client([_no_action("done")]))
        manager.spawn_root(name="main", preset="main")
        special_id = manager.spawn(parent_id="main", name="special", preset="special")["agent_id"]
        with self.assertRaisesRegex(ValueError, "защищён"):
            manager.spawn(parent_id="main", name="duplicate", preset="special")
        with self.assertRaisesRegex(ValueError, "Защищённый"):
            manager.delete(agent_id=special_id)

    def test_restore_failure_preserves_memory_and_does_not_replace_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            memory = MemoryStore(Path(temporary))
            memory.save({
                "agent_id": "main", "name": "Jarvis", "preset": "main",
                "parent_id": None, "modules": [], "actions": ["missing_action"],
                "handlers": [], "messages": [{"role": "user", "content": "saved"}],
            })
            before = (Path(temporary) / "main" / "instance.json").read_bytes()
            manager = self.manager(_Client([_no_action("done")]), memory=memory)
            self.assertIsNone(manager.restore(name="main", preset="main"))
            self.assertEqual(manager.agents, {})
            self.assertEqual(memory.load("main", "main")["actions"], ["missing_action"])
            self.assertEqual((Path(temporary) / "main" / "instance.json").read_bytes(), before)

    def test_disable_running_action_kills_execution_and_returns_one_disabled_result(self):
        marker = self.root / "running.pid"
        result_schema = {
            "type": "object", "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"], "additionalProperties": False,
        }
        self.write_action(
            "slow", EMPTY_SCHEMA, result_schema,
            f"    import os, time\n    open({str(marker)!r}, 'w').write(str(os.getpid()))\n    time.sleep(10)\n    return {{'ok': True}}\n",
        )
        manager = self.manager(_Client([_no_action("done")]))
        agent = manager.spawn_root(name="main", preset="main")
        manager.enable_action("main", "slow")
        from jarvis.core.protocol import ActionRequest
        manager.capabilities.dispatch(action=ActionRequest("slow", {}, "slow-1"), spec=manager.actions.require("slow"), agent=agent)
        self.assertTrue(_wait(marker.exists))
        pid = int(marker.read_text())
        manager.disable_action("main", "slow")
        self.assertTrue(_wait(lambda: any(r["call_id"] == "slow-1" for r in self.call_results(agent))))
        results = [r for r in self.call_results(agent) if r["call_id"] == "slow-1"]
        self.assertEqual([r["data"]["status"] for r in results], ["disabled"])
        self.assertEqual(results[0]["data"]["info"], "Action was disabled before completion.")
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_invalid_result_is_protocol_error_to_calling_child(self):
        self.write_action(
            "bad_result", EMPTY_SCHEMA,
            {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False},
            "    return {'undeclared': 'wrong'}\n",
        )
        manager = self.manager(_Client([_no_action("done")]))
        root = manager.spawn_root(name="main", preset="main")
        child_info = manager.spawn(parent_id="main", name="worker", preset="worker")
        child = manager.require_agent(child_info["agent_id"])
        manager.enable_action(child.agent_id, "bad_result")
        from jarvis.core.protocol import ActionRequest
        manager.capabilities.dispatch(action=ActionRequest("bad_result", {}, "bad-1"), spec=manager.actions.require("bad_result"), agent=child)
        self.assertTrue(_wait(lambda: any('"event_id":"capability_error"' in str(m.get("content")) for m in child.history)))
        errors = [json.loads(m["content"]) for m in child.history if m["role"] == "user" and isinstance(m["content"], str) and '"capability_error"' in m["content"]]
        self.assertEqual([e["data"]["call_id"] for e in errors], ["bad-1"])
        self.assertFalse(any('"capability_error"' in str(m.get("content")) for m in root.history[1:]))
        self.assertFalse(manager.results.has_pending(child.agent_id, "bad-1"))

    def test_disabled_state_survives_restore_and_available_list_excludes_known(self):
        with tempfile.TemporaryDirectory() as temporary:
            memory = MemoryStore(Path(temporary))
            first = self.manager(_Client([_no_action("one")]), memory=memory)
            root = first.spawn_root(name="main", preset="main")
            first.disable_action("main", "say")
            first.disable_handler("main", "tick")
            self.assertIn("say", memory.load("main", "main")["disabled_actions"])
            listed = first.capabilities.list_available(root.known_snapshot())
            self.assertNotIn("say", [item["id"] for item in listed["actions"]])
            self.assertNotIn("tick", [item["id"] for item in listed["handlers"]])
            second = self.manager(_Client([_no_action("two")]), memory=memory)
            restored = second.restore(name="main", preset="main")
            self.assertIn("say", restored.disabled_snapshot()["actions"])
            self.assertIn("tick", restored.disabled_snapshot()["handlers"])
            event = Event(event_id="tick.event", data={"text": "ignored"}, handler_id="tick")
            self.assertFalse(second.bus.publish(event))
            self.assertNotIn("say", restored._contract()[0])

    def test_available_list_describes_unloaded_action(self):
        self.write_action("extra", EMPTY_SCHEMA, EMPTY_SCHEMA, "    return {}\n")
        manager = self.manager(_Client([_no_action("done")]))
        root = manager.spawn_root(name="main", preset="main")
        self.assertNotIn("extra", manager.capabilities.loaded_actions())
        available = manager.capabilities.list_available(root.known_snapshot())
        self.assertEqual([item for item in available["actions"] if item["id"] == "extra"], [{"id": "extra", "description": "test"}])

    def test_interrupt_action_rpc_matches_agent_manager_signature(self):
        self.install_project_action("interrupt_agent")
        manager = self.manager(_Client([_no_action("done")]))
        main = manager.spawn_root(name="main", preset="main")
        child_id = manager.spawn(parent_id="main", name="worker", preset="worker")["agent_id"]
        manager.enable_action("main", "interrupt_agent")
        from jarvis.core.protocol import ActionRequest
        manager.capabilities.dispatch(
            action=ActionRequest("interrupt_agent", {"agent_id": child_id}, "interrupt-1"),
            spec=manager.actions.require("interrupt_agent"), agent=main,
        )
        self.assertTrue(_wait(lambda: any(item["call_id"] == "interrupt-1" for item in self.call_results(main))))
        result = next(item for item in self.call_results(main) if item["call_id"] == "interrupt-1")
        self.assertEqual(result["data"]["state"], "waiting")
        self.assertIn(child_id, manager.agents)

    def test_delete_action_rpc_needs_only_agent_id(self):
        self.install_project_action("delete_agent")
        manager = self.manager(_Client([_no_action("done")]))
        main = manager.spawn_root(name="main", preset="main")
        child_id = manager.spawn(parent_id="main", name="worker", preset="worker")["agent_id"]
        manager.enable_action("main", "delete_agent")
        from jarvis.core.protocol import ActionRequest
        manager.capabilities.dispatch(
            action=ActionRequest("delete_agent", {"agent_id": child_id}, "delete-1"),
            spec=manager.actions.require("delete_agent"), agent=main,
        )
        self.assertTrue(_wait(lambda: any(item["call_id"] == "delete-1" for item in self.call_results(main))))
        result = next(item for item in self.call_results(main) if item["call_id"] == "delete-1")
        self.assertEqual(result["data"], {"agent_id": child_id, "deleted": True})
        self.assertNotIn(child_id, manager.agents)

    def test_global_pause_then_resume_reloads_changed_action(self):
        self.install_project_action("toggle_capability")
        value_schema = {
            "type": "object", "properties": {"value": {"type": "string"}},
            "required": ["value"], "additionalProperties": False,
        }
        self.write_action("reloadable", EMPTY_SCHEMA, value_schema, "    return {'value': 'old'}\n")
        manager = self.manager(_Client([_no_action("done")]))
        main = manager.spawn_root(name="main", preset="main")
        child_id = manager.spawn(parent_id="main", name="worker", preset="worker")["agent_id"]
        manager.enable_action("main", "toggle_capability")
        manager.enable_action("main", "reloadable")
        manager.enable_action(child_id, "reloadable")
        old_host = manager.capabilities._actions["reloadable"].host
        old_process = old_host.process
        from jarvis.core.protocol import ActionRequest
        manager.capabilities.dispatch(
            action=ActionRequest("toggle_capability", {"kind": "action", "id": "reloadable"}, "pause-1"),
            spec=manager.actions.require("toggle_capability"), agent=main,
        )
        self.assertTrue(_wait(lambda: any(item["call_id"] == "pause-1" for item in self.call_results(main))))
        self.assertNotIn("reloadable", manager.capabilities.loaded_actions())
        self.assertNotIn(old_host.key, manager.capabilities._hosts)
        self.assertIsNotNone(old_process.poll())
        self.assertIn("reloadable", manager.capabilities.global_paused()["actions"])
        self.assertIn("reloadable", main.standalone_actions())
        self.assertIn("reloadable", manager.require_agent(child_id).standalone_actions())

        self.write_action("reloadable", EMPTY_SCHEMA, value_schema, "    return {'value': 'new'}\n")
        manager.capabilities.dispatch(
            action=ActionRequest("toggle_capability", {"kind": "action", "id": "reloadable"}, "resume-1"),
            spec=manager.actions.require("toggle_capability"), agent=main,
        )
        self.assertTrue(_wait(lambda: any(item["call_id"] == "resume-1" for item in self.call_results(main))))
        self.assertIn("reloadable", main.standalone_actions())
        self.assertIn("reloadable", manager.require_agent(child_id).standalone_actions())
        self.assertNotIn("reloadable", manager.capabilities.global_paused()["actions"])
        self.assertIsNot(manager.capabilities._actions["reloadable"].host, old_host)
        manager.capabilities.dispatch(
            action=ActionRequest("reloadable", {}, "reload-1"),
            spec=manager.actions.require("reloadable"), agent=main,
        )
        self.assertTrue(_wait(lambda: any(item["call_id"] == "reload-1" for item in self.call_results(main))))
        result = next(item for item in self.call_results(main) if item["call_id"] == "reload-1")
        self.assertEqual(result["data"], {"value": "new"})

    def test_global_pause_cancels_running_action_before_reporting_completion(self):
        self.install_project_action("toggle_capability")
        marker = self.root / "global-running.pid"
        schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}
        self.write_action(
            "global_slow", EMPTY_SCHEMA, schema,
            f"    import os, time\n    open({str(marker)!r}, 'w').write(str(os.getpid()))\n    time.sleep(10)\n    return {{'ok': True}}\n",
        )
        manager = self.manager(_Client([_no_action("done")]))
        main = manager.spawn_root(name="main", preset="main")
        manager.enable_action("main", "toggle_capability")
        manager.enable_action("main", "global_slow")
        from jarvis.core.protocol import ActionRequest
        manager.capabilities.dispatch(
            action=ActionRequest("global_slow", {}, "running-1"),
            spec=manager.actions.require("global_slow"), agent=main,
        )
        self.assertTrue(_wait(marker.exists))
        pid = int(marker.read_text())
        manager.capabilities.dispatch(
            action=ActionRequest("toggle_capability", {"kind": "action", "id": "global_slow"}, "pause-running-1"),
            spec=manager.actions.require("toggle_capability"), agent=main,
        )
        self.assertTrue(_wait(lambda: any(item["call_id"] == "pause-running-1" for item in self.call_results(main))))
        results = [item for item in self.call_results(main) if item["call_id"] == "running-1"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["data"]["status"], "paused")
        self.assertNotIn("global_slow", manager.capabilities.loaded_actions())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_global_toggle_same_capability_twice_in_batch_is_structure_error(self):
        self.install_project_action("toggle_capability")
        client = _Client([
            '{"actions":['
            '{"action_id":"toggle_capability","call_id":"toggle-1","data":{"kind":"action","id":"say"}},'
            '{"action_id":"toggle_capability","call_id":"toggle-2","data":{"kind":"action","id":"say"}}]}',
            _no_action("done-1"),
        ])
        manager = self.manager(client)
        main = manager.spawn_root(name="main", preset="main")
        manager.enable_action("main", "toggle_capability")
        self.publish(main, "check")
        self.assertTrue(_wait(lambda: len(client.chat.completions.calls) >= 2))
        self.assertIn("structure_error", str(main.history))
        self.assertIn("say", main.standalone_actions())
        self.assertNotIn("say", manager.capabilities.global_paused()["actions"])


if __name__ == "__main__":
    unittest.main()
