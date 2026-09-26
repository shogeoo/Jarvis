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

    def test_move_uses_pixel_coordinates_and_returns_named_image_once(self):
        action = self.load_action("move")
        complete = Mock()
        def command(args, **kwargs):
            self.assertNotIn("timeout", kwargs)
            if args[0] == "hyprshot":
                (Path(args[6]) / args[8]).write_bytes(b"png")
            return subprocess.CompletedProcess(args, 0, "", "")
        with patch("subprocess.run", side_effect=command) as run:
            action.run({"x": 123, "y": 456}, SimpleNamespace(complete=complete))
        self.assertEqual(run.call_args_list[0].args[0], ["ydotool", "mousemove", "-a", "-x", "123", "-y", "456"])
        complete.assert_called_once()
        self.assertTrue(complete.call_args.kwargs["parts"][0].name.endswith(".png"))
