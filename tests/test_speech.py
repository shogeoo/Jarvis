import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from jarvis.capabilities.manager import CapabilityManager
from jarvis.core.protocol import ActionRequest
from jarvis.core.registry import ActionRegistry, EventRegistry
from jarvis.core.runtime import CallResultTracker, EventBus, register_core_protocol
from jarvis.infrastructure.debug import Debugger
from jarvis.speech import service as speech_service
from jarvis.speech.service import SpeechService
from jarvis.speech.config import TTS_REFERENCE, TTS_REFERENCE_TEXT


class _Agent:
    agent_id = "main"
    name = "main"
    preset = "main"

    def __init__(self):
        self.results = []

    def enqueue_result(self, result):
        self.results.append(result)

    def is_enabled_action(self, action_id):
        return True


class _Manager:
    def __init__(self, agent):
        self.agent = agent
        self.debug = Debugger(enabled=False)
        self.results = CallResultTracker(debug=self.debug)

    def deliver_result(self, result):
        self.agent.enqueue_result(result)
        return True

    def report_capability_error(self, capability, error):
        self.debug.log("capability_error", capability=capability, error=str(error))


class SpeechProtocolTests(unittest.TestCase):
    def setUp(self):
        self.actions = ActionRegistry()
        self.events = EventRegistry()
        register_core_protocol(self.actions, self.events)

    def test_speech_belongs_only_to_primary_agent(self):
        plain_actions = self.actions.for_capabilities(
            modules=set(), actions=set()
        )
        primary_actions = self.actions.for_capabilities(
            modules=set(), actions=set(), primary=True
        )
        self.assertNotIn("speech", plain_actions)
        self.assertIn("speech", primary_actions)
        self.assertIn("no_action", primary_actions)

        plain_events = self.events.for_capabilities(
            modules=set(), handlers=set()
        )
        primary_events = self.events.for_capabilities(
            modules=set(), handlers=set(), primary=True
        )
        self.assertNotIn("speech_detected", plain_events)
        self.assertIn("speech_detected", primary_events)
        self.assertIn("structure_error", primary_events)

    def test_voice_reference_assets_live_in_core_package(self):
        self.assertTrue(TTS_REFERENCE.is_file())
        self.assertTrue(TTS_REFERENCE_TEXT.is_file())

    def test_speech_action_waits_for_background_initialization(self):
        service = SpeechService()
        release = threading.Event()
        finished = threading.Event()
        result = []

        def initialize(*args, **kwargs):
            release.wait(2)
            service._speaker = Mock()
            service._speaker.submit.return_value.result.return_value = {"status": "successful"}
            service.available = True

        with patch.object(service, "start", side_effect=initialize):
            service.begin_background(Path("/tmp"), emit=Mock())
            worker = threading.Thread(target=lambda: (result.append(service.speak_result("hello")), finished.set()))
            worker.start()
            self.assertFalse(finished.wait(0.05))
            release.set()
            self.assertTrue(finished.wait(2))
            worker.join()
        self.assertEqual(result, [{"status": "successful"}])
        service.shutdown()

    def test_speech_event_carries_text_only(self):
        event = self.events.get("speech_detected")
        self.assertIsNotNone(event)
        self.assertEqual(list(event.data_schema["properties"]), ["text"])


class CoreDispatchTests(unittest.TestCase):
    def manager(self, root):
        actions, events = ActionRegistry(), EventRegistry()
        register_core_protocol(actions, events)
        bus = EventBus(events, debug=Debugger(enabled=False))
        manager = CapabilityManager(
            bus,
            actions,
            events,
            root=root,
            debug=Debugger(enabled=False),
        )
        agent = _Agent()
        bus.manager = _Manager(agent)
        return manager, actions, agent

    def test_core_action_runs_in_process_and_returns_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            manager, actions, agent = self.manager(Path(temporary))
            try:
                with patch.object(
                    speech_service,
                    "speak_result",
                    return_value={"status": "successful"},
                ):
                    manager.dispatch(
                        action=ActionRequest("speech", {"text": "привет"}, "sp-1"),
                        spec=actions.require("speech"),
                        agent=agent,
                    )
                    deadline = time.time() + 5
                    while not agent.results and time.time() < deadline:
                        time.sleep(0.01)
                self.assertEqual(
                    agent.results[0].model_value(),
                    {
                        "event_id": "call_result",
                        "call_id": "sp-1",
                        "data": {"status": "successful"},
                    },
                )
            finally:
                manager.shutdown()

    def test_unavailable_speech_fails_action(self):
        self.assertFalse(speech_service.available)
        with self.assertRaisesRegex(RuntimeError, "speech_unavailable"):
            speech_service.speak_result("тест")

if __name__ == "__main__":
    unittest.main()
