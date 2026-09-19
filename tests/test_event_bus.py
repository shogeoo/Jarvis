import unittest

from jarvis.core.protocol import Event, object_schema
from jarvis.core.registry import EventRegistry
from jarvis.core.runtime import EventBus
from jarvis.infrastructure.debug import Debugger
from jarvis.modules import EventDefinition


class _Agent:
    def __init__(self, agent_id, modules):
        self.agent_id = agent_id
        self.modules = set(modules)
        self.events = []

    def accepts_module(self, module_id):
        return module_id in self.modules

    def enqueue(self, event):
        self.events.append(event)


class EventBusTests(unittest.TestCase):
    def setUp(self):
        events = EventRegistry()
        events.register(
            EventDefinition(
                "telegram.message",
                "message",
                object_schema({"text": {"type": "string"}}),
            ),
            owner="module:telegram",
        )
        self.bus = EventBus(events, debug=Debugger(enabled=False))
        self.first = _Agent("agent-001", {"telegram"})
        self.second = _Agent("agent-002", {"telegram"})
        self.other = _Agent("agent-003", {"calendar"})
        for agent in (self.first, self.second, self.other):
            self.bus.bind(agent)

    def test_handler_event_reaches_every_agent_with_module(self):
        event = Event(
            type="telegram.message",
            data={"text": "hello"},
            module_id="telegram",
        )
        self.assertTrue(self.bus.publish(event))
        self.assertEqual(self.first.events, [event])
        self.assertEqual(self.second.events, [event])
        self.assertEqual(self.other.events, [])

    def test_targeted_action_event_only_reaches_selected_agent(self):
        event = Event(
            type="telegram.message",
            data={"text": "sent"},
            module_id="telegram",
            target="agent-002",
        )
        self.assertTrue(self.bus.publish(event))
        self.assertEqual(self.first.events, [])
        self.assertEqual(self.second.events, [event])


if __name__ == "__main__":
    unittest.main()
