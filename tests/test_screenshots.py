"""Контракт модуля screenshots: fire-and-forget action + broadcast screenshot.

Проверяется точное событие:
  {"type": "screenshot", "data": {"text": "<имя>_jarvis.png"}}
PNG идёт только отдельной image_url-частью OpenAI message, base64 никогда
не попадает в data текстом. Реальный hyprshot подменяется стабом в PATH,
HOME переносится в песочницу.
"""

import base64
import importlib.util
import json
import os
import re
import stat
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path


PIXEL_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
    "AAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
NAME_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{6}_jarvis\.png$")
MODULE_DIR = Path(__file__).resolve().parents[1] / ".jarvis" / "modules" / "screenshots"


def _wait(predicate, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


class ScreenshotsContractTests(unittest.TestCase):
    def test_capture_fires_and_handler_broadcasts_exact_screenshot(self):
        sandbox = Path(tempfile.mkdtemp(prefix="screenshots-contract-"))
        self.addCleanup(lambda: os.environ.pop("HOME", None))
        old_home = os.environ.get("HOME")
        os.environ["HOME"] = str(sandbox)
        self.addCleanup(
            lambda: os.environ.__setitem__("HOME", old_home)
            if old_home is not None
            else None
        )
        old_path = os.environ.get("PATH", "")

        screens = sandbox / "Images" / "Screenshots"
        screens.mkdir(parents=True)
        (screens / "old_file.png").write_bytes(base64.b64decode(PIXEL_PNG_B64))
        pixel = sandbox / "pixel.png"
        pixel.write_bytes(base64.b64decode(PIXEL_PNG_B64))

        bin_dir = sandbox / "bin"
        bin_dir.mkdir()
        stub = bin_dir / "hyprshot"
        stub.write_text(
            "#!/usr/bin/env bash\n"
            'outdir=""; fname=""\n'
            "while [[ $# -gt 0 ]]; do\n"
            '  case "$1" in -o) outdir="$2"; shift 2;; -f) fname="$2"; shift 2;; *) shift;; esac\n'
            "done\n"
            'mkdir -p "$outdir"\n'
            f'cp "{pixel}" "$outdir/$fname"\n',
            encoding="utf-8",
        )
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        os.environ["PATH"] = str(bin_dir) + os.pathsep + old_path
        self.addCleanup(lambda: os.environ.__setitem__("PATH", old_path))

        package = types.ModuleType("screenshots_contract_pkg")
        package.__path__ = [str(MODULE_DIR)]
        sys.modules["screenshots_contract_pkg"] = package
        spec = importlib.util.spec_from_file_location(
            "screenshots_contract_pkg.module", MODULE_DIR / "module.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules["screenshots_contract_pkg.module"] = module
        spec.loader.exec_module(module)
        try:
            built = module.create_module()
            self.assertEqual(
                [a.type for a in built.actions], ["screenshots.capture"]
            )
            self.assertEqual([e.type for e in built.handlers[0].events], ["screenshot"])

            from jarvis.modules import ModuleContext

            collected = []
            ctx = ModuleContext(
                module_id="screenshots",
                module_path=MODULE_DIR,
                emit_event=collected.append,
            )
            from screenshots_contract_pkg.handlers.monitor import build_monitor
            from screenshots_contract_pkg.actions.capture import build_capture

            thread = threading.Thread(
                target=build_monitor, args=(ctx,), daemon=True
            )
            thread.start()
            self.addCleanup(lambda: (ctx.stop_event.set(), thread.join(timeout=3)))

            began = time.monotonic()
            build_capture({}, None)
            elapsed = time.monotonic() - began
            self.assertLess(elapsed, 5.0, "action не ждёт завершения команды")

            self.assertTrue(
                _wait(lambda: len(collected) == 1),
                f"ожидалось ровно одно событие, получено: {len(collected)}",
            )
            event = collected[0]
            self.assertIsNone(event.target, "screenshot рассылается всем, без target")
            self.assertEqual(
                event.model_value()["type"], "screenshot",
            )
            self.assertEqual(set(event.model_value()["data"]), {"text"})
            name = event.model_value()["data"]["text"]
            self.assertRegex(name, NAME_PATTERN)
            self.assertNotIn("base64", name)

            message = event.model_message()
            self.assertEqual(message["role"], "user")
            self.assertIsInstance(message["content"], list)
            self.assertEqual(len(message["content"]), 2)
            self.assertEqual(
                message["content"][0],
                {"type": "text", "text": json.dumps(event.model_value(), ensure_ascii=False, separators=(",", ":"))},
            )
            image_part = message["content"][1]
            self.assertEqual(image_part["type"], "image_url")
            url = image_part["image_url"]["url"]
            self.assertTrue(url.startswith("data:image/png;base64,"))
            self.assertTrue(
                base64.b64decode(url.split(",", 1)[1]).startswith(b"\x89PNG")
            )
        finally:
            for key in (
                "screenshots_contract_pkg",
                "screenshots_contract_pkg.module",
            ):
                sys.modules.pop(key, None)


if __name__ == "__main__":
    unittest.main()
