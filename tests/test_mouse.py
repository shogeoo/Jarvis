import subprocess
import tempfile
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from jarvis.capabilities.worker import load_unit
from jarvis.core.protocol import validate_json


ROOT = Path(__file__).resolve().parents[1] / ".jarvis" / "modules" / "mouse"


@unittest.skipUnless(ROOT.is_dir(), "Local mouse module is not installed")
class MouseTests(unittest.TestCase):
    def setUp(self):
        self.unit = load_unit(ROOT.parents[1], Path("modules/mouse"))
        self.buttons = sys.modules[self.unit.actions["mouse.hold_button"].run.__module__.rsplit(".actions.", 1)[0] + ".buttons"]
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.buttons.LOCK_PATH = Path(temporary.name) / "buttons.lock"

    def load_action(self, name):
        return SimpleNamespace(run=self.unit.actions["mouse." + name].run)

    def test_click_uses_native_button_mask_without_timeout(self):
        action = self.load_action("click")
        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            result = action.run({"button": "right"}, SimpleNamespace())
        self.assertEqual(result, {"status": "success", "error": None})
        self.assertEqual(run.call_args_list[0].args[0], ["ydotool", "click", "0xc1"])
        self.assertEqual(run.call_args.args[0], ["ydotool", "click", "0x81"])
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

    def test_timed_hold_requires_duration_and_releases_button(self):
        spec = self.unit.actions["mouse.hold_button"]
        with self.assertRaises(ValueError):
            validate_json({"button": "left"}, spec.args_schema)
        stop_event = Mock()
        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            result = spec.run({"button": "middle", "duration_ms": 1500}, SimpleNamespace(stop_event=stop_event))
        stop_event.wait.assert_called_once_with(1.5)
        self.assertEqual(result, {"status": "success", "error": None})
        self.assertEqual([call.args[0][-1] for call in run.call_args_list], ["0x42", "0x82"])
        self.assertFalse(any(self.buttons.owners))

    def test_button_down_up_and_module_unload(self):
        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            self.load_action("button_down").run({"button": "left"}, SimpleNamespace())
            self.assertTrue(self.buttons.owners[0])
            self.load_action("button_up").run({"button": "left"}, SimpleNamespace())
            self.assertFalse(self.buttons.owners[0])
            self.load_action("button_down").run({"button": "right"}, SimpleNamespace())
            self.unit.teardown[0](SimpleNamespace())
            self.assertEqual(run.call_args.args[0], ["ydotool", "click", "0x81"])
        self.assertFalse(any(self.buttons.owners))

    def test_cancelled_hold_releases_button(self):
        stop_event = Mock()
        stop_event.wait.side_effect = SystemExit("cancelled")
        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            with self.assertRaises(SystemExit):
                self.load_action("hold_button").run({"button": "left", "duration_ms": 5000}, SimpleNamespace(stop_event=stop_event))
        self.assertEqual(run.call_args.args[0], ["ydotool", "click", "0x80"])
        self.assertFalse(any(self.buttons.owners))

    def test_timed_hold_does_not_release_a_newer_button_down(self):
        stop_event = Mock()
        def during_hold(duration):
            self.load_action("button_up").run({"button": "left"}, SimpleNamespace())
            self.load_action("button_down").run({"button": "left"}, SimpleNamespace())
        stop_event.wait.side_effect = during_hold
        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            self.load_action("hold_button").run({"button": "left", "duration_ms": 5000}, SimpleNamespace(stop_event=stop_event))
        self.assertEqual([call.args[0][-1] for call in run.call_args_list], ["0x40", "0x80", "0x40"])
        self.assertTrue(self.buttons.owners[0])

    def test_cancelled_drag_also_releases_button(self):
        def command(args, **kwargs):
            if args[1] == "mousemove":
                raise SystemExit("cancelled")
            return subprocess.CompletedProcess(args, 0, "", "")
        with patch("subprocess.run", side_effect=command) as run:
            with self.assertRaises(SystemExit):
                self.load_action("drag").run({"x": 100, "y": 200, "button": "left"}, SimpleNamespace())
        self.assertEqual(run.call_args.args[0], ["ydotool", "click", "0x80"])
        self.assertFalse(any(self.buttons.owners))
