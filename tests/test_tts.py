import contextlib
import importlib
import io
import queue
import threading
import unittest
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jarvis.core.registry import ActionRegistry, EventRegistry
from jarvis.core.runtime import EventBus
from jarvis.infrastructure.debug import Debugger
from jarvis.modules.manager import ModuleManager


class _Player:
    def __init__(self):
        self.terminated = False

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True


class TtsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        events = EventRegistry()
        actions = ActionRegistry()
        bus = EventBus(events, debug=Debugger(enabled=False))
        cls.manager = ModuleManager(
            bus,
            actions,
            events,
            modules_dir=Path(".jarvis/modules"),
            debug=Debugger(enabled=False),
        )
        cls.manager.load("speech_output", start_handlers=False)
        package = cls.manager._loaded["speech_output"].python_module.__name__
        cls.tts = importlib.import_module(package + ".tts")

    @classmethod
    def tearDownClass(cls):
        cls.manager.unload("speech_output")

    def test_interrupted_stream_error_is_not_reported_as_synthesis_error(self):
        speaker = self.tts.Speaker.__new__(self.tts.Speaker)
        speaker.cfg = SimpleNamespace(
            tts_url="http://127.0.0.1:3030/generate",
            tts_voice="jarvis",
            tts_voice_dir="assets/voices",
        )
        speaker._lock = threading.RLock()
        speaker._response = None
        request = self.tts._SpeechRequest("текст", Future(), threading.Event())
        speaker._stop = threading.Event()
        request.interrupted.set()
        stderr = io.StringIO()

        with patch.object(
            self.tts.requests,
            "post",
            side_effect=AttributeError("'NoneType' object has no attribute 'read'"),
        ), contextlib.redirect_stderr(stderr):
            with self.assertRaises(self.tts.SpeechInterrupted):
                speaker._speak(request)

        self.assertEqual(stderr.getvalue(), "")

    def test_interrupt_marks_current_and_queued_speech_as_interrupted(self):
        speaker = self.tts.Speaker.__new__(self.tts.Speaker)
        speaker._queue = queue.Queue()
        speaker._lock = threading.RLock()
        speaker._response = None
        speaker._player = _Player()

        current = self.tts._SpeechRequest("текущая", Future(), threading.Event())
        queued = [
            self.tts._SpeechRequest(f"очередь {index}", Future(), threading.Event())
            for index in range(2)
        ]
        speaker._current = current
        for request in queued:
            speaker._queue.put(request)

        self.assertEqual(speaker.interrupt(), 3)
        self.assertTrue(speaker._player.terminated)
        for request in [current, *queued]:
            self.assertTrue(request.interrupted.is_set())
            self.assertIsInstance(
                request.future.exception(), self.tts.SpeechInterrupted
            )
        with self.assertRaises(queue.Empty):
            speaker._queue.get_nowait()


if __name__ == "__main__":
    unittest.main()
