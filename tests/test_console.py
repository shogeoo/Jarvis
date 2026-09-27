import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class ConsoleTests(unittest.TestCase):
    def test_runtime_captures_python_native_and_child_noise(self):
        with tempfile.TemporaryDirectory() as temporary:
            script = '''
import os, subprocess, sys
from pathlib import Path
from jarvis.infrastructure.console import runtime_console, logger
from jarvis.infrastructure.debug import Debugger
with runtime_console(Path(sys.argv[1])) as stream:
    debug = Debugger(stream=stream, buffered=True)
    debug.initializing()
    print("python diagnostic")
    os.write(1, b"native stdout\\n")
    os.write(2, b"native stderr\\n")
    subprocess.run([sys.executable, "-c", "print('child diagnostic')"], check=True)
    logger.error("API error")
    debug.initialized()
    debug.message({"type": "system_started", "data": {}})
    debug.message({"actions": []})
    print("shutdown diagnostic")
'''
            result = subprocess.run([sys.executable, "-c", script, temporary], capture_output=True, text=True, check=True)
            expected = "Инициализация системы Jarvis....\nСистема инициализирована.\n\n"
            expected += json.dumps({"type": "system_started", "data": {}}, indent=2) + "\n\n"
            expected += json.dumps({"actions": []}, indent=2) + "\n"
            self.assertEqual(result.stdout, expected)
            self.assertEqual(result.stderr, "")
            log = (Path(temporary) / "runtime/logs/core.log").read_text()
            for message in ("python diagnostic", "native stdout", "native stderr", "child diagnostic", "API error", "shutdown diagnostic"):
                self.assertIn(message, log)
