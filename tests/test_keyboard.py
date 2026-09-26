import multiprocessing
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from jarvis.capabilities.worker import load_unit
from jarvis.core.protocol import validate_json


ROOT = Path(__file__).resolve().parents[1] / ".jarvis"


@unittest.skipUnless((ROOT / "modules" / "keyboard" / "module.py").is_file(), "Local keyboard module is not installed")
class KeyboardTests(unittest.TestCase):
    def setUp(self):
        self.unit = load_unit(ROOT, Path("modules/keyboard"))
        self.keys = sys.modules[self.unit.actions["keyboard.key_down"].run.__module__.rsplit(".actions.", 1)[0] + ".keys"]
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.keys.LOCK_PATH = Path(temporary.name) / "input.lock"
        self.context = SimpleNamespace(complete=Mock())

    def run_action(self, name, data):
        spec = self.unit.actions["keyboard." + name]
        validate_json(data, spec.args_schema)
        result = spec.run(data, self.context)
        validate_json(result, spec.result_schema)
        return result

    def test_exactly_five_actions_with_key_names_and_numeric_codes(self):
        self.assertEqual(set(self.unit.actions), {"keyboard." + name for name in ("type_text", "press_key", "key_down", "key_up", "hotkey")})
        self.assertEqual(self.keys.keycode("CTRL"), 29)
        self.assertEqual(self.keys.keycode("ENTER"), 28)
        self.assertEqual(self.keys.keycode("KEY_F24"), 194)
        self.assertEqual(self.keys.keycode(30), 30)

    def text_command(self, args, **kwargs):
        device = {"name": "ydotoold-virtual-device", "rules": "", "model": "", "layout": "us,ru", "variant": "", "options": "", "active_layout_index": 0, "capsLock": False}
        stdout = json.dumps({"keyboards": [device]}) if args == ["hyprctl", "-j", "devices"] else ""
        return subprocess.CompletedProcess(args, 0, stdout, "")

    def test_text_physically_types_english_russian_and_restores_layout(self):
        with patch("subprocess.run", side_effect=self.text_command) as run:
            result = self.run_action("type_text", {"text": "AП"})
        self.assertEqual(result["status"], "success")
        self.assertEqual([call.args[0] for call in run.call_args_list], [
            ["hyprctl", "-j", "devices"],
            ["ydotool", "key", "42:1"], ["ydotool", "key", "30:1"],
            ["ydotool", "key", "30:0"], ["ydotool", "key", "42:0"],
            ["hyprctl", "switchxkblayout", "ydotoold-virtual-device", "1"],
            ["ydotool", "key", "42:1"], ["ydotool", "key", "34:1"],
            ["ydotool", "key", "34:0"], ["ydotool", "key", "42:0"],
            ["hyprctl", "switchxkblayout", "ydotoold-virtual-device", "0"],
        ])
        self.assertTrue(all("timeout" not in call.kwargs for call in run.call_args_list))
        self.assertFalse(any(self.keys.held))
        self.context.complete.assert_not_called()

    def test_unrepresentable_character_fails_before_any_keypress(self):
        with patch("subprocess.run", side_effect=self.text_command) as run:
            result = self.run_action("type_text", {"text": "hello🙂"})
        self.assertEqual(result["status"], "error")
        self.assertIn("U+1F642", result["error"])
        self.assertEqual(run.call_count, 1)

    def test_typing_failure_releases_keys_and_restores_layout(self):
        def command(args, **kwargs):
            if args == ["ydotool", "key", "34:1"]:
                return subprocess.CompletedProcess(args, 1, "", "key failed")
            return self.text_command(args, **kwargs)
        with patch("subprocess.run", side_effect=command) as run:
            result = self.run_action("type_text", {"text": "П"})
        self.assertEqual(result["status"], "error")
        self.assertIn(["ydotool", "key", "34:0"], [call.args[0] for call in run.call_args_list])
        self.assertEqual(run.call_args.args[0], ["hyprctl", "switchxkblayout", "ydotoold-virtual-device", "0"])
        self.assertFalse(any(self.keys.held))

    def test_caps_lock_is_respected_without_toggling_it(self):
        def command(args, **kwargs):
            result = self.text_command(args, **kwargs)
            if args == ["hyprctl", "-j", "devices"]:
                value = json.loads(result.stdout)
                value["keyboards"][0]["capsLock"] = True
                result.stdout = json.dumps(value)
            return result
        with patch("subprocess.run", side_effect=command) as run:
            result = self.run_action("type_text", {"text": "Aa"})
        self.assertEqual(result["status"], "success")
        events = [call.args[0][-1] for call in run.call_args_list if call.args[0][0] == "ydotool"]
        self.assertEqual(events, ["30:1", "30:0", "42:1", "30:1", "30:0", "42:0"])

    def test_hotkey_releases_in_reverse_order_and_preserves_held_modifier(self):
        self.keys.held[29] = 1
        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            result = self.run_action("hotkey", {"keys": ["CTRL", "SHIFT", "S"]})
        self.assertEqual(result["status"], "success")
        self.assertEqual([call.args[0][-1] for call in run.call_args_list], ["42:1", "31:1", "31:0", "42:0"])
        self.assertEqual(self.keys.held[29], 1)
        self.assertEqual(self.keys.held[42], 0)

    def test_failed_hotkey_releases_every_new_key(self):
        calls = []
        def command(args, **kwargs):
            calls.append(args[-1])
            return subprocess.CompletedProcess(args, 1 if args[-1] == "31:1" else 0, "", "key failed")
        with patch("subprocess.run", side_effect=command):
            result = self.run_action("hotkey", {"keys": ["CTRL", "S"]})
        self.assertEqual(result["status"], "error")
        self.assertEqual(calls, ["29:1", "31:1", "31:0", "29:0"])
        self.assertFalse(any(self.keys.held))

    def test_key_down_survives_invocation_process_and_unload_releases_it(self):
        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")):
            child = multiprocessing.get_context("fork").Process(target=self.run_action, args=("key_down", {"key": "W"}))
            child.start()
            child.join(2)
            self.assertEqual(child.exitcode, 0)
        self.assertEqual(self.keys.held[17], 1)
        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            self.unit.teardown[0](SimpleNamespace())
        self.assertEqual(run.call_args.args[0], ["ydotool", "key", "17:0"])
        self.assertFalse(any(self.keys.held))

    def test_press_key_sends_down_and_up_and_key_up_clears_state(self):
        with patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            self.assertEqual(self.run_action("press_key", {"key": "ENTER"})["status"], "success")
            self.assertEqual([call.args[0][-1] for call in run.call_args_list], ["28:1", "28:0"])
            self.keys.held[28] = 1
            self.assertEqual(self.run_action("key_up", {"key": 28})["status"], "success")
            self.assertEqual(self.keys.held[28], 0)
