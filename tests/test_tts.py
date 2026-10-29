import contextlib
import io
import queue
import socket
import subprocess
import sys
import threading
import unittest
import tempfile
from pathlib import Path
from concurrent.futures import Future
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch, Mock

from jarvis.speech import tts


class _Player:
    def __init__(self):
        self.terminated = False

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True


class TtsTests(unittest.TestCase):
    def test_playback_restores_system_audio_on_success_and_stream_failure(self):
        from jarvis.speech.config import build_config
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as temporary:
                speaker = tts.Speaker(build_config(Path(temporary)))
                request = tts._SpeechRequest("text", Future(), threading.Event())
                player = Mock(pid=100)
                def chunks():
                    yield b"audio"
                    if fail:
                        raise RuntimeError("stream failed")
                with patch.object(tts.subprocess, "Popen", return_value=player) as spawn, patch.object(tts, "SystemAudioMute") as mute:
                    if fail:
                        with self.assertRaisesRegex(RuntimeError, "stream failed"):
                            speaker._play(chunks(), 1, request)
                    else:
                        speaker._play(chunks(), 1, request)
                mute.assert_called_once_with(100)
                mute.return_value.start.assert_called_once()
                mute.return_value.close.assert_called_once()
                self.assertIsNone(speaker._audio_mute)
                environment = spawn.call_args.kwargs["env"]
                self.assertEqual(environment["SDL_AUDIODRIVER"], "pulseaudio")
                self.assertIn("application.id=jarvis.speech", environment["PULSE_PROP"])
                argv = spawn.call_args.args[0]
                self.assertEqual(argv[argv.index("-volume") + 1], "100")
                self.assertNotIn("-af", argv)

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

    @unittest.skipUnless(sys.platform == "linux", "Linux parent-thread death regression")
    def test_server_survives_background_initializer_exit_and_stops_with_speaker(self):
        from jarvis.speech.config import build_config
        import requests
        server_code = """
import argparse
from http.server import HTTPServer, BaseHTTPRequestHandler
parser = argparse.ArgumentParser()
parser.add_argument('--host')
parser.add_argument('--port', type=int)
args, _ = parser.parse_known_args()
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'ready')
    def log_message(self, *args):
        pass
HTTPServer((args.host, args.port), Handler).serve_forever()
"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with socket.socket() as reservation:
                reservation.bind(('127.0.0.1', 0))
                port = reservation.getsockname()[1]
            config = replace(build_config(root), tts_url=f'http://127.0.0.1:{port}/generate')
            config.tts_model.parent.mkdir(parents=True)
            config.tts_model.write_bytes(b'test model placeholder')
            speaker = tts.Speaker(config)
            errors = []
            def initialize():
                try:
                    speaker._start_server()
                except Exception as exc:
                    errors.append(exc)
            try:
                with patch.object(speaker, '_resolve_binary', return_value=sys.executable), patch.object(speaker, '_base_cmd', return_value=[sys.executable, '-u', '-c', server_code]):
                    initializer = threading.Thread(target=initialize)
                    initializer.start()
                    initializer.join(timeout=5)
                self.assertFalse(initializer.is_alive())
                self.assertEqual(errors, [])
                server, owner = speaker._server, speaker._server_owner
                self.assertTrue(owner.is_alive())
                # Before the fix PDEATHSIG killed the server when initializer exited.
                with self.assertRaises(subprocess.TimeoutExpired):
                    server.wait(timeout=0.15)
                with requests.get(config.tts_url, timeout=2) as response:
                    self.assertEqual(response.content, b'ready')
            finally:
                speaker.stop()
            self.assertIsNotNone(server.poll())
            self.assertFalse(owner.is_alive())

    def test_server_launch_failure_is_reported_without_leaving_owner_thread(self):
        from jarvis.speech.config import build_config
        with tempfile.TemporaryDirectory() as temporary:
            speaker = tts.Speaker(build_config(Path(temporary)))
            speaker.cfg.tts_model.parent.mkdir(parents=True)
            speaker.cfg.tts_model.write_bytes(b'test')
            with patch.object(speaker, '_resolve_binary', return_value='s2'), patch.object(speaker, '_base_cmd', return_value=['s2']), patch.object(tts.subprocess, 'Popen', side_effect=OSError('launch failed')):
                with self.assertRaisesRegex(OSError, 'launch failed'):
                    speaker._start_server()
            self.assertIsNone(speaker._server)
            self.assertIsNone(speaker._server_owner)
            speaker.stop()

    def test_dead_owned_server_is_not_considered_ready_from_an_http_response(self):
        from jarvis.speech.config import build_config
        with tempfile.TemporaryDirectory() as temporary:
            speaker = tts.Speaker(build_config(Path(temporary)))
            speaker._server = Mock(returncode=-9)
            speaker._server.poll.return_value = -9
            with patch.object(speaker, '_http_ready', return_value=True) as ready:
                with self.assertRaisesRegex(RuntimeError, '-9'):
                    speaker._wait_server()
            ready.assert_not_called()

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
        speaker._audio_mute = Mock()

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
        speaker._audio_mute.close.assert_called_once()
        for request in [current, *queued]:
            self.assertTrue(request.interrupted.is_set())
            self.assertIsInstance(request.future.exception(), tts.SpeechInterrupted)
        with self.assertRaises(queue.Empty):
            speaker._queue.get_nowait()


if __name__ == "__main__":
    unittest.main()
