import json
import threading
import time
import unittest
from concurrent.futures import Future
from unittest.mock import patch

from jarvis.builtin import register_builtin_actions, register_builtin_events
from jarvis.debug import Debugger
from jarvis.module_manager import ModuleManager
from jarvis.module_api import ActionSpec
from jarvis.protocol import Event, empty_object_schema, object_schema
from jarvis.registry import ActionRegistry, EventRegistry
from jarvis.runtime import Agent, AgentManager, EventBus


class _Message:
    refusal = None

    def __init__(self, content):
        self.content = content


class _Response:
    def __init__(self, content):
        self.choices = [type("Choice", (), {"message": _Message(content)})()]


class _Completions:
    def __init__(self, first_output):
        self.first_output = first_output
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            return _Response(self.first_output)
        return _Response('{"actions":[{"type":"no_action","data":{}}]}')


class _Client:
    def __init__(self, first_output):
        self.chat = type("Chat", (), {"completions": _Completions(first_output)})()


class _DeferredSpeaker:
    def __init__(self):
        self.submitted = threading.Event()
        self.future = Future()

    def submit(self, text):
        self.submitted.set()
        return self.future


class RuntimeTests(unittest.TestCase):
    def test_spawn_uses_immutable_preset_and_does_not_send_a_task(self):
        events = EventRegistry()
        register_builtin_events(events)
        actions = ActionRegistry()
        bus = EventBus(events, debug=Debugger(enabled=False))
        modules = ModuleManager(bus, actions, events, debug=Debugger(enabled=False))
        manager = AgentManager(
            model="test",
            client=_Client('{"actions":[{"type":"no_action","data":{}}]}'),
            actions=actions,
            events=events,
            bus=bus,
            module_manager=modules,
            debug=Debugger(enabled=False),
        )
        register_builtin_actions(actions, manager)
        with patch.object(Agent, "start", lambda self: self):
            manager.create_main("main")
            result = manager.spawn(parent_id="main", preset="module_manager")
            agent = manager.agents[result["agent_id"]]
            self.assertEqual(result["preset"], "module_manager")
            self.assertTrue(agent._events.empty())
            self.assertIn("workspace.delete", agent.available_actions())
            self.assertIn("module.complete", agent.available_actions())
            self.assertNotIn("module.create", actions.all())
        manager.shutdown()

    def test_actions_run_in_parallel_and_results_are_individual_events(self):
        events = EventRegistry()
        register_builtin_events(events)
        actions = ActionRegistry()
        bus = EventBus(events, debug=Debugger(enabled=False))
        modules = ModuleManager(bus, actions, events, debug=Debugger(enabled=False))
        started = 0
        started_lock = threading.Lock()
        both_started = threading.Event()
        release = threading.Event()

        def slow_action(data, context):
            nonlocal started
            with started_lock:
                started += 1
                if started == 2:
                    both_started.set()
            release.wait(timeout=2)
            return {"name": data["name"]}

        action_schema = object_schema({"name": {"type": "string"}})
        for name in ("test.one", "test.two"):
            actions.register(
                ActionSpec(
                    type=name,
                    description=name,
                    data_schema=action_schema,
                    handler=slow_action,
                    owner="test",
                )
            )
        actions.register(
            ActionSpec(
                type="no_action",
                description="wait",
                data_schema=empty_object_schema(),
                handler=lambda data, context: None,
                owner="builtin",
            )
        )
        client = _Client(
            '{"actions":['
            '{"type":"test.one","data":{"name":"one"}},'
            '{"type":"test.two","data":{"name":"two"}}'
            ']}'
        )
        manager = AgentManager(
            model="test",
            client=client,
            actions=actions,
            events=events,
            bus=bus,
            module_manager=modules,
            debug=Debugger(enabled=False),
        )
        agent = manager.create_main("test")
        bus.publish(Event(type="speech", data={"text": "start"}))
        self.assertTrue(both_started.wait(timeout=2))
        bus.publish(Event(type="speech", data={"text": "while busy"}))
        release.set()
        deadline = time.time() + 2
        while len(client.chat.completions.calls) < 2 and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(len(client.chat.completions.calls), 2)
        second_messages = client.chat.completions.calls[1]["messages"]
        event_messages = [
            message
            for message in second_messages
            if message["role"] == "user"
        ]
        self.assertGreaterEqual(len(event_messages), 3)
        values = [json.loads(message["content"]) for message in event_messages]
        self.assertEqual(
            sum(value["type"] == "action_result" for value in values),
            2,
        )
        self.assertIn(
            {"type": "speech", "data": {"text": "while busy"}},
            values,
        )
        manager.shutdown()

    def test_speech_keeps_agent_busy_until_playback_finishes(self):
        events = EventRegistry()
        register_builtin_events(events)
        actions = ActionRegistry()
        bus = EventBus(events, debug=Debugger(enabled=False))
        modules = ModuleManager(bus, actions, events, debug=Debugger(enabled=False))
        speaker = _DeferredSpeaker()

        def speech_action(data, context):
            speaker.submit(data["text"]).result()
            return {"spoken": True}

        actions.register(
            ActionSpec(
                type="speech",
                description="speech",
                data_schema=object_schema({"text": {"type": "string"}}),
                handler=speech_action,
                owner="test",
            )
        )
        actions.register(
            ActionSpec(
                type="no_action",
                description="wait",
                data_schema=empty_object_schema(),
                handler=lambda data, context: None,
                owner="builtin",
            )
        )
        client = _Client(
            '{"actions":[{"type":"speech","data":{"text":"ответ"}}]}'
        )
        manager = AgentManager(
            model="test",
            client=client,
            actions=actions,
            events=events,
            bus=bus,
            module_manager=modules,
            debug=Debugger(enabled=False),
        )
        manager.create_main("test")
        try:
            bus.publish(Event(type="speech", data={"text": "первое"}))
            self.assertTrue(speaker.submitted.wait(timeout=2))
            manager.hold(agent_id="main", until_event="speech")
            speaker.future.set_result({"spoken": True})
            time.sleep(0.05)
            self.assertEqual(len(client.chat.completions.calls), 1)

            bus.publish(Event(type="speech", data={"text": "второе"}))
            deadline = time.time() + 2
            while len(client.chat.completions.calls) < 2 and time.time() < deadline:
                time.sleep(0.01)
            self.assertEqual(len(client.chat.completions.calls), 2)
        finally:
            manager.shutdown()


if __name__ == "__main__":
    unittest.main()
