"""Installed-wheel smoke check: fake streaming API, no voice models/devices."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def run(python):
    seen = []
    lock = threading.Lock()

    class Api(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {"data": [{"id": "test", "input_modalities": ["text"]}]}
                ).encode()
            )

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            with lock:
                index = len(seen)
                seen.append(request)
            assert request["stream"] is True
            if index == 0:
                action = {
                    "action_id": "spawn_agent",
                    "call_id": "spawn-1",
                    "data": {"name": "builder", "preset": "module_manager"},
                }
            elif index == 1:
                action = {
                    "action_id": "reply",
                    "call_id": "reply-1",
                    "data": {"text": "Assistant ready"},
                }
            else:
                action = {
                    "action_id": "no_action",
                    "call_id": f"done-{index}",
                    "data": {},
                }
            content = json.dumps({"actions": [action]})
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            chunk = {
                "id": "fixture",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "test",
                "choices": [
                    {"index": 0, "delta": {"content": content}, "finish_reason": "stop"}
                ],
            }
            self.wfile.write(
                ("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode()
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Api)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env = root / ".env"
            env.write_text(
                f"LLM_MODEL=test\nLLM_API_KEY=test\nLLM_BASE_URL=http://127.0.0.1:{server.server_port}/v1\nLLM_REASONING_EFFORT=\nSTT_ENABLED=false\nTTS_ENABLED=true\nTTS_SERVER_BIN=/nonexistent/s2\nJARVIS_DIR=storage\n",
                encoding="utf-8",
            )
            environment = dict(os.environ)
            environment.pop("PYTHONPATH", None)
            for key in tuple(environment):
                if key.startswith(("LLM_", "STT_", "TTS_", "JARVIS_")):
                    environment.pop(key)
            process = subprocess.Popen(
                [
                    python,
                    "-m",
                    "jarvis",
                    "--env-file",
                    str(env),
                    "--message",
                    "Build a capability",
                ],
                cwd=root,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            try:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    with lock:
                        requests = list(seen)
                    rendered = json.dumps(requests, ensure_ascii=False)
                    if (
                        "builder" in rendered
                        and "user_message" in rendered
                        and "reply-1" in rendered
                    ):
                        records = list(
                            (root / "storage" / "memory" / "module_manager").glob(
                                "*/current/agent.json"
                            )
                        )
                        if records:
                            break
                    if process.poll() is not None:
                        raise AssertionError(process.communicate()[0])
                    time.sleep(0.05)
                else:
                    raise AssertionError(
                        "Installed wheel did not process a request and spawn a helper"
                    )
                system = requests[0]["messages"][0]["content"]
                catalog = json.loads(system.split("capabilities:\n", 1)[1])
                assert "speech" not in {
                    item["type"] for item in catalog["core_protocol"]["actions"]
                }
                assert not list((root / "storage" / "modules").iterdir())
                print(
                    "Installed-wheel startup, streaming, user_message, helper spawn, text reply and absent speech: OK"
                )
            finally:
                process.terminate()
                output, _ = process.communicate(timeout=10)
                assert "Assistant ready" in output
                if process.returncode not in (0, 143, -15):
                    raise AssertionError(output)
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    run(sys.argv[1])
