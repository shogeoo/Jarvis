"""Контракт take_screenshot: action возвращает PNG в результате.

Проверяются:
  данные {"filename": "<имя>", "error": null};
  image_url-часть с PNG в том же сообщении результата;
  base64 никогда не попадает в data текстом.
Реальный hyprshot подменяется стабом в PATH, HOME переносится в песочницу.
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
import unittest
from pathlib import Path

from jarvis.capabilities import PENDING

PIXEL_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
    "AAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
NAME_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{6}_jarvis\.png$")
ROOT = Path(__file__).resolve().parents[1]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return module


def _wait(predicate, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


class _Context:
    def __init__(self):
        self.completed = []
        self.complete = lambda data, parts=(): self.completed.append((data, parts))


class ScreenshotsContractTests(unittest.TestCase):
    def test_capture_returns_png_in_action_result(self):
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

        capture = _load(
            "screenshots_contract_capture",
            ROOT / "actions" / "take_screenshot" / "action.py",
        )
        self.addCleanup(lambda: sys.modules.pop("screenshots_contract_capture", None))
        action = capture.create_action()
        self.assertEqual(action.id, "")

        context = _Context()
        outcome = capture.run({}, context)
        self.assertIs(outcome, PENDING)
        self.assertEqual(
            len(context.completed), 1, "действие обязано вернуть ровно один результат"
        )
        data, parts = context.completed[0]
        self.assertRegex(data["filename"], NAME_PATTERN)
        self.assertIsNone(data["error"])
        self.assertNotIn("base64", json.dumps(data))
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0].type, "image")

        from jarvis.core.protocol import ActionResult

        result = ActionResult(
            action_id="shot-1", data=data, agent_id="main", parts=tuple(parts)
        )
        message = result.model_message()
        self.assertEqual(message["role"], "user")
        self.assertIsInstance(message["content"], list)
        self.assertEqual(len(message["content"]), 2)
        self.assertEqual(
            message["content"][0],
            {"type": "text", "text": result.model_content()},
        )
        image_part = message["content"][1]
        self.assertEqual(image_part["type"], "image_url")
        url = image_part["image_url"]["url"]
        self.assertTrue(url.startswith("data:image/png;base64,"))
        self.assertTrue(
            base64.b64decode(url.split(",", 1)[1]).startswith(b"\x89PNG")
        )

    def test_capture_reports_missing_file_as_error_data(self):
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
        bin_dir = sandbox / "bin"
        bin_dir.mkdir()
        stub = bin_dir / "hyprshot"
        stub.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        os.environ["PATH"] = str(bin_dir) + os.pathsep + old_path
        self.addCleanup(lambda: os.environ.__setitem__("PATH", old_path))

        capture = _load(
            "screenshots_contract_capture_missing",
            ROOT / "actions" / "take_screenshot" / "action.py",
        )
        self.addCleanup(
            lambda: sys.modules.pop("screenshots_contract_capture_missing", None)
        )
        capture.WAIT_SECONDS = 1
        capture.POLL_SECONDS = 0.05
        context = _Context()
        outcome = capture.run({}, context)
        # Файл не появился: либо синхронная ошибка в данных, либо
        # отложенный результат через complete.
        if outcome is PENDING:
            self.assertTrue(_wait(lambda: len(context.completed) == 1))
            data, parts = context.completed[0]
            self.assertIsNone(data["filename"])
            self.assertTrue(data["error"])
            self.assertEqual(parts, ())
        else:
            self.assertIsNone(outcome["filename"])
            self.assertTrue(outcome["error"])


if __name__ == "__main__":
    unittest.main()
