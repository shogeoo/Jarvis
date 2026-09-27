import contextlib
import io
import queue
import threading
import unittest
import tempfile
from pathlib import Path
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import patch

from jarvis.speech import tts


class _Player:
    def __init__(self):
        self.terminated = False

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True


class TtsTests(unittest.TestCase):
    def test_voice_profile_creation_uses_cuda_arguments(self):
        from jarvis.speech.config import build_config
        with tempfile.TemporaryDirectory() as temporary:
            speaker = tts.Speaker(build_config(Path(temporary)))
            def create_profile():
                speaker._profile_path().write_bytes(b"profile")
            with patch.object(speaker, "_base_cmd", return_value=["s2"]), patch.object(tts.subprocess, "Popen") as process:
                process.return_value.wait.side_effect = create_profile
                process.return_value.returncode = 0
                speaker._ensure_voice()
            argv = process.call_args.args[0]
            index = argv.index("--cuda")
            self.assertEqual(argv[index:index + 4], ["--cuda", "0", "-ngl", "-1"])

    def test_interrupted_stream_error_is_not_reported_as_synthesis_error(self):
        speaker = tts.Speaker.__new__(tts.Speaker)
        speaker.cfg = SimpleNamespace(
            tts_url="http://127.0.0.1:3030/generate",
            tts_voice="jarvis",
            tts_voice_dir="assets/voices",
        )
        speaker._lock = threading.RLock()
        speaker._response = None
        request = tts._SpeechRequest("текст", Future(), threading.Event())
        speaker._stop = threading.Event()
        request.interrupted.set()
        stderr = io.StringIO()

        with patch.object(
            tts.requests,
            "post",
            side_effect=AttributeError("'NoneType' object has no attribute 'read'"),
        ), contextlib.redirect_stderr(stderr):
            with self.assertRaises(tts.SpeechInterrupted):
                speaker._speak(request)

        self.assertEqual(stderr.getvalue(), "")

    def test_interrupt_marks_current_and_queued_speech_as_interrupted(self):
        speaker = tts.Speaker.__new__(tts.Speaker)
        speaker._queue = queue.Queue()
        speaker._lock = threading.RLock()
        speaker._response = None
        speaker._player = _Player()

        current = tts._SpeechRequest("текущая", Future(), threading.Event())
        queued = [
            tts._SpeechRequest(f"очередь {index}", Future(), threading.Event())
            for index in range(2)
        ]
        speaker._current = current
        for request in queued:
            speaker._queue.put(request)

        self.assertEqual(speaker.interrupt(), 3)
        self.assertTrue(speaker._player.terminated)
        for request in [current, *queued]:
            self.assertTrue(request.interrupted.is_set())
            self.assertIsInstance(request.future.exception(), tts.SpeechInterrupted)
        with self.assertRaises(queue.Empty):
            speaker._queue.get_nowait()


if __name__ == "__main__":
    unittest.main()
