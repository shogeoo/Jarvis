"""Contracts of the shipped module_manager; no installed capabilities required."""

import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from jarvis.core.lifecycle import ProcessManager
from jarvis.core.registry import ActionRegistry, EventRegistry
from jarvis.core.runtime import register_core_protocol
from jarvis.core.prompts import read_environment
from jarvis.infrastructure.runtime_layout import ensure_runtime_layout
from jarvis.presets import PresetStore


class ModuleManagerContractTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.storage = self.root / "storage"
        ensure_runtime_layout(self.storage)
        self.presets = PresetStore(self.storage / "presets")
        self.actions = ActionRegistry()
        register_core_protocol(self.actions, EventRegistry())
        self.processes = ProcessManager()
        self.addCleanup(self.processes.stop)
        manager = SimpleNamespace(
            processes=self.processes,
            require_agent=lambda agent_id: SimpleNamespace(_stop=threading.Event()),
        )
        self.context = SimpleNamespace(
            config=SimpleNamespace(project_root=self.root),
            capabilities=SimpleNamespace(root=self.storage),
            agent_manager=SimpleNamespace(_manager=manager),
            agent_id="developer",
            call_id="test-command",
        )

    def test_module_manager_has_complete_sdk_and_system_development_tools(self):
        preset = self.presets.load("module_manager")
        prompt = preset.person_prompt + read_environment()
        for marker in (
            "СТРОЖАЙШЕ ЗАПРЕЩЕНО читать исходный код Jarvis",
            "action_definition",
            "handler_definition",
            "module_definition",
            "context.emit",
            "create_module",
            "ДОЖДИСЬ call_result",
        ):
            self.assertIn(marker, prompt)
        specs = self.actions.for_capabilities(
            modules=set(), actions=set(), developer=True
        )
        expected = {
            "read_file",
            "write_file",
            "edit_file",
            "execute_command",
            "send_message_to_agent",
        }
        self.assertTrue(expected.issubset(specs))
        self.assertTrue({"list_capabilities", "capability_info", "toggle_capability"}.isdisjoint(specs))
        self.assertEqual(self.presets.load("module_manager").actions, ())
        main = self.actions.for_capabilities(modules=set(), actions=set(), primary=True)
        self.assertTrue(
            {"read_file", "write_file", "edit_file", "execute_command"}.isdisjoint(main)
        )

    def test_file_tools_accept_absolute_paths_and_validate_ranges(self):
        with tempfile.TemporaryDirectory() as outside:
            target = Path(outside) / "nested" / "file.txt"
            self.actions.require("write_file").run(
                {"path": str(target), "content": "one\ntwo\nthree\n"}, self.context
            )
            result = self.actions.require("read_file").run(
                {"path": str(target), "start_line": 2, "end_line": 3}, self.context
            )
            self.assertEqual(result["content"], "2: two\n3: three")
            self.actions.require("edit_file").run(
                {
                    "path": str(target),
                    "start_line": 2,
                    "end_line": 2,
                    "content": "changed",
                },
                self.context,
            )
            self.assertEqual(target.read_text(), "one\nchanged\nthree\n")
            with self.assertRaises(ValueError):
                self.actions.require("edit_file").run(
                    {
                        "path": str(target),
                        "start_line": 0,
                        "end_line": 2,
                        "content": "bad",
                    },
                    self.context,
                )
            self.assertEqual(target.read_text(), "one\nchanged\nthree\n")

    def test_bash_tool_captures_streams_and_honors_cwd(self):
        result = self.actions.require("execute_command").run(
            {"command": "printf out; printf err >&2; exit 7", "cwd": str(self.root)},
            self.context,
        )
        self.assertEqual(result, {"exit_code": 7, "stdout": "out", "stderr": "err"})
        result = self.actions.require("execute_command").run(
            {"command": "pwd", "cwd": None}, self.context
        )
        self.assertEqual(result["stdout"].strip(), str(self.root))


if __name__ == "__main__":
    unittest.main()
