import importlib.util
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1] / ".jarvis" / "modules" / "mouse"


@unittest.skipUnless(ROOT.is_dir(), "Local mouse module is not installed")
class MouseTests(unittest.TestCase):
    def load_action(self, name):
        spec = importlib.util.spec_from_file_location("mouse_" + name, ROOT / "actions" / name / "action.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_click_uses_native_button_mask_without_timeout(self):
        action = self.load_action("click")
        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            result = action.run({"button": "right"}, SimpleNamespace())
        self.assertEqual(result, {"status": "success", "error": None})
        self.assertEqual(run.call_args.args[0], ["ydotool", "click", "0xc1"])
        self.assertNotIn("timeout", run.call_args.kwargs)

    def test_drag_releases_button_when_movement_fails(self):
        action = self.load_action("drag")
        results = [subprocess.CompletedProcess([], 0, "", ""), subprocess.CompletedProcess([], 1, "", "movement failed"), subprocess.CompletedProcess([], 0, "", "")]
        with patch("subprocess.run", side_effect=results) as run:
            result = action.run({"x": 120, "y": 240, "button": "left"}, SimpleNamespace())
        self.assertEqual(result["status"], "error")
        self.assertEqual(run.call_args_list[-1].args[0], ["ydotool", "click", "0x80"])

    def test_move_uses_pixel_coordinates_without_screenshot(self):
        action = self.load_action("move")
        complete = Mock()
        def command(args, **kwargs):
            self.assertNotIn("timeout", kwargs)
            self.assertEqual(args[0], "ydotool")
            return subprocess.CompletedProcess(args, 0, "", "")
        with patch("subprocess.run", side_effect=command) as run:
            result = action.run({"x": 123, "y": 456}, SimpleNamespace(complete=complete))
        self.assertEqual(run.call_args_list[0].args[0], ["ydotool", "mousemove", "-a", "-x", "123", "-y", "456"])
        self.assertEqual(run.call_count, 1)
        self.assertEqual(result, {"status": "success", "error": None})
        complete.assert_not_called()

    def test_no_mouse_action_creates_screenshots(self):
        arguments = {
            "move": {"x": 1, "y": 2},
            "relative_move": {"x": 1, "y": 2},
            "scroll": {"steps": 2},
            "drag": {"x": 1, "y": 2, "button": "left"},
            "double_drag": {"x": 1, "y": 2, "button": "left"},
            "click": {"button": "left"},
            "double_click": {"button": "left"},
        }
        for name, data in arguments.items():
            with self.subTest(action=name):
                action = self.load_action(name)
                complete = Mock()
                with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
                    result = action.run(data, SimpleNamespace(complete=complete))
                self.assertEqual(result, {"status": "success", "error": None})
                self.assertTrue(all(call.args[0][0] == "ydotool" for call in run.call_args_list))
                complete.assert_not_called()
