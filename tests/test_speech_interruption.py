"""An interrupted speech result and the user's utterance form one input batch."""

from concurrent.futures import Future
from types import SimpleNamespace
import unittest

import test_system_state
from jarvis.capabilities.api import PENDING
from jarvis.core.protocol import Event
from jarvis.speech.service import SpeechService
from jarvis.speech.tts import SpeechInterrupted
from test_runtime import _Response


class InterruptedSpeechTests(unittest.TestCase):
    def test_interrupted_result_is_deferred_until_recognition(self):
        service = SpeechService()
        service.available = True
        future = Future()
        future.set_exception(SpeechInterrupted("interrupted"))
        service._speaker = SimpleNamespace(submit=lambda text: future)
        completed = []
        emitted = []
        result = service.speak_result(
            "A sentence.", interrupted_complete=lambda data: completed.append(data)
        )
        self.assertIs(result, PENDING)
        self.assertEqual(completed, [])

        def emit(event):
            for complete in event.get("_interrupted_completions", []):
                complete({"status": "interrupted"})
            emitted.append(event["text"])

        service._recognized_speech({"text": "New utterance."}, emit)
        self.assertEqual(completed, [{"status": "interrupted"}])
        self.assertEqual(emitted, ["New utterance."])
        service._recognized_speech({"text": "Another utterance."}, emit)
        self.assertEqual(len(completed), 1)

    def test_successful_speech_is_not_deferred(self):
        service = SpeechService()
        service.available = True
        future = Future()
        future.set_result({})
        service._speaker = SimpleNamespace(submit=lambda text: future)
        self.assertEqual(
            service.speak_result(
                "Done.",
                interrupted_complete=lambda data: self.fail("Unexpected interruption"),
            ),
            {"status": "successful"},
        )
        self.assertEqual(service._speech_completions, [])


class SpeechBatchTests(unittest.TestCase):
    setUp = test_system_state.SystemStateTests.setUp

    def test_result_precedes_new_speech_in_one_inference(self):
        self.main.stop()
        self.main._stop.clear()
        requests = []

        def request(**options):
            requests.append(options)
            return _Response(
                '{"actions":[{"action_id":"no_action","call_id":"after_interrupt","data":{}}]}'
            )

        self.manager.client.chat.completions.create = request
        self.manager.results.begin(
            agent_id="main",
            call_id="spoken",
            action_id="speech",
            result_schema=self.actions.require("speech").result_schema,
        )
        complete = lambda data: self.capabilities._complete_action(
            "main", "spoken", data
        )
        event = Event(
            "speech_detected",
            {"text": "Let me finish."},
            target="main",
            handler_id="core:speech",
        )
        self.assertTrue(
            self.manager.deliver_speech_with_interruptions(event, [complete])
        )
        items = []
        while not self.main._events.empty():
            item = self.main._events.get_nowait()
            if item is not None:
                items.append(item)
        self.assertEqual(
            [item.model_value()["event_id"] for item in items],
            ["call_result", "speech_detected"],
        )
        self.main._turn(items, self.main._generation)
        self.assertEqual(len(requests), 1)
        self.assertEqual(
            [item.model_value() for item in items],
            [
                {
                    "event_id": "call_result",
                    "call_id": "spoken",
                    "data": {"status": "interrupted"},
                },
                {"event_id": "speech_detected", "data": {"text": "Let me finish."}},
            ],
        )
        self.assertEqual(len(self.main.history), 4)

    def test_multiple_interrupted_calls_are_finalized_once_before_user_text(self):
        self.main.stop()
        self.main._stop.clear()
        callbacks = []
        for call_id in ("speech_one", "speech_two"):
            self.manager.results.begin(
                agent_id="main",
                call_id=call_id,
                action_id="speech",
                result_schema=self.actions.require("speech").result_schema,
            )
            callbacks.append(
                lambda data, call_id=call_id: self.capabilities._complete_action(
                    "main", call_id, data
                )
            )
        event = Event("speech_detected", {"text": "New text."}, target="main")
        self.manager.deliver_speech_with_interruptions(event, callbacks)
        self.assertFalse(self.manager.results.has_pending("main", "speech_one"))
        self.assertFalse(self.manager.results.has_pending("main", "speech_two"))
        for callback in callbacks:
            self.assertFalse(callback({"status": "interrupted"}))
        queued = [item for item in self.main._events.queue if item is not None]
        self.assertEqual(
            [item.model_value()["event_id"] for item in queued],
            ["call_result", "call_result", "speech_detected"],
        )
