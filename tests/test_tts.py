import queue
import contextlib
import io
import threading
import unittest
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import patch

from jarvis.tts import Speaker, SpeechInterrupted, _SpeechRequest


class _Player:
    def __init__(self):
        self.terminated = False

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True


class TtsTests(unittest.TestCase):
    def test_interrupted_stream_error_is_not_reported_as_synthesis_error(self):
        speaker = Speaker.__new__(Speaker)
        speaker.cfg = SimpleNamespace(
            tts_url="http://127.0.0.1:3030/generate",
            tts_voice="jarvis",
            tts_voice_dir="assets/voices",
        )
        speaker._lock = threading.RLock()
        speaker._response = None
        request = _SpeechRequest("текст", Future(), threading.Event())
        speaker._stop = threading.Event()
        request.interrupted.set()
        stderr = io.StringIO()

        with patch(
            "jarvis.tts.requests.post",
            side_effect=AttributeError("'NoneType' object has no attribute 'read'"),
        ), contextlib.redirect_stderr(stderr):
            with self.assertRaises(SpeechInterrupted):
                speaker._speak(request)

        self.assertEqual(stderr.getvalue(), "")

    def test_interrupt_marks_current_and_queued_speech_as_interrupted(self):
        speaker = Speaker.__new__(Speaker)
        speaker._queue = queue.Queue()
        speaker._lock = threading.RLock()
        speaker._response = None
        speaker._player = _Player()

        current = _SpeechRequest("текущая", Future(), threading.Event())
        queued = [
            _SpeechRequest(f"очередь {index}", Future(), threading.Event())
            for index in range(2)
        ]
        speaker._current = current
        for request in queued:
            speaker._queue.put(request)

        self.assertEqual(speaker.interrupt(), 3)
        self.assertTrue(speaker._player.terminated)
        for request in [current, *queued]:
            self.assertTrue(request.interrupted.is_set())
            self.assertIsInstance(request.future.exception(), SpeechInterrupted)
            self.assertEqual(str(request.future.exception()), "interrupted")
        with self.assertRaises(queue.Empty):
            speaker._queue.get_nowait()


if __name__ == "__main__":
    unittest.main()
