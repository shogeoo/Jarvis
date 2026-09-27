import unittest

from jarvis.core.protocol import Event, object_schema
from jarvis.core.registry import EventRegistry
from jarvis.core.runtime import EventBus
from jarvis.capabilities import EventDefinition
from jarvis.infrastructure.debug import Debugger


class _Agent:
    def __init__(self, agent_id, handlers=(), modules=()):
        self.agent_id = agent_id
        self.handlers = set(handlers)
        self.modules = set(modules)
        self.events = []

    def accepts_event(self, event):
        if event.handler_id is not None and event.handler_id in self.handlers:
            return True
        if event.module_id is not None and event.module_id in self.modules:
            return True
        return False

    def enqueue(self, event):
        self.events.append(event)


class EventBusTests(unittest.TestCase):
    def setUp(self):
        events = EventRegistry()
        events.register(
            EventDefinition(
                "example.message",
                "message",
                object_schema({"text": {"type": "string"}}),
            ),
            owner="module:example",
        )
        events.register(
            EventDefinition(
                "tick.event",
                "tick",
                object_schema({"text": {"type": "string"}}),
            ),
            owner="handler:tick",
        )
        self.bus = EventBus(events, debug=Debugger(enabled=False))
        self.first = _Agent("agent-001", modules={"example"})
        self.second = _Agent("agent-002", modules={"example"})
        self.other = _Agent("agent-003", handlers={"tick"})
        for agent in (self.first, self.second, self.other):
            self.bus.bind(agent)

    def test_handler_event_reaches_every_subscribed_agent(self):
        event = Event(
            type="example.message",
            data={"text": "hello"},
            module_id="example",
            handler_id="example.monitor",
        )
        self.assertTrue(self.bus.publish(event))
        self.assertEqual(self.first.events, [event])
        self.assertEqual(self.second.events, [event])
        self.assertEqual(self.other.events, [])

    def test_standalone_handler_event_reaches_only_subscribed_agent(self):
        event = Event(
            type="tick.event",
            data={"text": "tick"},
            handler_id="tick",
        )
        self.assertTrue(self.bus.publish(event))
        self.assertEqual(self.first.events, [])
        self.assertEqual(self.second.events, [])
        self.assertEqual(self.other.events, [event])

    def test_targeted_event_only_reaches_selected_agent(self):
        event = Event(
            type="example.message",
            data={"text": "sent"},
            module_id="example",
            handler_id="example.monitor",
            target="agent-002",
        )
        self.assertTrue(self.bus.publish(event))
        self.assertEqual(self.first.events, [])
        self.assertEqual(self.second.events, [event])


if __name__ == "__main__":
    unittest.main()
